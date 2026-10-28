import argparse
import asyncio
import contextlib
import json
import logging
import math
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, TextIO
from uuid import UUID


SAMPLE_CHARS = 220


def flatten(text: object, limit: int = 300) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit - 3] + "..."


def one_line(text: str | None, limit: int = SAMPLE_CHARS) -> str:
    if not text:
        return "(empty)"
    return repr(flatten(text, limit))


OK = "OK"
FAIL = "FAIL"
SKIP = "SKIP"

DEFAULT_TIMEOUT_SECONDS = 60.0
SMOKE_STRATEGY_NAME = "smoke_check"

_BASE_URL = os.environ.get("AI_INTERNAL_BASE_URL", "http://localhost:8000")
_HTTP_TIMEOUT_SECONDS = 20.0


class SkipCheck(Exception):
    pass


class CheckFailed(Exception):
    pass


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str
    seconds: float = 0.0


@dataclass
class SmokeContext:
    with_llm: bool = False
    memo: dict[str, Any] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def once(
            self, key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        async with self.lock:
            if key not in self.memo:
                self.memo[key] = await factory()
            return self.memo[key]


CheckFunc = Callable[[SmokeContext], Awaitable[str]]


@dataclass(frozen=True)
class Check:
    name: str
    func: CheckFunc
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    needs_llm: bool = False


async def run_one(check: Check, ctx: SmokeContext) -> CheckResult:
    started = time.monotonic()
    if check.needs_llm and not ctx.with_llm:
        return CheckResult(
            check.name, SKIP, "makes LLM calls; pass --with-llm")
    try:
        detail = await asyncio.wait_for(check.func(ctx), timeout=check.timeout)
        status = OK
    except SkipCheck as exc:
        status, detail = SKIP, str(exc)
    except TimeoutError:
        status, detail = FAIL, f"timed out after {check.timeout:g}s"
    except CheckFailed as exc:
        status, detail = FAIL, str(exc)
    except Exception as exc:
        status, detail = FAIL, f"{type(exc).__name__}: {exc}"
    return CheckResult(
        check.name, status, flatten(detail),
        time.monotonic() - started)


def format_line(result: CheckResult, name_width: int = 0) -> str:
    return (
        f"{result.status:<4} {result.name:<{name_width}}  {result.detail}"
    ).rstrip()


def summarize(results: Sequence[CheckResult], seconds: float) -> str:
    counts = dict.fromkeys((OK, FAIL, SKIP), 0)
    for result in results:
        counts[result.status] += 1
    line = (
        f"SUMMARY ok={counts[OK]} fail={counts[FAIL]} skip={counts[SKIP]} "
        f"total={len(results)} in {seconds:.1f}s")
    failed = [r.name for r in results if r.status == FAIL]
    if failed:
        line += " -- failed: " + ", ".join(failed)
    return line


def exit_code(results: Sequence[CheckResult]) -> int:
    return 1 if any(r.status == FAIL for r in results) else 0


def select_checks(checks: Sequence[Check],
                  only: Sequence[str] | None) -> list[Check]:
    if not only:
        return list(checks)
    known = {check.name for check in checks}
    unknown = [name for name in only if name not in known]
    if unknown:
        raise ValueError(
            f"unknown check(s): {', '.join(unknown)}; known: "
            f"{', '.join(sorted(known))}")
    wanted = set(only)
    return [check for check in checks if check.name in wanted]


async def run_checks(checks: Sequence[Check], ctx: SmokeContext,
                     out: TextIO) -> list[CheckResult]:
    width = max((len(check.name) for check in checks), default=0)
    results: list[CheckResult] = []
    for check in checks:
        result = await run_one(check, ctx)
        results.append(result)
        print(format_line(result, width), file=out, flush=True)
    return results


async def _latest_analysis(ctx: SmokeContext) -> tuple[UUID, str] | None:

    async def load() -> tuple[UUID, str] | None:
        from database import SessionLocal
        from sqlalchemy import select
        from domains.analysis.model import Analysis

        async with SessionLocal() as session:
            row = (
                await session.execute(
                    select(Analysis.id, Analysis.ticker).order_by(
                        Analysis.created_at.desc()).limit(1))).first()
        return (row[0], row[1]) if row else None

    result: tuple[UUID, str] | None = await ctx.once("latest_analysis", load)
    return result


async def _ensure_bm25(ctx: SmokeContext) -> int:

    async def build() -> int:
        from ai.rag.indexing import rebuild_bm25_from_qdrant

        return await rebuild_bm25_from_qdrant()

    count: int = await ctx.once("bm25_count", build)
    return count


async def _filing_ticker(ctx: SmokeContext) -> str:

    async def find() -> str | None:
        from config import settings
        from qdrant import get_qdrant_client
        from qdrant_client.http import models as qmodels

        client = get_qdrant_client()
        counts: dict[str, int] = {}
        offset = None
        while True:
            points, offset = await client.scroll(
                collection_name=settings.QDRANT_COLLECTION_RESEARCH,
                scroll_filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="category", match=qmodels.MatchValue(
                                value="filing"))
                    ]), limit=256, offset=offset, with_payload=["ticker"],
                with_vectors=False)
            for point in points:
                ticker = (point.payload or {}).get("ticker")
                if ticker:
                    counts[str(ticker)] = counts.get(str(ticker), 0) + 1
            if offset is None:
                break
        if not counts:
            return None
        if "AAPL" in counts:
            return "AAPL"
        return max(counts, key=lambda t: (counts[t], t))

    ticker: str | None = await ctx.once("filing_ticker", find)
    if ticker is None:
        raise SkipCheck("no filing chunks indexed in Qdrant")
    return ticker


def _http(method: str, path: str,
          body: dict[str, Any] | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if path.startswith("/internal/"):
        from config import settings

        if settings.INTERNAL_API_TOKEN:
            headers["X-Internal-Token"] = settings.INTERNAL_API_TOKEN
    request = urllib.request.Request(
        f"{_BASE_URL}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request,
                                    timeout=_HTTP_TIMEOUT_SECONDS) as resp:
            return int(resp.status), resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode(errors="replace")


async def check_db(ctx: SmokeContext) -> str:
    from config import settings
    from database import SessionLocal
    from sqlalchemy import func, select, text
    from domains.analysis.model import Analysis

    async with SessionLocal() as session:
        one = (await session.execute(text("SELECT 1"))).scalar_one()
        if one != 1:
            raise CheckFailed(f"SELECT 1 returned {one!r}")
        rows = (
            await session.execute(
                select(Analysis.status,
                       func.count()).group_by(Analysis.status))).all()
    by_status = ", ".join(
        f"{status}={count}" for status, count in sorted(rows))
    return f"schema={settings.POSTGRES_SCHEMA} analyses: {by_status or 'none'}"


async def check_redis(ctx: SmokeContext) -> str:
    from config import settings
    from shared.redis import get_cache_client

    pong = await get_cache_client(settings).ping()
    if not pong:
        raise CheckFailed(f"PING returned {pong!r}")
    return "PING ok"


async def check_qdrant(ctx: SmokeContext) -> str:
    from config import settings
    from qdrant import get_qdrant_client

    client = get_qdrant_client()
    name = settings.QDRANT_COLLECTION_RESEARCH
    if not await client.collection_exists(collection_name=name):
        raise CheckFailed(f"collection {name!r} does not exist")
    info = await client.get_collection(collection_name=name)
    size = getattr(info.config.params.vectors, "size", None)
    if size != settings.QDRANT_VECTOR_SIZE:
        raise CheckFailed(
            f"collection {name!r} vector size {size} != "
            f"QDRANT_VECTOR_SIZE {settings.QDRANT_VECTOR_SIZE}")
    return f"collection={name} size={size} points={info.points_count}"


async def check_bm25(ctx: SmokeContext) -> str:
    from ai.rag.indexing import get_bm25_index

    count = await _ensure_bm25(ctx)
    if count == 0:
        raise CheckFailed("rebuilt BM25 index from Qdrant with 0 chunks")
    ticker = await _filing_ticker(ctx)
    hits = get_bm25_index().search(
        "revenue risk factors", top_k=5, category="filing", ticker=ticker)
    if not hits:
        raise CheckFailed(
            f"{count} chunks indexed but 0 filing hits for {ticker}")
    return f"chunks={count} filing hits for {ticker}={len(hits)}"


async def check_retrieval(ctx: SmokeContext) -> str:
    from ai.rag.retriever import FilingRetriever

    await _ensure_bm25(ctx)
    ticker = await _filing_ticker(ctx)
    retriever = FilingRetriever(use_query_expansion=False, visibility=None)
    chunks = await retriever.retrieve(
        "revenue and risk factors", ticker=ticker, sources=frozenset({"bm25"}))
    if not chunks:
        raise CheckFailed(f"FilingRetriever returned 0 chunks for {ticker}")
    wrong = [c.chunk_id for c in chunks if c.ticker != ticker]
    if wrong:
        raise CheckFailed(f"{len(wrong)} chunk(s) outside ticker {ticker}")
    newest = await retriever.newest_filing_chunks("revenue", ticker)
    newest_date = (newest[0].content_date or "?")[:10] if newest else "none"
    return f"{ticker}: {len(chunks)} chunks; newest filing {newest_date}"


async def check_kg(ctx: SmokeContext) -> str:
    from database import SessionLocal
    from sqlalchemy import func, select
    from domains.knowledge.model import KgEntity
    from domains.knowledge.repository import KnowledgeGraphRepository

    latest = await _latest_analysis(ctx)
    async with SessionLocal() as session:
        total = (
            await session.execute(select(func.count()).select_from(KgEntity)
                                  )).scalar_one()
        if total == 0:
            raise SkipCheck("kg_entities is empty")
        name: str | None = None
        if latest is not None:
            found = (
                await session.execute(
                    select(KgEntity.name).where(
                        KgEntity.normalized_name ==
                        latest[1].strip().upper()).limit(1)
                )).scalar_one_or_none()
            name = found
        if name is None:
            name = (
                await session.execute(
                    select(KgEntity.name).order_by(
                        KgEntity.created_at.desc()).limit(1))).scalar_one()
        relations = await KnowledgeGraphRepository(
            session).find_relations_for_entity_name(name, limit=20)
    return (
        f"entities={total}; relations for {name!r}={len(relations)} (limit 20)"
    )


async def check_filing_records(ctx: SmokeContext) -> str:
    from scripts.backfill_filing_records import filings_without_records

    missing = await filings_without_records()
    if missing:
        shown = ", ".join(missing[:5])
        more = f" and {len(missing) - 5} more" if len(missing) > 5 else ""
        raise CheckFailed(
            f"{len(missing)} filing document(s) without records: {shown}"
            f"{more}; run python -m scripts.backfill_filing_records "
            "--filename <file> --apply")
    return "every filing document has its records row"


async def check_market_data(ctx: SmokeContext) -> str:
    from ai.tools.market_data_cache import get_bars_cache_first
    from database import SessionLocal
    from domains.market.service import MarketService

    ticker = "AAPL"
    async with SessionLocal() as session:
        service = MarketService(session)
        written = await service.refresh_from_external_provider(
            ticker, lookback_days=10)
        await session.commit()
        bars = await service.get_recent_prices(ticker)
    if written == 0 or not bars:
        raise CheckFailed(f"refresh wrote {written} bars, {len(bars)} cached")
    latest = bars[0]
    latest_close = float(latest.close)
    if not math.isfinite(latest_close) or latest_close <= 0:
        raise CheckFailed(
            f"latest close {latest_close!r} is not a valid price")

    lookback = 90
    window = await get_bars_cache_first(
        ticker, interval="1d", lookback_days=lookback)
    problem = window_coverage_problem([bar.timestamp for bar in window],
                                      datetime.now(UTC), lookback)
    if problem:
        raise CheckFailed(f"get_bars_cache_first({lookback}d): {problem}")
    oldest = min(bar.timestamp for bar in window).date()
    return (
        f"{ticker} refresh wrote {written}, cached={len(bars)}, "
        f"close {latest.timestamp.date()}={latest_close:.2f}; "
        f"{lookback}d window {len(window)} bars from {oldest}")


def window_coverage_problem(
        timestamps: Sequence[datetime], now: datetime,
        lookback_days: int) -> str | None:
    if not timestamps:
        return "no bars"
    stamps = [ts if ts.tzinfo else ts.replace(tzinfo=UTC) for ts in timestamps]
    start = now - timedelta(days=lookback_days)
    slack = timedelta(days=5)
    oldest, newest = min(stamps), max(stamps)
    if oldest < start - timedelta(days=1):
        return (
            f"oldest bar {oldest.date()} is before the window start "
            f"{start.date()}")
    if oldest > start + slack:
        return (
            f"oldest bar {oldest.date()} leaves the window start "
            f"{start.date()} uncovered")
    if newest < now - slack:
        return f"newest bar {newest.date()} is stale"
    return None


async def check_fx(ctx: SmokeContext) -> str:
    from ai.providers.registry import get_market_data_provider

    quote = await get_market_data_provider().get_current_price("USDEUR=X")
    rate = float(quote.price)
    if not math.isfinite(rate) or not 0.3 < rate < 3.0:
        raise CheckFailed(f"USD->EUR rate {rate!r} is implausible")
    return f"USD->EUR {rate:.4f}"


async def check_fred(ctx: SmokeContext) -> str:
    from ai.providers.fred import FredProvider
    from config import settings

    if not settings.FRED_API_KEY:
        raise SkipCheck("FRED_API_KEY is not set")
    provider = FredProvider()
    latest: list[str] = []
    for series, lookback_days in (("fed_funds_rate", 90), ("DGS10", 30)):
        points = await provider.get_indicator(
            series, lookback_days=lookback_days)
        if not points:
            raise CheckFailed(
                f"no {series} observations in {lookback_days} days")
        last = points[-1]
        latest.append(f"{series} {last.value} ({last.observed_at})")
    return "; ".join(latest)


async def check_core_service(ctx: SmokeContext) -> str:
    from clients import core_client
    from database import SessionLocal
    from sqlalchemy import select
    from domains.analysis.model import Analysis

    tickers = await core_client.get_tracked_tickers()
    async with SessionLocal() as session:
        portfolio_id = (
            await session.execute(
                select(Analysis.portfolio_id).where(
                    Analysis.portfolio_id.is_not(None)).order_by(
                        Analysis.created_at.desc()).limit(1)
            )).scalar_one_or_none()
    if portfolio_id is None:
        return (
            f"tracked tickers={len(tickers)}; no analysis with a portfolio_id")
    pid = str(portfolio_id)
    owner = await core_client.get_portfolio_owner(pid)
    if owner is None:
        raise CheckFailed(f"core-service says portfolio {pid} does not exist")
    holdings = await core_client.get_portfolio_holdings(pid)
    cash = await core_client.get_portfolio_cash_balance(pid)
    return (
        f"tracked tickers={len(tickers)}; portfolio {pid}: "
        f"visibility={owner.get('visibility')} holdings={len(holdings)} "
        f"cash={cash:.2f}")


async def check_internal_api(ctx: SmokeContext) -> str:
    status, body = await asyncio.to_thread(_http, "GET", "/health")
    if status != 200:
        raise CheckFailed(f"GET /health -> {status}")
    health = json.loads(body)
    down = [key for key, value in health.items() if value is False]
    if down:
        raise CheckFailed(f"GET /health reports down: {', '.join(down)}")

    status, body = await asyncio.to_thread(
        _http, "POST", "/api/v1/analyses", {"ticker": ""})
    if 200 <= status < 300:
        created = ""
        try:
            created = str(json.loads(body)["data"]["id"])
        except (ValueError, KeyError, TypeError):
            created = body[:200]
        raise CheckFailed(
            f"!!! POST /api/v1/analyses WITHOUT a token returned {status} and "
            f"created analysis {created} -- delete it "
            f"(scripts/delete_analyses.py) and fix auth")
    if status != 401:
        raise CheckFailed(
            f"POST /api/v1/analyses without token -> {status}, want 401")

    latest = await _latest_analysis(ctx)
    if latest is None:
        return "health ok; create without token 401; no analysis to read"
    analysis_id = latest[0]
    status, _ = await asyncio.to_thread(
        _http, "GET", f"/api/v1/analyses/{analysis_id}")
    if status != 401:
        raise CheckFailed(
            f"GET /api/v1/analyses/<id> without token -> {status}, want 401")
    status, body = await asyncio.to_thread(
        _http, "GET", f"/internal/analyses/{analysis_id}")
    if status != 200:
        raise CheckFailed(f"GET /internal/analyses/{analysis_id} -> {status}")
    ref_status = json.loads(body).get("status")
    if not ref_status:
        raise CheckFailed(
            f"GET /internal/analyses/{analysis_id} has no status")
    return (
        f"health ok; public create/get without token 401; "
        f"internal {analysis_id} status={ref_status}")


async def check_backtest_metrics(ctx: SmokeContext) -> str:
    from ai.tools import indicators as metrics

    prices = [100.0, 110.0, 99.0, 108.9, 120.0]
    returns = metrics.simple_returns(prices)
    expected_returns = [0.10, -0.10, 0.10, 120.0 / 108.9 - 1]
    checks: list[tuple[str, bool]] = [
        (
            "simple_returns",
            all(
                math.isclose(a, b, rel_tol=1e-9)
                for a, b in zip(returns, expected_returns, strict=True))),
        (
            "total_return",
            math.isclose(metrics.total_return(prices) or 0.0, 0.20)),
        (
            "max_drawdown",
            math.isclose(metrics.max_drawdown(prices) or 0.0, 0.10)),
        ("win_rate", metrics.win_rate([0.1, -0.2, 0.3, 0.0]) == 0.5),
        ("sharpe>0", (metrics.sharpe_ratio(returns) or 0.0) > 0),
        ("sortino>0", (metrics.sortino_ratio(returns) or 0.0) > 0),
        ("sharpe(short)=None", metrics.sharpe_ratio([0.1]) is None)
    ]
    alpha, beta = metrics.alpha_beta(returns, returns)
    checks.append(
        ("beta(self)=1", beta is not None and math.isclose(beta, 1.0)))
    checks.append((
        "alpha(self)=0", alpha is not None
        and math.isclose(alpha, 0.0, abs_tol=1e-9)))
    checks.append((
        "tracking_error(self)=0", metrics.tracking_error(returns,
                                                         returns) == 0))
    bad = [name for name, passed in checks if not passed]
    if bad:
        raise CheckFailed("wrong: " + ", ".join(bad))
    return f"{len(checks)} metric assertions passed"


async def check_backtest_replay(ctx: SmokeContext) -> str:
    from ai.workflows.backtest_workflow import (
        run_backtest_workflow, run_recommendation_replay_workflow)
    from database import SessionLocal
    from sqlalchemy import select
    from domains.analysis.model import (Analysis, Recommendation,
                                        RecommendationTier)
    from domains.backtest.model import Backtest, Benchmark
    from domains.backtest.repository import BacktestRepository
    from shared.enums import TierStatus

    end = datetime.now(UTC).date()
    start = end - timedelta(days=60)
    async with SessionLocal() as session:
        recent = (
            await session.execute(
                select(Analysis.ticker).join(
                    Recommendation,
                    Recommendation.analysis_id == Analysis.id).join(
                        RecommendationTier,
                        RecommendationTier.recommendation_id ==
                        Recommendation.id).
                where(
                    RecommendationTier.status == TierStatus.TRIGGERED.value,
                    RecommendationTier.triggered_at >= datetime.combine(
                        start, datetime.min.time(), tzinfo=UTC)).order_by(
                            RecommendationTier.triggered_at.desc()).limit(1)
            )).scalar_one_or_none()
        any_triggered = (
            await session.execute(
                select(RecommendationTier.id).where(
                    RecommendationTier.status ==
                    TierStatus.TRIGGERED.value).limit(1)
            )).scalar_one_or_none()
        benchmark_existed = (
            await session.execute(
                select(Benchmark.id).where(Benchmark.symbol == "SPY")
            )).scalar_one_or_none() is not None
    if any_triggered is None:
        raise SkipCheck(
            "no triggered recommendation tier exists; nothing to replay")

    backtest_id: UUID | None = None
    try:
        async with SessionLocal() as session:
            backtest = await BacktestRepository(session).create(
                user_id=None, strategy_name=SMOKE_STRATEGY_NAME,
                benchmark_symbol="SPY", start_date=start, end_date=end,
                initial_capital=100_000.0)
            backtest_id = backtest.id
            await session.commit()
        if recent is not None:
            mode = f"single-ticker {recent}"
            await run_backtest_workflow(str(backtest_id), ticker=recent)
        else:
            mode = "recommendation replay"
            await run_recommendation_replay_workflow(str(backtest_id))
        async with SessionLocal() as session:
            done = (
                await session.execute(
                    select(Backtest.status, Backtest.strategy_return).where(
                        Backtest.id == backtest_id))).one()
        if done[0] != "completed":
            raise CheckFailed(
                f"{mode} {start}..{end} ended with status={done[0]}")
        strategy_return = (
            "n/a" if done[1] is None else f"{float(done[1]):+.4f}")
        return (
            f"{mode} {start}..{end} completed, "
            f"strategy_return={strategy_return}")
    finally:
        if backtest_id is not None:
            await _delete_scratch_backtest(
                backtest_id, drop_spy=not benchmark_existed)


async def _delete_scratch_backtest(
        backtest_id: UUID, *, drop_spy: bool) -> None:
    from database import SessionLocal
    from sqlalchemy import delete, select
    from domains.backtest.model import (Backtest, BacktestMetrics, Benchmark,
                                        BenchmarkResult)

    async with SessionLocal() as session:
        children = (BenchmarkResult, BacktestMetrics)
        for child in children:
            await session.execute(
                delete(child).where(child.backtest_id == backtest_id))
        await session.execute(
            delete(Backtest).where(
                Backtest.id == backtest_id, Backtest.user_id.is_(None),
                Backtest.strategy_name == SMOKE_STRATEGY_NAME))
        if drop_spy:
            still_used = (
                await session.execute(
                    select(BenchmarkResult.id).join(
                        Benchmark,
                        Benchmark.id == BenchmarkResult.benchmark_id).where(
                            Benchmark.symbol == "SPY").limit(1)
                )).scalar_one_or_none()
            if still_used is None:
                await session.execute(
                    delete(Benchmark).where(Benchmark.symbol == "SPY"))
        await session.commit()


async def check_tier_monitoring_dry(ctx: SmokeContext) -> str:
    from database import SessionLocal
    from sqlalchemy import func, select
    from domains.analysis.model import RecommendationTier
    from shared.enums import TierStatus

    open_statuses = (
        TierStatus.PENDING_ACCEPTANCE.value,
        TierStatus.ACCEPTED_AWAITING_TRIGGER.value)
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(RecommendationTier.status, func.count()).where(
                    RecommendationTier.status.in_(open_statuses)).group_by(
                        RecommendationTier.status))).all()
    counts: dict[str, int] = {str(row[0]): int(row[1]) for row in rows}
    raise SkipCheck(
        "no side-effect-free trigger evaluation in tier_monitoring_workflow "
        "(_check_one_tier executes trades); open tiers: "
        f"accepted_awaiting_trigger={counts.get(open_statuses[1], 0)} "
        f"pending_acceptance={counts.get(open_statuses[0], 0)}")


async def check_review(ctx: SmokeContext) -> str:
    from ai.workflows.review_workflow import (_directional_return,
                                              _score_recommendation)

    cases: list[tuple[str, bool]] = [
        (
            "sell negates (B25)",
            math.isclose(_directional_return("sell", 0.10), -0.10)),
        ("buy keeps", math.isclose(_directional_return("buy", 0.10), 0.10)),
        (
            "hold keeps",
            math.isclose(_directional_return("hold", -0.05), -0.05)),
        ("buy up correct", _score_recommendation("buy", 0.05) == 1.0),
        ("buy down wrong", _score_recommendation("buy", -0.05) == 0.0),
        ("sell down correct", _score_recommendation("sell", -0.05) == 1.0),
        ("sell up wrong", _score_recommendation("sell", 0.05) == 0.0),
        ("hold flat correct", _score_recommendation("hold", 0.01) == 1.0),
        ("hold moved wrong", _score_recommendation("hold", 0.05) == 0.0)
    ]
    bad = [name for name, passed in cases if not passed]
    if bad:
        raise CheckFailed("wrong: " + ", ".join(bad))
    return f"{len(cases)} scoring assertions passed"


async def check_queue(ctx: SmokeContext) -> str:
    from database import SessionLocal
    from sqlalchemy import func, select
    from domains.analysis.model import Analysis

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(
                    Analysis.status, func.count(),
                    func.min(Analysis.created_at)).where(
                        Analysis.status.in_(
                            ("pending", "running"))).group_by(Analysis.status)
            )).all()
    info = {status: (count, oldest) for status, count, oldest in rows}
    running = info.get("running", (0, None))[0]
    pending, oldest_pending = info.get("pending", (0, None))
    parts = [f"running={running}", f"pending={pending}"]
    if oldest_pending is not None:
        age = datetime.now(UTC) - oldest_pending
        parts.append(f"oldest pending {int(age.total_seconds() // 60)} min")
    if running > 1:
        raise CheckFailed(
            "more than one running analysis (the queue is serial): " +
            " ".join(parts))
    return "DB view (in-process queue not visible here): " + " ".join(parts)


async def check_llm(ctx: SmokeContext) -> str:
    from ai.llm.base import LLMMessage, LLMTier
    from ai.llm.router import LLMRouter
    from ai.rag.embedding import embed_texts
    from ai.rag.retriever import FilingRetriever
    from config import settings

    response = await LLMRouter().complete(
        LLMTier.CHEAP, [LLMMessage(role="user", content="Reply with OK")],
        max_tokens=64, timeout_seconds=30.0)
    if not response.content.strip():
        raise CheckFailed(f"empty completion from {response.model}")
    vectors = await embed_texts(["ping"], purpose="query")
    if len(vectors) != 1 or len(vectors[0]) != settings.QDRANT_VECTOR_SIZE:
        size = len(vectors[0]) if vectors else 0
        raise CheckFailed(
            f"embedding size {size} != QDRANT_VECTOR_SIZE "
            f"{settings.QDRANT_VECTOR_SIZE}")
    vector_detail = ""
    try:
        ticker = await _filing_ticker(ctx)
    except SkipCheck:
        vector_detail = "; no filing ticker for a vector search"
    else:
        chunks = await FilingRetriever(
            use_query_expansion=False, visibility=None).retrieve(
            "ping", ticker=ticker, sources=frozenset({"vector"}),
            query_vector=vectors[0])
        if not chunks:
            raise CheckFailed(
                f"vector filing search for {ticker} returned 0 chunks")
        vector_detail = f"; vector search {ticker}={len(chunks)}"
    return (
        f"completion {response.provider}/{response.model} "
        f"{flatten(response.content, 20)!r}; embedding dim {len(vectors[0])}"
        f"{vector_detail}")


CHECKS: tuple[Check, ...] = (
    Check("db", check_db), Check("redis", check_redis),
    Check("qdrant", check_qdrant), Check("bm25", check_bm25, timeout=120.0),
    Check("retrieval", check_retrieval, timeout=120.0), Check("kg", check_kg),
    Check("filing_records", check_filing_records, timeout=120.0),
    Check("market_data", check_market_data,
          timeout=90.0), Check("fx", check_fx), Check("fred", check_fred),
    Check("core_service",
          check_core_service), Check("internal_api", check_internal_api),
    Check("backtest_metrics", check_backtest_metrics),
    Check("backtest_replay", check_backtest_replay, timeout=180.0),
    Check("tier_monitoring_dry",
          check_tier_monitoring_dry), Check("review", check_review),
    Check("queue",
          check_queue), Check("llm", check_llm, timeout=120.0, needs_llm=True))


def _quiet_logging() -> None:
    import structlog

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))


async def _close_clients() -> None:
    closers: list[Callable[[], Awaitable[Any]]] = []
    if "qdrant" in sys.modules:
        from qdrant import close_qdrant_client

        closers.append(close_qdrant_client)
    if "database" in sys.modules:
        from database import engine

        closers.append(engine.dispose)
    if "shared.redis" in sys.modules:
        from shared.redis import close_redis_connections

        closers.append(close_redis_connections)
    if "shared.http_client" in sys.modules:
        from shared.http_client import close_http_clients

        closers.append(close_http_clients)
    for close in closers:
        with contextlib.suppress(Exception):
            await close()


async def _amain(
        checks: Sequence[Check], ctx: SmokeContext, out: TextIO,
        cleanup: Callable[[], Awaitable[None]]) -> int:
    started = time.monotonic()
    try:
        results = await run_checks(checks, ctx, out)
    finally:
        await cleanup()
    print(summarize(results, time.monotonic() - started), file=out, flush=True)
    return exit_code(results)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="smoke_check", description=(
            "Smoke-check ai-service features a full analysis never runs."))
    parser.add_argument(
        "--only", nargs="+", metavar="NAME",
        help="run only these checks: " + ", ".join(c.name for c in CHECKS))
    parser.add_argument(
        "--with-llm", action="store_true",
        help="also run the llm check (one CHEAP completion, one embedding)")
    return parser.parse_args(argv)


def main(
        argv: Sequence[str] | None = None, *, checks: Sequence[Check] = CHECKS,
        out: TextIO | None = None,
        cleanup: Callable[[], Awaitable[None]] = _close_clients) -> int:
    args = parse_args(argv)
    stream = out if out is not None else sys.stdout
    try:
        selected = select_checks(checks, args.only)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if out is None:
        _quiet_logging()
    ctx = SmokeContext(with_llm=args.with_llm)
    return asyncio.run(_amain(selected, ctx, stream, cleanup))


if __name__ == "__main__":
    sys.exit(main())
