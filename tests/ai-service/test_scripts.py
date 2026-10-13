import argparse
import ast
import asyncio
import importlib
import io
import json
import os
import re
import subprocess
import time
import tomllib
import urllib.error
from contextlib import redirect_stdout
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
import ai.rag.indexing as indexing_module
import ai.workflows.backtest_workflow as backtest_workflow
import httpx
import pytest
import pytest_asyncio
import qdrant as qdrant_module
from ai.providers.base import PriceBar
from ai.tools import indicators as production_metrics
from ai.workflows.backtest_workflow import _twr_daily_returns
from config import settings
from database import SessionLocal
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels
from sqlalchemy import delete, func, select, text
from domains.analysis.model import Analysis, Recommendation, RecommendationTier
from domains.backtest.model import Backtest
from scripts import smoke_check as smoke
from shared.enums import TierStatus
from shared.http_client import build_http_client

ROOT = Path(__file__).resolve().parents[2]
AI_DIR = ROOT / "services" / "ai-service" / "ai"

recall = importlib.import_module("scripts.measure_rag_recall")
verify = importlib.import_module("scripts.verify_portfolio_backtest")


async def _ok(ctx: smoke.SmokeContext) -> str:
    return "all good"


async def _fail(ctx: smoke.SmokeContext) -> str:
    raise smoke.CheckFailed("wrong answer")


async def _crash(ctx: smoke.SmokeContext) -> str:
    raise RuntimeError("boom\nsecond line")


async def _skip(ctx: smoke.SmokeContext) -> str:
    raise smoke.SkipCheck("nothing to do")


async def _hang(ctx: smoke.SmokeContext) -> str:
    await asyncio.sleep(30)
    return "never"


def _run(checks: list[smoke.Check], *,
         with_llm: bool = False) -> list[smoke.CheckResult]:
    return asyncio.run(
        smoke.run_checks(
            checks, smoke.SmokeContext(with_llm=with_llm), io.StringIO()))


def _daily(now: datetime, first_days_ago: int,
           last_days_ago: int) -> list[datetime]:
    return [(now - timedelta(days=d)).replace(
        hour=4, minute=0, second=0, microsecond=0)
            for d in range(first_days_ago, last_days_ago - 1, -1)]


def _fake_http(
        post_status: int, post_body: str, seen: list[tuple[str, str, object]]):

    def fake(method: str, path: str, body: dict[str, object] | None = None):
        seen.append((method, path, body))
        if path == "/health":
            return 200, json.dumps({"service": "ai-service", "database": True})
        if method == "POST":
            return post_status, post_body
        if path.startswith("/api/v1/analyses/"):
            return 401, "{}"
        return 200, json.dumps({"status": "completed"})

    return fake


async def _smoke_backtest_count() -> int:
    async with SessionLocal() as session:
        return int((
            await session.execute(
                select(func.count()).select_from(Backtest).where(
                    Backtest.strategy_name == smoke.SMOKE_STRATEGY_NAME)
            )).scalar_one())


class _AnySymbolProvider:

    async def get_price_history_range(
            self, ticker: str, *, start: date, end: date,
            interval: str = "1d") -> list[PriceBar]:
        base = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
        days = (end - start).days + 1
        return [
            PriceBar(
                ticker=ticker, interval=interval,
                timestamp=base + timedelta(days=i), open=100 + i, high=100 + i,
                low=100 + i, close=100 + i, volume=1000, source="fake")
            for i in range(days)
        ]


async def _memory_qdrant(
        monkeypatch, *, vector_size: int) -> AsyncQdrantClient:
    client = AsyncQdrantClient(location=":memory:")
    await client.create_collection(
        collection_name=settings.QDRANT_COLLECTION_RESEARCH,
        vectors_config=qmodels.VectorParams(
            size=vector_size, distance=qmodels.Distance.COSINE))
    monkeypatch.setattr(qdrant_module, "_client", client)
    monkeypatch.setattr(indexing_module, "_bm25_index", None)
    return client


async def _add_chunk(
        client: AsyncQdrantClient, *, ticker: str, category: str, text: str,
        document_id: str, content_date: str, chunk_index: int = 0) -> None:
    point_id = str(uuid4())
    vector = [0.0] * settings.QDRANT_VECTOR_SIZE
    vector[chunk_index % settings.QDRANT_VECTOR_SIZE] = 1.0
    await client.upsert(
        collection_name=settings.QDRANT_COLLECTION_RESEARCH, points=[
            qmodels.PointStruct(
                id=point_id, vector=vector, payload={
                    "chunk_id": point_id,
                    "text": text,
                    "category": category,
                    "ticker": ticker,
                    "document_id": document_id,
                    "content_date": content_date,
                    "chunk_index": chunk_index
                })
        ])


class TestSmokeRunner:

    def test_isolation_timeouts_memo_main_and_registry(self):
        results = _run([
            smoke.Check("a_crash", _crash),
            smoke.Check("b_fail", _fail),
            smoke.Check("c_ok", _ok),
            smoke.Check("d_skip", _skip)
        ])
        assert [(r.name, r.status) for r in results] == [("a_crash", "FAIL"),
                                                         ("b_fail", "FAIL"),
                                                         ("c_ok", "OK"),
                                                         ("d_skip", "SKIP")]
        assert results[0].detail == "RuntimeError: boom second line"
        assert results[1].detail == "wrong answer"
        assert results[2].detail == "all good"
        assert results[3].detail == "nothing to do"

        started = time.monotonic()
        results = _run([
            smoke.Check("slow", _hang, timeout=0.05),
            smoke.Check("fast", _ok)
        ])
        assert time.monotonic() - started < 5
        assert results[0].status == "FAIL"
        assert results[0].detail == "timed out after 0.05s"
        assert results[1].status == "OK"

        check = smoke.Check("llm", _ok, needs_llm=True)
        assert _run([check])[0].status == "SKIP"
        assert "--with-llm" in _run([check])[0].detail
        assert _run([check], with_llm=True)[0].status == "OK"

        calls: list[int] = []

        async def scenario() -> None:
            ctx = smoke.SmokeContext()

            async def factory() -> int:
                calls.append(1)
                return 7

            assert await ctx.once("k", factory) == 7
            assert await ctx.once("k", factory) == 7

            async def broken() -> int:
                raise RuntimeError("x")

            with pytest.raises(RuntimeError):
                await ctx.once("other", broken)
            assert "other" not in ctx.memo

        asyncio.run(scenario())
        assert calls == [1]

        checks = [smoke.Check("good", _ok), smoke.Check("bad", _fail)]
        cleanups: list[int] = []

        async def _cleanup() -> None:
            cleanups.append(1)

        out = io.StringIO()
        assert smoke.main([], checks=checks, out=out, cleanup=_cleanup) == 1
        lines = out.getvalue().splitlines()
        assert lines[0].startswith("OK   good")
        assert lines[1].startswith("FAIL bad")
        assert lines[-1].startswith("SUMMARY ok=1 fail=1 skip=0 total=2")
        assert cleanups == [1]

        out = io.StringIO()
        assert (
            smoke.main(["--only", "good"], checks=checks, out=out,
                       cleanup=_cleanup) == 0)
        assert [line.split()[1]
                for line in out.getvalue().splitlines()[:-1]] == ["good"]

        out = io.StringIO()
        assert (
            smoke.main(["--only", "nope"], checks=checks, out=out,
                       cleanup=_cleanup) == 2)
        assert out.getvalue() == ""

        names = [c.name for c in smoke.CHECKS]
        assert len(names) == len(set(names)) == 18
        assert [c.name for c in smoke.CHECKS if c.needs_llm] == ["llm"]


class TestSmokeChecks:

    async def test_offline_window_filing_golden_and_scoring_checks(
            self, monkeypatch) -> None:
        import ai.evaluation.rag_recall as recall_module
        import scripts.backfill_filing_records as backfill
        from ai.evaluation.rag_recall import GoldenQAPair
        from shared.enums import DocumentCategory

        now = datetime(2026, 9, 29, 20, 0, tzinfo=UTC)
        assert (
            smoke.window_coverage_problem(_daily(now, 89, 0), now, 90) is None)
        assert smoke.window_coverage_problem([], now, 90) == "no bars"
        too_short = smoke.window_coverage_problem(_daily(now, 30, 0), now, 90)
        assert too_short is not None and "uncovered" in too_short
        too_old = smoke.window_coverage_problem(_daily(now, 140, 0), now, 90)
        assert too_old is not None and "before the window start" in too_old
        stale = smoke.window_coverage_problem(_daily(now, 89, 10), now, 90)
        assert stale is not None and "stale" in stale
        naive = [ts.replace(tzinfo=None) for ts in _daily(now, 89, 0)]
        assert smoke.window_coverage_problem(naive, now, 90) is None

        cases = {
            "all-filings-have-records": [],
            "filings-without-records": [
                f"AMD_10-Q_2026-0{i}-01.txt" for i in range(1, 8)
            ]
        }
        for case_id, missing in cases.items():
            pending = {"AMD": [(name, "10-Q", "2026") for name in missing]}

            async def _tickers(pending=pending):
                return list(pending)

            async def _pending(ticker, pending=pending):
                return pending[ticker]

            monkeypatch.setattr(backfill, "filing_tickers", _tickers)
            monkeypatch.setattr(backfill, "_pending", _pending)
            if not missing:
                detail = await smoke.check_filing_records(smoke.SmokeContext())
                assert detail == "every filing document has its records row"
                continue
            with pytest.raises(smoke.CheckFailed, match="7 filing") as exc:
                await smoke.check_filing_records(smoke.SmokeContext())
            assert "and 2 more" in str(exc.value), case_id

        seen: list[str] = []

        async def _embed(texts, *, purpose="document"):
            seen.append(purpose)
            return [[0.0] for _ in texts]

        monkeypatch.setattr(recall_module, "embed_texts", _embed)
        pair = GoldenQAPair(
            question="q", chunk_id="c", document_id=None, ticker=None,
            category=DocumentCategory.NEWS)
        assert await recall.embed_golden_questions([pair]) == [[0.0]]
        assert seen == ["query"]

        detail = await smoke.check_backtest_metrics(smoke.SmokeContext())
        assert detail.endswith("metric assertions passed")
        detail = await smoke.check_review(smoke.SmokeContext())
        assert detail == "9 scoring assertions passed"

    async def test_internal_api_probe_and_unexpected_post_status(
            self, monkeypatch) -> None:
        seen: list[tuple[str, str, object]] = []
        monkeypatch.setattr(smoke, "_http", _fake_http(401, "{}", seen))
        ctx = smoke.SmokeContext()
        analysis_id = uuid4()
        ctx.memo["latest_analysis"] = (analysis_id, "AAPL")
        detail = await smoke.check_internal_api(ctx)
        assert "status=completed" in detail
        posts = [entry for entry in seen if entry[0] == "POST"]
        assert posts == [("POST", "/api/v1/analyses", {"ticker": ""})]
        assert ("GET", f"/internal/analyses/{analysis_id}", None) in seen

        cases = {
            "created-analysis-names-its-id": (
                201, json.dumps({"data": {
                    "id": "created-analysis-id"
                }}), "created-analysis-id"),
            "unprocessable-is-a-failure": (422, "{}", "want 401")
        }
        for case_id, (post_status, post_body, match) in cases.items():
            monkeypatch.setattr(
                smoke, "_http", _fake_http(post_status, post_body, []))
            with pytest.raises(smoke.CheckFailed, match=match):
                await smoke.check_internal_api(smoke.SmokeContext())

    async def test_db_queue_tier_dry_and_backtest_replay_checks(
            self, monkeypatch, _schema_ready) -> None:
        detail = await smoke.check_db(smoke.SmokeContext())
        assert detail.startswith("schema=ai analyses:")
        try:
            queue_detail = await smoke.check_queue(smoke.SmokeContext())
            assert queue_detail.startswith("DB view")
        except smoke.CheckFailed as exc:
            assert "more than one running" in str(exc)

        with pytest.raises(smoke.SkipCheck,
                           match="accepted_awaiting_trigger="):
            await smoke.check_tier_monitoring_dry(smoke.SmokeContext())

        monkeypatch.setattr(
            backtest_workflow, "get_market_data_provider",
            lambda: _AnySymbolProvider())
        ticker = f"SMK{uuid4().hex[:5].upper()}"
        async with SessionLocal() as session:
            analysis = Analysis(ticker=ticker)
            session.add(analysis)
            await session.flush()
            recommendation = Recommendation(
                analysis_id=analysis.id, recommendation="buy", confidence=0.8,
                reasoning="smoke fixture", debate_converged=True)
            session.add(recommendation)
            await session.flush()
            tier = RecommendationTier(
                recommendation_id=recommendation.id, side="buy",
                target_price=101, quantity=2,
                status=TierStatus.TRIGGERED.value,
                triggered_at=datetime.now(UTC) + timedelta(seconds=1),
                triggered_price=101)
            session.add(tier)
            await session.commit()
            analysis_id, recommendation_id = analysis.id, recommendation.id
            tier_id = tier.id

        try:
            before = await _smoke_backtest_count()
            detail = await smoke.check_backtest_replay(smoke.SmokeContext())
            assert detail.startswith(f"single-ticker {ticker} ")
            assert "completed" in detail
            assert await _smoke_backtest_count() == before == 0
        finally:
            async with SessionLocal() as session:
                await session.execute(
                    delete(RecommendationTier).where(
                        RecommendationTier.id == tier_id))
                await session.execute(
                    delete(Recommendation).where(
                        Recommendation.id == recommendation_id))
                await session.execute(
                    delete(Analysis).where(Analysis.id == analysis_id))
                await session.commit()

    async def test_qdrant_bm25_retrieval_and_filing_ticker_checks(
            self, monkeypatch) -> None:
        client = await _memory_qdrant(
            monkeypatch, vector_size=settings.QDRANT_VECTOR_SIZE)
        await _add_chunk(
            client, ticker="AAPL", category="filing",
            text="Apple revenue grew; risk factors include supply chain.",
            document_id="d-old", content_date="2026-05-01")
        await _add_chunk(
            client, ticker="AAPL", category="filing",
            text="Quarterly revenue and new risk factors for Apple.",
            document_id="d-new", content_date="2026-08-01", chunk_index=1)
        await _add_chunk(
            client, ticker="MSFT", category="filing",
            text="Microsoft revenue risk factors.", document_id="d-m",
            content_date="2026-08-02")
        await _add_chunk(
            client, ticker="AAPL", category="news",
            text="Apple revenue headline risk factors.", document_id="d-n",
            content_date="2026-08-03")
        ctx = smoke.SmokeContext()

        qdrant_detail = await smoke.check_qdrant(ctx)
        assert f"size={settings.QDRANT_VECTOR_SIZE} points=4" in qdrant_detail
        assert await smoke.check_bm25(ctx) == (
            "chunks=4 filing hits for AAPL=2")
        retrieval = await smoke.check_retrieval(ctx)
        assert retrieval == "AAPL: 2 chunks; newest filing 2026-08-01"

        await _memory_qdrant(monkeypatch, vector_size=8)
        with pytest.raises(smoke.CheckFailed, match="vector size 8"):
            await smoke.check_qdrant(smoke.SmokeContext())
        with pytest.raises(smoke.SkipCheck, match="no filing chunks"):
            await smoke._filing_ticker(smoke.SmokeContext())

        client = await _memory_qdrant(
            monkeypatch, vector_size=settings.QDRANT_VECTOR_SIZE)
        for i in range(2):
            await _add_chunk(
                client, ticker="MSFT", category="filing", text=f"m {i}",
                document_id="m", content_date="2026-08-01", chunk_index=i)
        await _add_chunk(
            client, ticker="KO", category="filing", text="k", document_id="k",
            content_date="2026-08-01")
        assert await smoke._filing_ticker(smoke.SmokeContext()) == "MSFT"


class Seeder:

    def __init__(self) -> None:
        self.rows: list[tuple[str, Any]] = []

    async def insert(self, table: str, **cols: Any) -> UUID:
        cols.setdefault("id", uuid4())
        placeholders = []
        params: dict[str, Any] = {}
        for key, value in cols.items():
            if isinstance(value, dict):
                placeholders.append(f"CAST(:{key} AS jsonb)")
                params[key] = json.dumps(value)
            else:
                placeholders.append(f":{key}")
                params[key] = value
        sql = (
            f"INSERT INTO {table} ({', '.join(cols)}) "
            f"VALUES ({', '.join(placeholders)})")
        async with SessionLocal() as session:
            await session.execute(text(sql), params)
            await session.commit()
        self.rows.append((table, cols["id"]))
        return cols["id"]

    async def purge(self) -> None:
        async with SessionLocal() as session:
            for table, row_id in reversed(self.rows):
                await session.execute(
                    text(f"DELETE FROM {table} WHERE id = :id"),
                    {"id": row_id})
            await session.commit()


async def _agent_id(name: str) -> UUID:
    from domains.analysis.repository import AgentCatalogRepository

    async with SessionLocal() as session:
        agent = await AgentCatalogRepository(session).get_or_create(name)
        await session.commit()
        return agent.id


async def _scalar(sql: str, **params: Any) -> Any:
    async with SessionLocal() as session:
        return (await session.execute(text(sql), params)).scalar_one_or_none()


async def _count(table: str, where: str, **params: Any) -> int:
    value = await _scalar(
        f"SELECT COUNT(*) FROM {table} WHERE {where}", **params)
    return int(value)


async def _exists(table: str, row_id: UUID) -> bool:
    return await _count(table, "id = :id", id=row_id) == 1


@pytest_asyncio.fixture
async def seeder(_schema_ready):
    s = Seeder()
    yield s
    await s.purge()


@pytest_asyncio.fixture
async def fake_qdrant(monkeypatch):
    client = AsyncQdrantClient(location=":memory:")
    await client.create_collection(
        collection_name=settings.QDRANT_COLLECTION_RESEARCH,
        vectors_config=qmodels.VectorParams(
            size=4, distance=qmodels.Distance.COSINE))
    monkeypatch.setattr(qdrant_module, "_client", client)
    yield client
    await client.close()


@pytest_asyncio.fixture
async def core_documents(_schema_ready):
    async with SessionLocal() as session:
        await session.execute(text("CREATE SCHEMA IF NOT EXISTS core"))
        await session.execute(
            text(
                "CREATE TABLE IF NOT EXISTS core.documents "
                "(id uuid PRIMARY KEY, filename text NOT NULL)"))
        await session.commit()
    created: list[UUID] = []

    async def _add(filename: str) -> UUID:
        doc_id = uuid4()
        async with SessionLocal() as session:
            await session.execute(
                text(
                    "INSERT INTO core.documents (id, filename) "
                    "VALUES (:id, :f)"), {
                        "id": doc_id,
                        "f": filename
                    })
            await session.commit()
        created.append(doc_id)
        return doc_id

    yield _add
    async with SessionLocal() as session:
        for doc_id in created:
            await session.execute(
                text("DELETE FROM core.documents WHERE id = :id"),
                {"id": doc_id})
        await session.commit()


@pytest_asyncio.fixture
async def unowned_documents_purged(core_documents):
    owned = (
        "SELECT id FROM core.documents UNION SELECT id FROM ai.agent_reports")
    async with SessionLocal() as session:
        for table in ("ai.document_chunks", "ai.embedding_jobs"):
            await session.execute(
                text(
                    f"DELETE FROM {table} WHERE document_id NOT IN ({owned})"))
        await session.commit()


async def _add_points(client: Any, document_id: str, n: int,
                      **payload: Any) -> list[str]:
    from ai.rag.indexing import chunk_point_id

    ids = [chunk_point_id(document_id, i) for i in range(n)]
    await client.upsert(
        collection_name=settings.QDRANT_COLLECTION_RESEARCH, points=[
            qmodels.PointStruct(
                id=pid, vector=[0.1, 0.2, 0.3, 0.4], payload={
                    "document_id": document_id,
                    "chunk_id": pid,
                    "chunk_index": i,
                    "text": f"chunk {i}",
                    **payload
                }) for i, pid in enumerate(ids)
        ])
    return ids


async def _points_for(client: Any, document_id: str) -> list[dict[str, Any]]:
    points, _ = await client.scroll(
        collection_name=settings.QDRANT_COLLECTION_RESEARCH,
        scroll_filter=qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="document_id", match=qmodels.MatchValue(
                        value=document_id))
            ]), limit=100, with_payload=True)
    return [p.payload for p in points]


async def _entity(seeder: Seeder, name: str) -> UUID:
    return await seeder.insert(
        "ai.kg_entities", name=name, normalized_name=name.lower(),
        entity_type="concept")


async def _chunk(seeder: Seeder, document_id: UUID, index: int = 0) -> UUID:
    from ai.rag.indexing import chunk_point_id

    return await seeder.insert(
        "ai.document_chunks", document_id=document_id, chunk_index=index,
        token_count=10,
        qdrant_point_id=chunk_point_id(str(document_id), index))


async def _seed_delete_case(
        seeder: Seeder, fake_qdrant: Any,
        core_documents: Any) -> dict[str, UUID]:
    t0 = datetime(2031, 3, 2, 13, 30, tzinfo=UTC)
    market = await _agent_id("market")
    news = await _agent_id("news")
    ids: dict[str, UUID] = {}

    ids["A"] = await seeder.insert(
        "ai.analyses", ticker="DAA", status="failed",
        failure_reason="judge_unavailable", created_at=t0)
    ids["A_run"] = await seeder.insert(
        "ai.agent_runs", analysis_id=ids["A"], agent_id=market,
        status="completed", started_at=t0,
        completed_at=t0 + timedelta(minutes=5))
    ids["A_res"] = await seeder.insert(
        "ai.agent_results", agent_run_id=ids["A_run"], score=0.9, summary="s")
    ids["A_rep"] = await seeder.insert(
        "ai.agent_reports", analysis_id=ids["A"], agent_role="news",
        ticker="DAA", category="news", content="an archived DAA news note",
        created_at=t0 + timedelta(minutes=12))
    await _add_points(
        fake_qdrant, str(ids["A_rep"]), 2, ticker="DAA", category="news")
    ids["A_chunk"] = await _chunk(seeder, ids["A_rep"])
    ids["A_job"] = await seeder.insert(
        "ai.embedding_jobs", document_id=ids["A_rep"], embedding_model="m",
        status="ready")
    ids["E1"] = await _entity(seeder, "DAA only entity")
    ids["E2"] = await _entity(seeder, "DAA shared entity")
    ids["A_rel"] = await seeder.insert(
        "ai.kg_relations", source_entity_id=ids["E1"],
        target_entity_id=ids["E2"], relation_type="depends_on",
        source_report_id=ids["A_rep"])
    ids["A_rec"] = await seeder.insert(
        "ai.recommendations", analysis_id=ids["A"], recommendation="hold",
        confidence=0.0, reasoning="x", debate_converged=False)
    ids["A_tier"] = await seeder.insert(
        "ai.recommendation_tiers", recommendation_id=ids["A_rec"], side="buy",
        target_price=10, quantity=1, status="superseded")
    ids["A_metric"] = await seeder.insert(
        "ai.recommendation_metrics", recommendation_id=ids["A_rec"],
        accuracy_score=0.5)
    ids["A_sess"] = await seeder.insert(
        "ai.debate_sessions", analysis_id=ids["A"])
    ids["A_arg"] = await seeder.insert(
        "ai.debate_arguments", debate_session_id=ids["A_sess"], side="bull",
        content="bull", round_number=1)
    ids["A_refl"] = await seeder.insert(
        "ai.reflections", analysis_id=ids["A"], round_number=1, verified=False)
    ids["A_epi"] = await seeder.insert(
        "ai.episodic_memories", analysis_id=ids["A"], ticker="DAA",
        recommendation="hold", confidence=0.0, summary="episode")
    ids["A_mem"] = await seeder.insert(
        "ai.agent_memories", agent_id=market, ticker="DAA", score=0.9,
        memory_type="trajectory", content={"final_answer": "A market memory"},
        created_at=t0 + timedelta(minutes=11))
    ids["news_mem"] = await seeder.insert(
        "ai.agent_memories", agent_id=news, ticker="DAA", score=0.9,
        memory_type="trajectory", content={"final_answer": "news memory"},
        created_at=t0 + timedelta(minutes=11))

    ids["K"] = await seeder.insert(
        "ai.analyses", ticker="DAA", status="completed",
        created_at=t0 + timedelta(minutes=60))
    ids["K_run"] = await seeder.insert(
        "ai.agent_runs", analysis_id=ids["K"], agent_id=market,
        status="completed", started_at=t0 + timedelta(minutes=60),
        completed_at=t0 + timedelta(minutes=65))
    ids["K_mem"] = await seeder.insert(
        "ai.agent_memories", agent_id=market, ticker="DAA", score=0.9,
        memory_type="trajectory", content={"final_answer": "K market memory"},
        created_at=t0 + timedelta(minutes=70))
    ids["K_rep"] = await seeder.insert(
        "ai.agent_reports", analysis_id=ids["K"], agent_role="risk",
        ticker="DAA", category="risk_analysis", content="K risk note")
    ids["K_rel"] = await seeder.insert(
        "ai.kg_relations", source_entity_id=ids["E2"],
        target_entity_id=ids["E2"], relation_type="self",
        source_report_id=ids["K_rep"])

    ids["B"] = await seeder.insert(
        "ai.analyses", ticker="DAB", status="completed",
        created_at=t0 + timedelta(minutes=2))
    ids["B_rec"] = await seeder.insert(
        "ai.recommendations", analysis_id=ids["B"], recommendation="hold",
        confidence=0.0, reasoning="weak", debate_converged=False)

    ids["D"] = await seeder.insert(
        "ai.analyses", ticker="DAD", status="failed", failure_reason="timeout",
        created_at=t0 + timedelta(minutes=3))
    ids["D_rec"] = await seeder.insert(
        "ai.recommendations", analysis_id=ids["D"], recommendation="buy",
        confidence=0.6, reasoning="r", debate_converged=True)
    ids["D_dec"] = await seeder.insert(
        "ai.investment_decisions", user_id=uuid4(),
        recommendation_id=ids["D_rec"], ticker="DAD", action_taken="followed",
        notes="kept")

    ids["R"] = await seeder.insert(
        "ai.analyses", ticker="DAR", status="running",
        created_at=t0 + timedelta(minutes=4))

    ids["core_doc"] = await core_documents("DAA_10-Q_2031-01-01.txt")
    await _add_points(
        fake_qdrant, str(ids["core_doc"]), 2, ticker="DAA", category="filing")
    for n in (2, 3):
        extra = await core_documents(f"DAA_10-Q_2031-0{n}-01.txt")
        await _add_points(
            fake_qdrant, str(extra), 1, ticker="DAA", category="filing")
    ids["orphan_doc"] = uuid4()
    await _add_points(
        fake_qdrant, str(ids["orphan_doc"]), 3, ticker="DAO",
        category="filing")
    ids["orphan_chunk"] = await _chunk(seeder, ids["orphan_doc"])
    ids["orphan_job"] = await seeder.insert(
        "ai.embedding_jobs", document_id=ids["orphan_doc"],
        embedding_model="m", status="ready")
    for n in range(5):
        doc = uuid4()
        ids[f"jobs_only_{n}"] = await seeder.insert(
            "ai.embedding_jobs", document_id=doc, embedding_model="m",
            status="ready")
    return ids


DELETED_WITH_A = [("ai.analyses", "A"), ("ai.agent_runs", "A_run"),
                  ("ai.agent_results", "A_res"), ("ai.agent_reports", "A_rep"),
                  ("ai.document_chunks", "A_chunk"),
                  ("ai.embedding_jobs", "A_job"), ("ai.kg_relations", "A_rel"),
                  ("ai.kg_entities", "E1"), ("ai.recommendations", "A_rec"),
                  ("ai.recommendation_tiers", "A_tier"),
                  ("ai.recommendation_metrics", "A_metric"),
                  ("ai.debate_sessions", "A_sess"),
                  ("ai.debate_arguments", "A_arg"),
                  ("ai.reflections", "A_refl"),
                  ("ai.episodic_memories", "A_epi"),
                  ("ai.agent_memories", "A_mem"), ("ai.analyses", "B"),
                  ("ai.recommendations", "B_rec"),
                  ("ai.document_chunks", "orphan_chunk"),
                  ("ai.embedding_jobs", "orphan_job"),
                  *[("ai.embedding_jobs", f"jobs_only_{n}") for n in range(5)]]
KEPT = [("ai.analyses", "K"), ("ai.agent_runs", "K_run"),
        ("ai.agent_memories", "K_mem"), ("ai.agent_memories", "news_mem"),
        ("ai.agent_reports", "K_rep"), ("ai.kg_relations", "K_rel"),
        ("ai.kg_entities", "E2"), ("ai.analyses", "D"),
        ("ai.recommendations", "D_rec"), ("ai.investment_decisions", "D_dec"),
        ("ai.analyses", "R")]


class TestDeleteAnalyses:

    @pytest.mark.asyncio
    async def test_fk_tree_selection_memory_window_and_cli_rules(self, seeder):
        from scripts.delete_analyses import (AnalysisTarget, _select_ids,
                                             foreign_key_tree, memory_window,
                                             parse_args, rag_sweep_refusal)

        async with SessionLocal() as session:
            tree = await foreign_key_tree(session, "ai", "recommendations")
        by_column = {fk.referencing: depth for depth, fk in tree}
        assert by_column["ai.recommendation_tiers.recommendation_id"] == 1
        assert by_column["ai.investment_decisions.recommendation_id"] == 1
        assert by_column["ai.recommendation_metrics.recommendation_id"] == 1
        assert by_column["ai.decision_outcomes.decision_id"] == 2
        assert by_column["ai.decision_reviews.decision_id"] == 2

        day = datetime(2032, 5, 6, 23, 59, tzinfo=UTC)
        failed = await seeder.insert(
            "ai.analyses", ticker="DSF", status="failed", created_at=day)
        inside = await seeder.insert(
            "ai.analyses", ticker="DSI", status="completed", created_at=day)
        outside = await seeder.insert(
            "ai.analyses", ticker="DSO", status="completed",
            created_at=day + timedelta(minutes=2))
        async with SessionLocal() as session:
            by_failed, _ = await _select_ids(session, [], True, None, None)
            by_window, _ = await _select_ids(
                session, [], False, date(2032, 5, 6), date(2032, 5, 6))
            nothing, _ = await _select_ids(session, [], False, None, None)
        assert failed in by_failed and inside not in by_failed
        assert {failed, inside} <= set(by_window) and outside not in by_window
        assert failed not in nothing and inside not in nothing

        t0 = datetime(2033, 1, 1, 10, 0, tzinfo=UTC)
        market = await _agent_id("market")
        first = await seeder.insert(
            "ai.analyses", ticker="DOV", status="completed", created_at=t0)
        await seeder.insert(
            "ai.agent_runs", analysis_id=first, agent_id=market,
            status="completed", completed_at=t0 + timedelta(minutes=30))
        second = await seeder.insert(
            "ai.analyses", ticker="DOV", status="failed",
            created_at=t0 + timedelta(minutes=5))
        await seeder.insert(
            "ai.agent_runs", analysis_id=second, agent_id=market,
            status="completed", completed_at=t0 + timedelta(minutes=10))
        target = AnalysisTarget(
            analysis_id=second, ticker="DOV", status="failed",
            failure_reason=None, created_at=t0 + timedelta(minutes=5))
        async with SessionLocal() as session:
            window = await memory_window(session, target)
        assert isinstance(window, str) and "still running" in window

        assert rag_sweep_refusal(0, 0) is None
        assert rag_sweep_refusal(25, 100) is None
        refusal = rag_sweep_refusal(26, 100)
        assert refusal is not None and "refused" in refusal

        a = uuid4()
        args = parse_args(["--id", str(a), "--failed", "--confirm"])
        assert args.ids == [a] and args.failed and args.confirm
        with pytest.raises(SystemExit):
            parse_args(["--since", "2026-09-01"])
        with pytest.raises(SystemExit):
            parse_args(["--since", "2026-09-02", "--until", "2026-09-01"])

    @pytest.mark.asyncio
    async def test_preview_then_confirm_then_rerun(
            self, seeder, fake_qdrant, core_documents,
            unowned_documents_purged, capsys):
        from scripts.delete_analyses import run

        ids = await _seed_delete_case(seeder, fake_qdrant, core_documents)
        select_ids = [ids["A"], ids["B"], ids["D"], ids["R"]]

        plan = await run(confirm=False, ids=select_ids)
        out = capsys.readouterr().out
        orphans = {d.document_id for d in plan.orphan_documents}
        assert plan.unhandled_fks == []
        assert plan.rag_sweep_allowed, plan.rag_sweep_note
        assert {t.analysis_id for t in plan.deletable} >= {ids["A"], ids["B"]}
        assert ids["D"] not in {t.analysis_id for t in plan.deletable}
        assert ids["R"] not in {t.analysis_id for t in plan.deletable}
        assert "has user investment decisions" in out
        assert "status is running" in out
        target_a = next(t for t in plan.targets if t.analysis_id == ids["A"])
        assert [m.memory_id for m in target_a.memories] == [ids["A_mem"]]
        assert target_a.reports[0].qdrant_points == 2
        assert "[concept] 'DAA only entity'" in plan.kg_orphans_after
        assert "[concept] 'DAA shared entity'" not in plan.kg_orphans_after
        assert str(ids["orphan_doc"]) in orphans
        assert str(ids["core_doc"]) not in orphans
        assert str(ids["A_rep"]) not in orphans
        assert sum(1 for d in plan.orphan_documents if not d.has_content) >= 5
        assert "with only leftover embedding_jobs rows" in out
        for table, key in DELETED_WITH_A + KEPT:
            assert await _exists(
                table, ids[key]), f"preview changed {table} {key}"
        assert len(await _points_for(fake_qdrant, str(ids["A_rep"]))) == 2

        plan = await run(confirm=True, ids=select_ids)
        capsys.readouterr()
        assert plan.applied_analyses == 2
        for table, key in DELETED_WITH_A:
            assert not await _exists(
                table, ids[key]), f"{table} {key} not deleted"
        for table, key in KEPT:
            assert await _exists(table, ids[key]), f"{table} {key} was deleted"
        assert await _points_for(fake_qdrant, str(ids["A_rep"])) == []
        assert await _points_for(fake_qdrant, str(ids["orphan_doc"])) == []
        assert len(await _points_for(fake_qdrant, str(ids["core_doc"]))) == 2

        plan = await run(confirm=True, ids=select_ids)
        capsys.readouterr()
        assert plan.applied_analyses == 0
        assert plan.orphan_documents == []
        assert plan.not_found == [ids["A"], ids["B"]]
        for table, key in KEPT:
            assert await _exists(table, ids[key])

    @pytest.mark.asyncio
    async def test_unhandled_fk_blocks_apply_and_report_only_delete(
            self, seeder, fake_qdrant, core_documents, capsys):
        from scripts.delete_analyses import run

        victim = await seeder.insert(
            "ai.analyses", ticker="DFK", status="failed",
            created_at=datetime(2034, 1, 1, tzinfo=UTC))
        async with SessionLocal() as session:
            await session.execute(
                text(
                    "CREATE TABLE ai.tmp_delete_fk_probe (id uuid PRIMARY "
                    "KEY, "
                    "analysis_id uuid REFERENCES ai.analyses(id))"))
            await session.commit()
        try:
            plan = await run(confirm=True, ids=[victim])
            out = capsys.readouterr().out
            assert "ai.tmp_delete_fk_probe.analysis_id" in plan.unhandled_fks
            assert "NOT HANDLED" in out and "=== Applying ===" not in out
            assert await _exists("ai.analyses", victim)
        finally:
            async with SessionLocal() as session:
                await session.execute(
                    text("DROP TABLE ai.tmp_delete_fk_probe"))
                await session.commit()

        t0 = datetime(2031, 4, 2, 13, 30, tzinfo=UTC)
        analysis = await seeder.insert(
            "ai.analyses", ticker="RPT", status="completed", created_at=t0)
        await seeder.insert(
            "ai.analyses", ticker="RPT", status="completed",
            created_at=t0 + timedelta(days=1))
        keep = await seeder.insert(
            "ai.agent_reports", analysis_id=analysis, agent_role="fundamental",
            ticker="RPT", category="analyst_report",
            content="fresh numbers note", created_at=t0 + timedelta(minutes=5))
        drop = await seeder.insert(
            "ai.agent_reports", analysis_id=analysis, agent_role="macro",
            ticker="RPT", category="macro_research",
            content="recycled macro note",
            created_at=t0 + timedelta(minutes=6))
        await _add_points(
            fake_qdrant, str(drop), 1, ticker="RPT", category="macro_research")
        await _add_points(
            fake_qdrant, str(keep), 1, ticker="RPT", category="analyst_report")
        await _chunk(seeder, drop)
        e1 = await _entity(seeder, "RPT recycled entity")
        e2 = await _entity(seeder, "RPT other entity")
        rel = await seeder.insert(
            "ai.kg_relations", source_entity_id=e1, target_entity_id=e2,
            relation_type="depends_on", source_report_id=drop)

        plan = await run(confirm=False, report_ids=[drop, uuid4()])
        out = capsys.readouterr().out
        assert [r.report_id for r in plan.reports] == [drop]
        assert plan.reports[0].qdrant_points == 1
        assert plan.reports[0].kg_relations == 1
        assert len(plan.reports_not_found) == 1
        assert "[concept] 'RPT recycled entity'" in plan.kg_orphans_after
        assert "[concept] 'RPT other entity'" in plan.kg_orphans_after
        assert "Archived notes to delete on their own" in out
        assert await _exists("ai.agent_reports", drop)

        plan = await run(confirm=True, report_ids=[drop])
        capsys.readouterr()
        assert plan.applied_reports == 1
        assert not await _exists("ai.agent_reports", drop)
        assert not await _exists("ai.kg_relations", rel)
        assert await _points_for(fake_qdrant, str(drop)) == []
        assert await _exists("ai.agent_reports", keep)
        assert len(await _points_for(fake_qdrant, str(keep))) == 1
        assert await _exists("ai.analyses", analysis)


def _tier_checker(out=None):
    from scripts.e2e_tier_check import TierCheck

    args = argparse.Namespace(
        keep=False, verbose=False, break_lock=False,
        base_url="https://gateway", host_header="localhost")
    checker = TierCheck(args, out or io.StringIO())
    checker.admin_id = uuid4()
    checker.portfolio_id = str(uuid4())
    checker.portfolio_deleted = True
    return checker


async def _accept_all(checker):
    async with SessionLocal() as session:
        await session.execute(
            text(
                "UPDATE ai.recommendation_tiers "
                "SET status = 'accepted_awaiting_trigger' "
                "WHERE recommendation_id = :rec"),
            {"rec": checker.recommendation_id})
        await session.commit()


@pytest.fixture
def fake_core(monkeypatch):
    import ai_events
    import clients.core_client as core_client

    orders: list[dict] = []
    events: list[tuple] = []

    async def _record(portfolio_id, **kwargs):
        if kwargs["quantity"] > 10:
            raise core_client.TransactionRejectedError(
                422, "Not enough shares held to sell 100000.0 AMD.")
        if not any(o["idempotency_key"] == kwargs["idempotency_key"]
                   for o in orders):
            orders.append({"portfolio_id": portfolio_id, **kwargs})
        return {"id": str(uuid4())}

    async def _publish(name, payload):
        events.append((str(name), payload))
        return True

    monkeypatch.setattr(core_client, "record_transaction", _record)
    monkeypatch.setattr(ai_events, "publish_event", _publish)
    return orders, events


class TestE2eTierCheck:

    @pytest.mark.asyncio
    async def test_seed_execute_reject_and_cleanup(
            self, fake_core, _schema_ready):
        orders, events = fake_core
        checker = _tier_checker()
        await checker.s_seed()
        assert set(checker.tier_ids) == {"executed", "rejected", "cancelled"}
        rows = await checker.tier_rows()
        assert {status
                for status, _ in rows.values()} == {"pending_acceptance"}

        await _accept_all(checker)
        assert await checker.s_accepted() == (
            "3 tiers accepted_awaiting_trigger")

        assert await checker.execute("executed") == "triggered"
        assert await checker.execute("executed", again=True) == "sent again"
        assert len(orders) == 1
        order = orders[0]
        assert (order["transaction_type"], order["quantity"],
                order["price"]) == ("sell", 3.0, 120.0)
        assert order["idempotency_key"] == (
            f"tier:{checker.tier_ids['executed']}")

        assert (await checker.s_rejected()
                ).startswith("core refused 100000 shares: Tier ")
        assert [name for name, _ in events
                ] == ["tier.executed", "tier.execution_failed"]
        assert events[0][1]["user_id"] == str(checker.admin_id)

        assert (await checker.tier_rows())["cancelled"][0] == (
            "accepted_awaiting_trigger")

        async with SessionLocal() as session:
            await session.execute(
                text(
                    "INSERT INTO ai.investment_decisions (id, user_id, "
                    "recommendation_id, ticker, action_taken, created_at) "
                    "VALUES (:id, :user, :rec, 'AMD', 'followed', now())"), {
                        "id": uuid4(),
                        "user": checker.admin_id,
                        "rec": checker.recommendation_id
                    })
            await session.commit()

        assert (await checker.s_cleanup()
                ).startswith(f"removed analysis {checker.analysis_id}")
        async with SessionLocal() as session:
            left = (
                await session.execute(
                    select(func.count()).select_from(Analysis).where(
                        Analysis.id == checker.analysis_id))).scalar_one()
        assert left == 0
        assert await checker.tier_rows() == {}

    @pytest.mark.asyncio
    async def test_notifications_prerequisites_quantities_and_bad_token(
            self, monkeypatch, capsys):
        from scripts.e2e_tier_check import CheckFailed
        from scripts import e2e_tier_check as script
        import shared.redis as shared_redis

        checker = _tier_checker()
        checker.token = "t"
        old = (checker.started_at - timedelta(minutes=5)).isoformat()
        new = datetime.now(UTC).replace(tzinfo=None).isoformat()

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={
                    "items": [{
                        "type": "tier_cancelled",
                        "title": "Tier cancelled",
                        "content": "portfolio_deleted",
                        "created_at": old
                    }, {
                        "type": "tier_executed",
                        "title": "Tier executed",
                        "content": "A planned tier was executed: "
                        "sell 3 AMD at 120.",
                        "created_at": new
                    }]
                })

        checker.api = httpx.AsyncClient(
            base_url="https://gateway",
            transport=httpx.MockTransport(_handler))
        item = await checker.wait_notification("tier_executed", "sell 3 AMD")
        assert item["type"] == "tier_executed"

        with pytest.raises(CheckFailed,
                           match="types of this run: tier_executed"):
            await checker.wait_notification(
                "tier_cancelled", "portfolio_deleted", timeout=0)
        await checker.api.aclose()

        out = io.StringIO()
        checker = _tier_checker(out)
        checker.token = ""

        await checker.check("admin", checker.s_admin)
        await checker.check(
            "portfolio", checker.s_portfolio, requires=("admin", ))
        assert checker.summary() == 1
        output = out.getvalue()
        assert "FAIL tiers.admin  E2E_ADMIN_TOKEN is not set" in output
        assert "SKIP tiers.portfolio  needs admin" in output
        assert "SUMMARY ok=0 fail=1 skip=1 total=2 -- failed: admin" in output
        await checker.api.aclose()

        assert (
            script.EXECUTED_QUANTITY < script.BUY_QUANTITY <
            script.REJECTED_QUANTITY)
        assert (
            script.EXECUTED_QUANTITY + script.CANCELLED_QUANTITY
            <= script.BUY_QUANTITY)
        assert Decimal("10000") < script.UNREACHABLE_TARGET

        async def _no_lock(*args, **kwargs):
            raise AssertionError("the lock must not be taken")

        monkeypatch.setattr(shared_redis, "acquire_e2e_lock", _no_lock)
        monkeypatch.setenv("E2E_ADMIN_TOKEN", "")
        out = io.StringIO()
        assert await script.main_async([], out) == 2
        err = capsys.readouterr().err
        assert (
            "E2E_ADMIN_TOKEN is not usable: E2E_ADMIN_TOKEN is not set" in err)
        assert "nothing was created" in err
        assert "tiers." not in out.getvalue()


class TestAnalysisCli:

    async def test_invalid_uuid_access_denied_and_committed_budget(
            self, monkeypatch, rows):
        from scripts import analysis_cli

        def _no_urlopen(*args, **kwargs):
            raise AssertionError("no request for an invalid id")

        monkeypatch.setattr(
            analysis_cli.urllib.request, "urlopen", _no_urlopen)
        for flag in ("--user", "--portfolio"):
            with pytest.raises(SystemExit) as exc:
                analysis_cli.main(["create", "AMD", flag, "not-a-uuid"])
            assert exc.value.code == 2, flag

        def _urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found", {},
                io.BytesIO(b'{"error": {"code": "not_found"}}'))

        monkeypatch.setattr(analysis_cli.urllib.request, "urlopen", _urlopen)
        out = io.StringIO()
        with redirect_stdout(out):
            code = analysis_cli.main([
                "create", "AMD", "--user",
                str(uuid4()), "--portfolio",
                str(uuid4())
            ])
        assert code == 1
        assert "not_found" in out.getvalue()

        pf = uuid4()
        analysis_id, _, _ = await rows.recommendation(
            ticker="KO", portfolio_id=pf, action="buy",
            tiers=[("buy", "60", "5", "pending_acceptance"),
                   ("sell", "70", "5", "pending_acceptance")])

        assert await analysis_cli._committed(str(pf)) == (
            f"KO 300.00 (analysis {analysis_id}, pending acceptance)\n"
            "total committed BUY cash: 300.00")
        assert await analysis_cli._committed(str(uuid4())) == (
            "total committed BUY cash: 0.00")
        with pytest.raises(SystemExit):
            analysis_cli.main(["committed", "not-a-uuid"])


class TestUploadSeedAsSystem:

    def _tree(self, tmp_path: Path) -> Path:
        root = tmp_path / "seed"
        (root / "earnings_call").mkdir(parents=True)
        (root / "macro_research").mkdir()
        (root / "earnings_call" /
         "AMD_Q2_call_2026-08-05.txt").write_text("call")
        (root / "macro_research" / "fed_outlook.pdf").write_bytes(b"%PDF-1.4")
        (root / "filing").mkdir()
        (root / "filing" / "AAPL_10-K_2025-10-31.txt").write_text("10-K")
        return root

    @pytest.mark.asyncio
    async def test_metadata_from_file_names_and_dry_run(
            self, tmp_path, monkeypatch):
        from scripts.upload_seed_documents import (_default_seed_root,
                                                   collect_seed_files, main)
        from scripts import upload_seed_documents

        checkout = ROOT / "rag_seed_documents"
        assert _default_seed_root() == (
            checkout if checkout.is_dir() else
            Path("/tmp/rag_seed_documents"))

        root = self._tree(tmp_path)
        files, missing = collect_seed_files(
            {"earnings_call", "macro_research", "filing", "news"}, root)
        by_name = {f.path.name: f for f in files}
        call = by_name["AMD_Q2_call_2026-08-05.txt"]
        assert (call.category, call.ticker, call.content_date, call.title) == (
            "earnings_call", "AMD", "2026-08-05", "AMD_Q2_call_2026-08-05")
        macro = by_name["fed_outlook.pdf"]
        assert (macro.ticker, macro.content_date) == (None, None)
        filing = by_name["AAPL_10-K_2025-10-31.txt"]
        assert (filing.category, filing.ticker,
                filing.content_date) == ("filing", "AAPL", "2025-10-31")
        assert missing == [root / "news"]

        async def _none_stored(*args, **kwargs):
            return set()

        monkeypatch.setattr(
            upload_seed_documents, "existing_filenames", _none_stored)
        out = io.StringIO()
        with redirect_stdout(out):
            code = await main([
                "--as-system", "--dry-run", "--seed-root",
                str(root), "--categories", "earnings_call"
            ])
        assert code == 0
        assert (
            "DRY-RUN AMD_Q2_call_2026-08-05.txt: category=earnings_call"
            in out.getvalue())
        assert "1 files would be uploaded" in out.getvalue()

        with pytest.raises(SystemExit) as exc, redirect_stdout(io.StringIO()):
            await main([])
        assert exc.value.code == 2

    @pytest.mark.asyncio
    async def test_uploads_multipart_duplicates_and_token_header(
            self, tmp_path, monkeypatch):
        from scripts.upload_seed_documents import (SeedFile,
                                                   collect_seed_files,
                                                   run_as_system)
        from shared.config import get_settings

        root = self._tree(tmp_path)
        files, _ = collect_seed_files({"earnings_call", "macro_research"},
                                      root)
        seen: list[httpx.Request] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            body = request.content
            if b"fed_outlook" in body:
                return httpx.Response(
                    201, json={
                        "id": "d2",
                        "status": "uploaded",
                        "duplicate": True
                    })
            if b"AMD_Q2" in body:
                return httpx.Response(
                    201, json={
                        "id": "d1",
                        "status": "uploaded",
                        "duplicate": False
                    })
            return httpx.Response(415, json={"detail": "bad type"})

        client = build_http_client(transport=httpx.MockTransport(_handler))
        out = io.StringIO()
        with redirect_stdout(out):
            counts = await run_as_system(
                files, dry_run=False, client=client,
                core_url="http://core:8000")
        await client.aclose()
        assert counts == {
            "uploaded": 1,
            "duplicates": 1,
            "failed": 0,
            "planned": 0,
            "skipped": 0
        }
        seen = [r for r in seen if r.method == "POST"]
        assert all(
            str(r.url) == "http://core:8000/internal/documents/system"
            for r in seen)
        first = seen[0].content
        assert b'name="category"' in first and b"earnings_call" in first
        assert b'name="ticker"' in first and b"AMD" in first
        assert b'name="content_date"' in first and b"2026-08-05" in first
        assert b'filename="AMD_Q2_call_2026-08-05.txt"' in first
        assert "DUPLICATE fed_outlook.pdf" in out.getvalue()

        shared_settings = get_settings()
        monkeypatch.setattr(
            shared_settings, "INTERNAL_API_TOKEN", "seed-token")
        monkeypatch.setattr(
            shared_settings, "CORE_SERVICE_URL", "http://core-service:8000")
        path = tmp_path / "AMD_x.txt"
        path.write_text("x")
        headers: list = []

        def _token_handler(request):
            headers.append(request.headers.get("X-Internal-Token"))
            return httpx.Response(
                201, json={
                    "id": "d",
                    "status": "uploaded",
                    "duplicate": False
                })

        client = build_http_client(
            transport=httpx.MockTransport(_token_handler))
        with redirect_stdout(io.StringIO()):
            await run_as_system([SeedFile(path, "news", "AMD", None, "AMD_x")],
                                dry_run=False, client=client,
                                core_url=shared_settings.CORE_SERVICE_URL)
        await client.aclose()
        assert headers == ["seed-token", "seed-token"]


class TestRagScripts:

    def test_reembed_args_recall_scores_and_sweep_apply(self, tmp_path: Path):
        from scripts import reembed_collection

        with pytest.raises(SystemExit):
            reembed_collection.parse_args(["--target", "x"])
        args = reembed_collection.parse_args(
            ["--source", "live", "--target", "new"])
        assert (args.source, args.target) == ("live", "new")

        assert recall.rank_of(["a", "b", "c"], "b") == 2
        assert recall.rank_of(["a"], "z") is None
        score = recall.Score()
        for rank in (1, 3, None, 6):
            score.add(rank)
        assert score.total == 4
        assert score.recall(1) == 0.25 and score.recall(5) == 0.5
        assert score.recall(10) == 0.75
        assert score.mrr() == pytest.approx((1 + 1 / 3 + 1 / 6) / 4)

        path = tmp_path / "category_weights.json"
        path.write_text(json.dumps({"_comment": "kept"}))
        tie = recall.SweepResult(
            "news", 20, {(1.0, 0.0): 15,
                         (0.5, 0.5): 15}, (0.5, 0.5))
        thin = recall.SweepResult(
            "filing", 30, {(0.8, 0.2): 21,
                           (0.5, 0.5): 20}, (0.5, 0.5))
        solid = recall.SweepResult(
            "earnings_call", 90, {(0.9, 0.1): 80,
                                  (0.5, 0.5): 70}, recall.live_ratio(None))
        assert recall.apply_sweep([tie, thin, solid],
                                  path) == ["filing", "earnings_call"]
        written = json.loads(path.read_text())
        assert written["_comment"] == "kept" and "news" not in written
        assert written["filing"]["bm25"] == 0.8 and "THIN MARGIN" in (
            written["filing"]["_evidence"])
        assert "THIN MARGIN" not in written["earnings_call"]["_evidence"]
        assert recall.live_ratio({"bm25": 1.0, "vector": 1.0}) == (0.5, 0.5)


def _txn(
        day: str, kind: str, ticker: str, qty: str, price: str | None = None,
        **extra: Decimal) -> object:
    moment = datetime.fromisoformat(day).replace(tzinfo=UTC)
    return verify.Txn(
        day=moment.date(), moment=moment, kind=kind, ticker=ticker,
        quantity=Decimal(qty), price=Decimal(price) if price else None,
        dividend_per_share=extra.get("dps"), split_ratio=extra.get("ratio"))


class TestVerifyPortfolioBacktest:

    def test_positions_tolerances_and_independent_metrics(self):
        txns = [
            _txn("2026-09-01T14:00:00", "deposit", "", "10000"),
            _txn("2026-09-02T15:00:00", "buy", "MSFT", "10", "500"),
            _txn(
                "2026-09-03T15:00:00", "dividend", "MSFT", "10",
                dps=Decimal("0.8")),
            _txn(
                "2026-09-04T15:00:00", "stock_split", "MSFT", "0",
                ratio=Decimal("2")),
            _txn("2026-09-05T15:00:00", "sell", "MSFT", "5", "260"),
            _txn("2026-09-06T15:00:00", "adjustment", "AMD", "3", "100"),
            _txn("2026-09-07T15:00:00", "withdrawal", "", "1000")
        ]
        cash, shares = verify.position_as_of(txns, date(2026, 9, 2))
        assert cash == Decimal("5000") and shares == {"MSFT": Decimal("10")}
        cash, shares = verify.position_as_of(txns, date(2026, 9, 7))
        assert cash == Decimal("5000") + Decimal("8") + Decimal(
            "1300") - Decimal("1000")
        assert shares == {"MSFT": Decimal("15"), "AMD": Decimal("3")}

        assert verify.same(100.004, 100.0, money=True)
        assert not verify.same(100.02, 100.0, money=True)
        assert verify.same(0.1234567, 0.12345671)
        assert not verify.same(None, 0.1) and verify.same(None, None)
        assert verify.same(0.0314085297327937, Decimal("0.0314085297327937"))

        days = [date(2026, 9, d) for d in (1, 2, 3, 4, 7, 8)]
        values = [10000.0, 10100.0, 11050.0, 10900.0, 10700.0, 11200.0]
        benchmark = [500.0, 503.0, 501.0, 498.0, 505.0, 507.0]
        flows = {date(2026, 9, 3): 1000.0, date(2026, 9, 6): -200.0}
        mine = verify.independent_metrics(days, values, benchmark, flows)

        returns = _twr_daily_returns(values, days, flows)
        curve = [values[0]]
        for r in returns:
            curve.append(curve[-1] * (1 + r))
        bench_returns = production_metrics.simple_returns(benchmark)
        alpha, beta = production_metrics.alpha_beta(returns, bench_returns)
        assert mine.total_return == pytest.approx(
            production_metrics.total_return(curve))
        assert mine.ending_capital == pytest.approx(curve[-1])
        assert mine.max_drawdown == pytest.approx(
            production_metrics.max_drawdown(curve))
        assert mine.sharpe == pytest.approx(
            production_metrics.sharpe_ratio(returns))
        assert mine.sortino == pytest.approx(
            production_metrics.sortino_ratio(returns))
        assert mine.beta == pytest.approx(beta)
        assert mine.alpha == pytest.approx(alpha)
        assert mine.benchmark_return == pytest.approx(
            production_metrics.total_return(benchmark))


def _env_example() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def _dep_names(specs: list[str]) -> set[str]:
    return {re.split(r"[\[<>=!~ ;]", s, maxsplit=1)[0].lower() for s in specs}


def _health_alert_snippet() -> str:
    src = (ROOT / "scripts" / "health.sh").read_text()
    after = src.split('curl -sS "$prom/api/v1/alerts" | python3 -c \'', 1)[1]
    return after.split("\n'\n", 1)[0]


def _fake_docker(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text('#!/bin/sh\necho "$@"\n')
    docker.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}


def _logs_cleanup(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(ROOT / "infra" / "scheduler" / "logs-cleanup.sh")], env={
            **os.environ, "LOGS_DIR": str(tmp_path / "logs")
        }, capture_output=True, text=True)


_HISTORY = re.compile(
    r"\bround\s?\d|real gap(?: found|:)|real fix|real bug|used to accept|"
    r"earlier version of this|confirmed 20\d\d|found 20\d\d|added 20\d\d",
    re.IGNORECASE)


def _non_docstring_strings(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if (isinstance(
                node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.body):
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value,
                                                          ast.Constant):
                docstrings.add(id(first.value))
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings]


class TestRepositoryConfiguration:

    def test_dependencies_env_example_gitignore_and_clean_target(self) -> None:
        data = tomllib.loads((ROOT / "pyproject.toml").read_text())
        runtime = _dep_names(data["project"]["dependencies"])
        dev = _dep_names(data["dependency-groups"]["dev"])
        removed = {
            "langchain", "langchain-openai", "langchain-anthropic",
            "langchain-mistralai", "langchain-google-genai", "authlib",
            "orjson", "python-dotenv", "pytest-cov", "pre-commit"
        }
        assert not (runtime | dev) & removed
        assert "langgraph" in runtime
        assert (
            ".pre-commit-config" not in (ROOT / "pyproject.toml").read_text())

        lock = tomllib.loads((ROOT / "uv.lock").read_text())
        root = next(p for p in lock["package"] if p["name"] == "alphamind")
        locked = {d["name"] for d in root.get("dependencies", [])}
        locked_dev = {
            d["name"]
            for d in root.get("dev-dependencies", {}).get("dev", [])
        }
        for name in ("langchain", "authlib", "pytest-cov", "pre-commit"):
            assert name not in locked | locked_dev
        assert "langgraph" in locked
        names = {p["name"] for p in lock["package"]}
        assert "langchain-core" in names

        env = _env_example()
        for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
                    "MISTRAL_API_KEY"):
            assert env[key] == ""
        assert env["QDRANT_API_KEY"] == ""

        lines = (ROOT / ".gitignore").read_text().splitlines()
        entries = [ln for ln in lines if ln and not ln.startswith("#")]
        assert "/runs/" in entries
        for keep in (".env", ".env.*", "!.env.example", "backups/", "/logs/"):
            assert keep in entries
        assert len(entries) == len(set(entries))

        ignore = [
            ln for ln in (ROOT / ".dockerignore").read_text().splitlines()
            if ln and not ln.startswith("#")
        ]
        assert ignore[0] == "*"
        allowed = {ln[1:] for ln in ignore if ln.startswith("!")}
        for key in ("**/.env", "**/.env.*", "**/.venv"):
            assert key in ignore
        dockerfiles = [
            *ROOT.glob("services/*/Dockerfile"),
            ROOT / "infra" / "scheduler" / "Dockerfile"
        ]
        for dockerfile in dockerfiles:
            for line in dockerfile.read_text().splitlines():
                parts = line.split()
                if not parts or parts[0] != "COPY":
                    continue
                if any(p.startswith("--from") for p in parts[1:]):
                    continue
                for source in [p for p in parts[1:-1]
                               if not p.startswith("--")]:
                    path = source.rstrip("/").rstrip("*")
                    assert any(
                        path == a or path.startswith(a + "/")
                        for a in allowed), (dockerfile, source)
        makefile = (ROOT / "Makefile").read_text()
        clean = makefile.split("\nclean:\n", 1)[1].split("\n\n", 1)[0]
        assert '".mypy_cache"' in clean

    def test_files_keep_required_text_and_vector_keeps_compose_only(self):
        cases = {
            "heartbeat-names-the-scheduled-job-label": ((
                "infra", "scheduler", "heartbeat.sh"), [
                    "alphamind_job_last_success_timestamp_seconds"
                    "{scheduled_job}"
                ], ["alphamind_job_last_success_timestamp_seconds{job}"]),
            "run-analysis-uses-lib-and-runs-folder": (
                ("scripts", "run_analysis.sh"),
                ['. "$(dirname "$0")/lib.sh"',
                 'OUTDIR="runs/run_'], ["COMPOSE=("]),
            "ai-image-creates-no-avatar-folder": ((
                "services", "ai-service", "Dockerfile"), [],
                                                  ["avatars", "/app/data"]),
            "scheduler-image-has-the-s3-copy-tools": ((
                "infra", "scheduler", "Dockerfile"), [
                    " rclone",
                    "COPY infra/scheduler/s3-remote.sh /scripts/s3-remote.sh"
                ], []),
            "cron-jobs-get-the-s3-settings": (
                ("infra", "scheduler", "entrypoint.sh"),
                ["S3_ENDPOINT_URL S3_ACCESS_KEY S3_SECRET_KEY S3_REGION"
                 ], ["OBJECTSTORE_DATA_DIR"]),
            "cron-jobs-reach-auth-service": (
                ("infra", "scheduler", "entrypoint.sh"),
                ["export AUTH_SERVICE_URL='${AUTH_SERVICE_URL:-http://auth"
                 ], []),
            "logs-cleanup-reports-in-the-container": (
                ("infra", "scheduler", "logs-cleanup.sh"),
                ["        /scripts/heartbeat.sh logs-cleanup\n"], [])
        }
        for case_id, (parts, required, forbidden) in cases.items():
            text = ROOT.joinpath(*parts).read_text()
            for snippet in required:
                assert snippet in text, case_id
            for snippet in forbidden:
                assert snippet not in text, case_id

        config = tomllib.loads(
            (ROOT / "infra" / "vector" / "vector.toml").read_text())
        source = config["transforms"]["prepare"]["source"]
        assert config["transforms"]["prepare"]["drop_on_abort"] is True
        assert "container_name" not in source
        label = 'string(.label."com.docker.compose.service") ?? ""'
        assert f"service = {label}" in source
        assert 'if service == "" {\n  abort\n}' in source
        assert source.index("abort") < source.index("replace(service")


class TestOperatorShellScripts:

    def test_health_alerts_no_label_and_probe_options(self, tmp_path) -> None:
        alerts = {
            "data": {
                "alerts": [{
                    "state": "firing",
                    "labels": {
                        "alertname": "DailyJobStale",
                        "job": "core-service",
                        "scheduled_job": "backup"
                    }
                }, {
                    "state": "firing",
                    "labels": {
                        "alertname": "ServiceDown",
                        "job": "ai-service"
                    }
                }]
            }
        }
        out = subprocess.run(
            ["python3", "-c", _health_alert_snippet()],
            input=json.dumps(alerts), capture_output=True, text=True,
            check=True).stdout.splitlines()
        assert out == ["DailyJobStale  backup", "ServiceDown  ai-service"]

        text = (ROOT / "scripts" / "health.sh").read_text()
        assert not re.search(r"round\d", text, re.IGNORECASE)

        cases = {
            "default-model": ([], ""),
            "health-flag": (["--health"], " --health"),
            "model-and-per-key": (["gemini-3.6-flash", "--per-key", "1"],
                                  " --model gemini-3.6-flash --per-key 1"),
            "model-and-health": (["gemini-3.6-flash", "--health"],
                                 " --model gemini-3.6-flash --health")
        }
        env = _fake_docker(tmp_path)
        for case_id, (args, tail) in cases.items():
            probe = subprocess.run(
                ["bash", str(ROOT / "scripts" / "probe.sh"), *args], env=env,
                capture_output=True, text=True, check=True).stdout.strip()
            assert probe.endswith(
                "python -m scripts.probe_model_latency" + tail), case_id

    @pytest.mark.skipif(
        Path("/scripts/heartbeat.sh").exists(),
        reason="runs where the container's heartbeat script is absent")
    def test_logs_cleanup_on_the_host_skips_the_missing_heartbeat(
            self, tmp_path):
        (tmp_path / "logs" / "core-service").mkdir(parents=True)
        old = tmp_path / "logs" / "core-service" / "2026-01-01.log"
        old.write_text("x")
        os.utime(old, (0, 0))
        result = _logs_cleanup(tmp_path)
        assert result.returncode == 0, result.stderr
        assert not old.exists()
        assert "success not reported" in result.stdout
        result = _logs_cleanup(tmp_path / "nowhere")
        assert result.returncode == 0, result.stderr
        assert "success not reported" in result.stdout


class TestPromptStringHygiene:

    def test_reflection_rules_and_no_development_history(self):
        from ai.agents.debate_common import FREE_CASH_FLOW_RULE, OVERLAP_RULE
        from ai.agents.reflection import _SYSTEM_PROMPT

        assert OVERLAP_RULE in _SYSTEM_PROMPT
        assert FREE_CASH_FLOW_RULE in _SYSTEM_PROMPT

        found = [
            f"{path.relative_to(ROOT)}:{line}: {text[:80]!r}"
            for path in sorted(AI_DIR.rglob("*.py"))
            for line, text in _non_docstring_strings(path)
            if _HISTORY.search(text)
        ]
        assert found == []


_FAKE_DEPLOY_DOCKER = """#!/bin/sh
echo "docker $*" >> "$FAKE_LOG"
case "$*" in
  *" ps -q "*) [ -n "$FAKE_RUNNING" ] && echo 0123abcd ;;
  *" config --services"*) printf '%s' "$FAKE_SERVICES" ;;
esac
exit 0
"""

_FAKE_DEPLOY_GIT = """#!/bin/sh
echo "git $*" >> "$FAKE_LOG"
case "$1" in
  merge-base) exit "$FAKE_ANCESTOR_RC" ;;
  log) echo "abc1234 subject" ;;
esac
exit 0
"""


def _run_deploy(tmp_path: Path, *, running: bool = False,
                services: str = "gateway\npostgres\nscheduler\n",
                ssh_command: str | None = None,
                ancestor: bool = True) -> tuple[int, list[str]]:
    # A copy of the script in a fake checkout, so `cd ..` lands there and
    # the required files are test placeholders; docker and git only log.
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "scripts" / "deploy.sh").write_text(
        (ROOT / "scripts" / "deploy.sh").read_text())
    (checkout / ".env.docker").write_text("")
    certs = checkout / "infra" / "gateway" / "certs"
    certs.mkdir(parents=True)
    (certs / "server.crt").write_text("")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", _FAKE_DEPLOY_DOCKER),
                       ("git", _FAKE_DEPLOY_GIT)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "calls.log"
    env = {
        k: v
        for k, v in os.environ.items() if k != "SSH_ORIGINAL_COMMAND"
    }
    env.update(PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_LOG=str(log),
               FAKE_RUNNING="1" if running else "", FAKE_SERVICES=services,
               FAKE_ANCESTOR_RC="0" if ancestor else "1")
    if ssh_command is not None:
        env["SSH_ORIGINAL_COMMAND"] = ssh_command
    result = subprocess.run(
        ["bash", str(checkout / "scripts" / "deploy.sh")], env=env,
        capture_output=True, text=True)
    calls = log.read_text().splitlines() if log.exists() else []
    return result.returncode, calls


def test_deploy_script_commit_choice_and_scheduler_state(tmp_path) -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    compose = "docker compose --env-file .env.docker"

    rc, calls = _run_deploy(tmp_path / "running", running=True)
    assert rc == 0
    assert "git merge --ff-only --quiet origin/main" in calls
    ups = [c for c in calls if f"{compose} up " in c]
    assert ups == [f"{compose} up -d --build --remove-orphans --wait "
                   "--wait-timeout 300"]
    assert not any("stop scheduler" in c for c in calls)

    rc, calls = _run_deploy(tmp_path / "stopped", ssh_command=sha)
    assert rc == 0
    assert f"git merge-base --is-ancestor {sha} origin/main" in calls
    assert f"git merge --ff-only --quiet {sha}" in calls
    ups = [i for i, c in enumerate(calls) if f"{compose} up " in c]
    assert len(ups) == 1
    assert calls[ups[0]].endswith("--wait-timeout 300 gateway postgres")
    assert calls.index(f"{compose} build scheduler") < ups[0]
    assert calls.index(f"{compose} stop scheduler") > ups[0]

    for case_id, kwargs in {
            "no-services": {"services": ""},
            "only-scheduler": {"services": "scheduler\n"},
            "not-a-sha": {"ssh_command": "main; rm -rf /"},
            "short-sha": {"ssh_command": sha[:12]},
            "not-on-main": {"ssh_command": sha, "ancestor": False},
    }.items():
        rc, calls = _run_deploy(tmp_path / case_id, **kwargs)
        assert rc == 1, case_id
        assert not any(f"{compose} up " in c for c in calls), case_id
        if "ssh_command" in kwargs:
            assert not any("git merge --ff" in c for c in calls), case_id
        if case_id in ("not-a-sha", "short-sha"):
            assert not any(c.startswith("git ") for c in calls), case_id
