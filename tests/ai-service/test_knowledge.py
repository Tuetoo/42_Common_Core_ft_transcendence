import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4
import ai.graph.nodes as nodes_module
import ai.ingestion.sec_edgar_filings as sec_edgar_filings
import ai.knowledge.archival as archival_module
import ai.knowledge.extraction as extraction_module
import ai.workflows.auto_ingestion_workflow as auto_workflow
import config
import pytest
from ai.ingestion.sec_edgar_filings import NewFiling
from ai.knowledge import filing_summary
from ai.knowledge.kg_context import _tag_superseded_readings
from ai.knowledge.recycled_content import (note_and_source_passages,
                                           prior_note_similarity,
                                           recycled_share,
                                           split_agent_note_passages)
from ai.llm.base import LLMConfigError, LLMError, LLMResponse, LLMUsage
from ai.rag.visibility import ChunkVisibility
from database import SessionLocal
from sqlalchemy import select
from domains.analysis.repository import AnalysisRepository
from domains.knowledge.model import AgentReport, KgEntity, KgRelation
from domains.knowledge.repository import KnowledgeGraphRepository
from domains.rag.model import DocumentChunk, EmbeddingJob
from shared.enums import AgentRole, DocumentCategory


async def _create_analysis(ticker: str = "AMD") -> UUID:
    async with SessionLocal() as session:
        analysis = await AnalysisRepository(session).create(
            user_id=None, portfolio_id=None, ticker=ticker)
        await session.commit()
        return analysis.id


class _ScriptedRouter:

    def __init__(self, response_text: str):
        self._response_text = response_text
        self.calls: list = []

    async def complete(self, tier, messages, **kwargs) -> LLMResponse:
        self.calls.append(messages)
        return LLMResponse(
            content=self._response_text, provider="fake", model="fake-model",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1))


class _FailingRouter:

    async def complete(self, tier, messages, **kwargs):
        raise LLMConfigError("no key configured")


_LONG_REPORT = (
    "The Federal Reserve held its benchmark rate steady at its July meeting, "
    "citing solid economic activity alongside elevated uncertainty tied to "
    "geopolitical tensions. AMD's own results this quarter benefited from "
    "continued strong demand for AI accelerators, though the broader "
    "semiconductor sector remains sensitive to any shift in Fed policy given "
    "the capital intensity of AI infrastructure buildouts across the "
    "industry. Investors should watch the September meeting closely for any "
    "change in tone.")

_ITERATION_CAP_FALLBACK = (
    "Iteration cap (4) reached before the agent volunteered a final "
    "answer. Best-effort summary from the last observation gathered: "
    "Web search results for Federal Reserve policy stance indicated "
    "continued uncertainty about the pace of future rate changes "
    "given mixed economic data across several key indicators.")

_NOT_WELL_SUPPORTED_BAILOUT = (
    "I could not retrieve any current, verifiable macroeconomic "
    "indicators (policy rate, 10y yield, CPI, etc.) or the latest "
    "FOMC narrative to describe the current regime. Without "
    "reliable, up-to-date data, I cannot credibly summarize "
    "interest-rate policy, sector-rotation signals, or the market "
    "regime for this ticker. If you can provide a fresh data feed "
    "or allow a different tool, I can revisit this task "
    "immediately.")


class TestExtractEntitiesRelations:

    @pytest.mark.asyncio
    async def test_unusable_extraction_is_empty_and_missing_type_recovered(
            self):
        unknown_type = json.dumps([{
            "source": "AMD",
            "source_type": "spaceship",
            "relation": "x",
            "target": "Mars",
            "target_type": "company"
        }])
        cases = {
            "unparseable-response": _ScriptedRouter("not json at all"),
            "unknown-entity-type-dropped": _ScriptedRouter(unknown_type),
            "llm-failure-fails-open": _FailingRouter()
        }
        for case_id, router in cases.items():
            triples = await extraction_module.extract_entities_relations(
                _LONG_REPORT, llm_router=router)
            assert triples == [], case_id

        router = _ScriptedRouter(
            json.dumps([{
                "source": "PlasmaSolve",
                "source_type": "company",
                "relation": "acquired_by",
                "target": "Apple",
                "confidence": 0.95
            }]))
        triples = await extraction_module.extract_entities_relations(
            _LONG_REPORT, llm_router=router)
        assert len(triples) == 1
        assert triples[0].target_type == "concept"
        assert triples[0].source == "PlasmaSolve"


class TestArchiveAgentReport:

    @pytest.mark.asyncio
    async def test_skip_rules_for_role_length_and_unreliable_reports(
            self, monkeypatch):
        skipped = {
            "non-archivable-role": (AgentRole.TECHNICAL, _LONG_REPORT),
            "short-content": (AgentRole.MACRO, "Too short to bother with.")
        }
        for role, content in skipped.values():
            await archival_module.archive_agent_report(
                analysis_id=uuid4(), role=role, ticker="AMD", content=content)

        def _should_not_be_called(*args, **kwargs):
            raise AssertionError(
                "should not reach chunk_document for an unreliable report")

        monkeypatch.setattr(
            archival_module, "chunk_document", _should_not_be_called)
        unreliable = {
            "hit-iteration-cap": (
                _ITERATION_CAP_FALLBACK, {
                    "hit_iteration_cap": True
                }),
            "not-well-supported": (
                _NOT_WELL_SUPPORTED_BAILOUT, {
                    "is_well_supported": False
                })
        }
        for case_id, (content, flag) in unreliable.items():
            assert len(content.split()) >= 40, case_id
            await archival_module.archive_agent_report(
                analysis_id=uuid4(), role=AgentRole.MACRO, ticker=None,
                content=content, **flag)

        content = (
            "A genuinely completed report with plenty of real, substantive "
            "content describing actual findings from actual research done "
            "by the agent, not a generic fallback summary of any kind at "
            "all -- covering real market conditions, real analysis, and "
            "real conclusions drawn from real evidence gathered over the "
            "course of a normal, successful agent run that never hit any "
            "kind of iteration limit or fallback path whatsoever.")
        assert len(content.split()) >= 40
        called = {"yes": False}

        def _tracks_call(*args, **kwargs):
            called["yes"] = True
            raise RuntimeError(
                "stop here -- only checking whether this was reached")

        monkeypatch.setattr(archival_module, "chunk_document", _tracks_call)

        with pytest.raises(RuntimeError):
            await archival_module.archive_agent_report(
                analysis_id=uuid4(), role=AgentRole.NEWS, ticker="AMD",
                content=content)

        assert called["yes"], "a normal report must still reach real work"

    @pytest.mark.usefixtures("_schema_ready")
    @pytest.mark.asyncio
    async def test_persists_report_chunks_and_kg_and_survives_kg_failure(
            self, monkeypatch):
        analysis_id = await _create_analysis("AMD")

        async def _fake_embed_texts(texts):
            return [[0.1, 0.2, 0.3] for _ in texts]

        indexed_calls = []

        async def _fake_index_chunks(**kwargs):
            indexed_calls.append(kwargs)

        router = _ScriptedRouter(
            json.dumps([{
                "source": "Federal Reserve",
                "source_type": "institution",
                "relation": "held_rates_steady_at",
                "target": "3.50%-3.75%",
                "target_type": "concept",
                "confidence": 0.9
            }]))

        monkeypatch.setattr(archival_module, "embed_texts", _fake_embed_texts)
        monkeypatch.setattr(
            archival_module, "index_chunks", _fake_index_chunks)
        monkeypatch.setattr(extraction_module, "LLMRouter", lambda: router)

        await archival_module.archive_agent_report(
            analysis_id=analysis_id, role=AgentRole.MACRO, ticker="AMD",
            content=_LONG_REPORT)

        assert len(indexed_calls) == 1
        assert indexed_calls[0]["ticker"] == "AMD"
        assert indexed_calls[0]["source"] == "agent:macro"

        async with SessionLocal() as session:
            report = (
                await session.scalars(
                    select(AgentReport).where(
                        AgentReport.analysis_id == analysis_id))).one()
            assert report.agent_role == "macro"
            assert report.category == "macro_research"
            assert report.content == _LONG_REPORT

            chunks = (
                await session.scalars(
                    select(DocumentChunk).where(
                        DocumentChunk.document_id == report.id))).all()
            assert len(chunks) >= 1

            job = (
                await session.scalars(
                    select(EmbeddingJob).where(
                        EmbeddingJob.document_id == report.id))).one()
            assert job.status == "ready"

            relations = (
                await session.scalars(
                    select(KgRelation).where(
                        KgRelation.source_report_id == report.id))).all()
            assert len(relations) == 1

            entities = (await session.scalars(select(KgEntity))).all()
            entity_names = {e.name for e in entities}
            assert "Federal Reserve" in entity_names

        analysis_id = await _create_analysis("AMD")

        async def _boom(*args, **kwargs):
            raise RuntimeError("kg extraction blew up")

        monkeypatch.setattr(
            archival_module, "extract_entities_relations", _boom)

        await archival_module.archive_agent_report(
            analysis_id=analysis_id, role=AgentRole.RISK, ticker="AMD",
            content=_LONG_REPORT)

        async with SessionLocal() as session:
            report = (
                await session.scalars(
                    select(AgentReport).where(
                        AgentReport.analysis_id == analysis_id))).one()
            assert report.content == _LONG_REPORT

    @pytest.mark.asyncio
    async def test_archive_owner_decides_chunk_visibility(self, _schema_ready):
        user = uuid4()
        async with SessionLocal() as session:
            repo = AnalysisRepository(session)
            mine = await repo.create(
                user_id=user, portfolio_id=None, ticker="AMD")
            system = await repo.create(
                user_id=None, portfolio_id=None, ticker="AMD")
            await session.commit()

        owner = await archival_module._archive_owner(mine.id)
        assert archival_module._archive_visibility(owner) == (
            ChunkVisibility.for_owner(user))
        owner = await archival_module._archive_owner(system.id)
        assert archival_module._archive_visibility(owner) == (
            ChunkVisibility.system())
        owner = await archival_module._archive_owner(uuid4())
        assert archival_module._archive_visibility(owner) == (
            ChunkVisibility())

        async with SessionLocal() as session:
            repo = AnalysisRepository(session)
            await session.delete(await repo.get_by_id(mine.id))
            await session.delete(await repo.get_by_id(system.id))
            await session.commit()


class TestNearDuplicateArchivalSupersede:
    executed: list = []

    class _FakeReport:

        def __init__(
                self, id: str, content: str,
                category: str = "macro_research") -> None:
            self.id = id
            self.content = content
            self.category = category
            self.analysis_id = None

    class _FakeSession:

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def commit(self) -> None:
            pass

        async def execute(self, statement, params=None) -> None:
            TestNearDuplicateArchivalSupersede.executed.append(
                (str(statement), params))

    def _patch_recent_reports(self, monkeypatch, reports: list) -> None:

        class _FakeRepo:

            def __init__(self, session):
                pass

            async def list_recent_by_category(
                    self, category, ticker=None, limit=5):
                return reports[:limit]

        monkeypatch.setattr(
            archival_module, "AgentReportRepository", _FakeRepo)
        monkeypatch.setattr(
            archival_module, "SessionLocal",
            lambda: TestNearDuplicateArchivalSupersede._FakeSession())

    def _patch_embed_texts(self, monkeypatch,
                           vectors: list[list[float]]) -> list[list]:
        calls = []

        async def _fake_embed_texts(texts):
            calls.append(texts)
            return vectors

        monkeypatch.setattr(archival_module, "embed_texts", _fake_embed_texts)
        return calls

    @pytest.mark.asyncio
    async def test_different_content_is_not_flagged_and_old_report_is_kept(
            self, monkeypatch):
        self._patch_recent_reports(
            monkeypatch, [self._FakeReport("r1", "Fed funds rate content")])
        self._patch_embed_texts(
            monkeypatch, [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])

        result = await archival_module._find_near_duplicate_of_recent_report(
            category=DocumentCategory.MACRO_RESEARCH,
            content="Oil prices content", ticker=None)

        assert result is None

        from ai.rag.indexing import DeletionResult

        original = (
            "The Federal Reserve maintained the federal funds rate at 3.63 "
            "percent following the latest FOMC meeting, citing continued "
            "progress on inflation while monitoring labor market conditions "
            "closely. The 10-year Treasury yield stood at 4.63 percent, "
            "reflecting market expectations of a stable rate environment "
            "through the remainder of the year. Economic indicators suggest "
            "moderate growth continuing into the next quarter across most "
            "sectors of the broader economy.")
        assert len(original.split()) >= 40

        class _FakeReportRepo:

            def __init__(self, session):
                pass

            async def list_recent_by_category(
                    self, category, ticker=None, limit=5):
                return [
                    TestNearDuplicateArchivalSupersede._FakeReport(
                        "r1", original)
                ]

            async def delete(self, report_id):
                superseded_report_ids.append(report_id)

        superseded_report_ids: list = []
        monkeypatch.setattr(
            archival_module, "AgentReportRepository", _FakeReportRepo)
        monkeypatch.setattr(
            archival_module, "SessionLocal",
            lambda: TestNearDuplicateArchivalSupersede._FakeSession())
        self._patch_embed_texts(
            monkeypatch, [[0.9, 0.1, 0.0], [0.89, 0.12, 0.0]])

        rag_deletion_calls = []

        async def _fake_delete_document_completely(document_id, session):
            rag_deletion_calls.append(document_id)
            return DeletionResult(
                document_id=document_id, qdrant_bm25_chunks_removed=3)

        class _FakeKgRepo:

            def __init__(self, session):
                pass

            async def delete_relations_for_report(self, report_id):
                return 0

        monkeypatch.setattr(
            archival_module, "delete_document_completely",
            _fake_delete_document_completely)
        monkeypatch.setattr(
            archival_module, "KnowledgeGraphRepository", _FakeKgRepo)

        chunk_document_calls = []

        def _fake_chunk_document(content, category, metadata=None):
            chunk_document_calls.append(content)
            return []

        monkeypatch.setattr(
            archival_module, "chunk_document", _fake_chunk_document)

        await archival_module.archive_agent_report(
            analysis_id=uuid4(), role=AgentRole.MACRO, ticker=None,
            content=original)

        assert chunk_document_calls == [original], (
            "new content must still be chunked (not skipped) even though a "
            "near-duplicate was found")
        assert rag_deletion_calls == [], "old report's RAG data must be kept"
        assert superseded_report_ids == [], "old report's DB row must be kept"

    @pytest.mark.asyncio
    async def test_old_report_superseded_only_after_new_report_is_stored(
            self, monkeypatch):
        from ai.rag.chunking import Chunk as RagChunk
        from ai.rag.indexing import DeletionResult

        content = " ".join(["macro"] * 60)
        events: list[str] = []

        class _Repo:

            def __init__(self, session):
                pass

            async def list_recent_by_category(
                    self, category, ticker=None, limit=5):
                return [
                    TestNearDuplicateArchivalSupersede._FakeReport(
                        "r1", content)
                ]

            async def create(self, **kwargs):
                events.append("new_report_created")

            async def delete(self, report_id):
                events.append(f"deleted:{report_id}")

        class _JobRepo:

            def __init__(self, session):
                pass

            async def create(self, **kwargs):
                return SimpleNamespace(id=uuid4())

            async def set_status(self, job_id, status):
                return True

        async def _record_document_chunks(session, **kwargs):
            return None

        class _KgRepo:

            def __init__(self, session):
                pass

            async def delete_relations_for_report(self, report_id):
                return 0

        async def _delete_document_completely(document_id, session):
            events.append(f"rag_deleted:{document_id}")
            return DeletionResult(
                document_id=document_id, qdrant_bm25_chunks_removed=1)

        async def _index_chunks(**kwargs):
            events.append("new_report_indexed")

        async def _no_kg(**kwargs):
            return None

        monkeypatch.setattr(archival_module, "AgentReportRepository", _Repo)
        monkeypatch.setattr(
            archival_module, "EmbeddingJobRepository", _JobRepo)
        monkeypatch.setattr(
            archival_module, "record_document_chunks", _record_document_chunks)
        monkeypatch.setattr(
            archival_module, "KnowledgeGraphRepository", _KgRepo)
        monkeypatch.setattr(
            archival_module, "SessionLocal",
            lambda: TestNearDuplicateArchivalSupersede._FakeSession())
        monkeypatch.setattr(
            archival_module, "delete_document_completely",
            _delete_document_completely)
        monkeypatch.setattr(archival_module, "index_chunks", _index_chunks)
        monkeypatch.setattr(archival_module, "_extract_and_store_kg", _no_kg)

        async def _owners(ids):
            return dict.fromkeys(ids)

        monkeypatch.setattr(archival_module, "_analysis_owners", _owners)
        monkeypatch.setattr(
            archival_module, "chunk_document",
            lambda content, category, metadata=None:
            [RagChunk(text=content, chunk_index=0, category=category)])
        self._patch_embed_texts(monkeypatch, [[1.0, 0.0], [1.0, 0.0]])

        await archival_module.archive_agent_report(
            analysis_id=uuid4(), role=AgentRole.MACRO, ticker=None,
            content=content)
        assert events == [
            "new_report_indexed", "new_report_created", "rag_deleted:r1",
            "deleted:r1"
        ]


def _reading_row(
        other_name, relation_type, direction="->", corroboration_count=1,
        confidence=0.8, source_categories="risk_analysis", first_seen_at=None,
        last_confirmed_at=None):
    first_seen_at = first_seen_at or datetime(2026, 8, 1, tzinfo=UTC)
    last_confirmed_at = last_confirmed_at or first_seen_at
    return (
        other_name, relation_type, direction, corroboration_count, confidence,
        source_categories, first_seen_at, last_confirmed_at)


class TestTagSupersededReadings:

    def test_only_repeated_readings_are_tagged(self):
        rows = [_reading_row("82.6%", "has_30d_volatility")]
        tags = _tag_superseded_readings(rows)
        assert tags == {}

        older = _reading_row(
            "82.6%", "has_30d_volatility",
            last_confirmed_at=datetime(2026, 8, 8, tzinfo=UTC))
        newer = _reading_row(
            "83.3%", "has_30d_volatility",
            last_confirmed_at=datetime(2026, 8, 20, tzinfo=UTC))
        tags = _tag_superseded_readings([older, newer])

        assert tags[id(older)] == " [SUPERSEDED by the MOST RECENT reading]"
        assert tags[id(newer)] == " [MOST RECENT reading]"


class TestKnowledgeGraphRepository:

    @pytest.mark.usefixtures("_schema_ready")
    @pytest.mark.asyncio
    async def test_entity_race_keeps_earlier_flushed_work(self):
        suffix = uuid4().hex[:8].upper()
        raced = f"RACED_{suffix}"
        earlier = f"EARLIER_{suffix}"

        async with SessionLocal() as session:
            session.add(
                KgEntity(
                    name=raced, normalized_name=raced, entity_type="company"))
            await session.commit()

        async with SessionLocal() as session:
            repo = KnowledgeGraphRepository(session)
            first = await repo.get_or_create_entity(
                name=earlier, entity_type="company")

            real_scalar = session.scalar
            calls = {"n": 0}

            async def _scalar_missing_once(stmt, *args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    return None
                return await real_scalar(stmt, *args, **kwargs)

            session.scalar = (  # type: ignore[method-assign]
                _scalar_missing_once
            )
            got = await repo.get_or_create_entity(
                name=raced, entity_type="company")
            session.scalar = real_scalar  # type: ignore[method-assign]
            assert got.normalized_name == raced
            await session.commit()
            first_id = first.id

        async with SessionLocal() as session:
            rows = (
                await session.scalars(
                    select(KgEntity).where(
                        KgEntity.normalized_name.in_([raced,
                                                      earlier])))).all()
            assert {r.normalized_name for r in rows} == {raced, earlier}
            assert first_id in {r.id for r in rows}


class TestFindRelationsForEntityNameDatesAndPagination:

    async def _make_report(
            self, db_session, *, ticker: str, category: str) -> AgentReport:
        analysis = await AnalysisRepository(db_session).create(
            user_id=None, portfolio_id=None, ticker=ticker)
        report = AgentReport(
            id=uuid4(), analysis_id=analysis.id,
            agent_role=AgentRole.FUNDAMENTAL, ticker=ticker, category=category,
            content="real test content, not empty")
        db_session.add(report)
        await db_session.flush()
        return report

    async def _make_entity(self, db_session, *, name: str) -> KgEntity:
        entity = KgEntity(
            name=name, normalized_name=name.strip().upper(),
            entity_type="company")
        db_session.add(entity)
        await db_session.flush()
        return entity

    async def _make_relation(
            self, db_session, *, source: KgEntity, target: KgEntity,
            report: AgentReport, created_at: datetime) -> KgRelation:
        relation = KgRelation(
            source_entity_id=source.id, target_entity_id=target.id,
            relation_type="competes_with", source_report_id=report.id,
            confidence=0.9, created_at=created_at)
        db_session.add(relation)
        await db_session.flush()
        return relation

    @pytest.mark.asyncio
    async def test_global_recency_sort_across_both_directions(
            self, db_session):
        source_report = await self._make_report(
            db_session, ticker="TESTCO",
            category=DocumentCategory.ANALYST_REPORT)
        target_report = await self._make_report(
            db_session, ticker="TESTCO",
            category=DocumentCategory.RISK_ANALYSIS)
        testco = await self._make_entity(db_session, name="TESTCO")

        base = datetime(2026, 1, 1, tzinfo=UTC)
        source_dated: list[tuple[KgEntity, datetime]] = []
        for i in range(4):
            other = await self._make_entity(db_session, name=f"SRC_O{i}")
            dated = base + timedelta(days=i)
            await self._make_relation(
                db_session, source=testco, target=other, report=source_report,
                created_at=dated)
            source_dated.append((other, dated))

        target_side_entity = await self._make_entity(db_session, name="TGT_T1")
        most_recent = base + timedelta(days=4)
        await self._make_relation(
            db_session, source=target_side_entity, target=testco,
            report=target_report, created_at=most_recent)

        repo = KnowledgeGraphRepository(db_session)
        results = await repo.find_relations_for_entity_name("TESTCO", limit=3)

        assert len(results) == 3, "must return exactly `limit` rows, not more"
        returned_names = [row[0] for row in results]
        returned_dates = [row[7] for row in results]

        assert "TGT_T1" in returned_names, (
            "the single most-recent relation (Jan 5, target-direction) must "
            "survive the global re-sort, not be dropped in favor of an older "
            "source-direction row that merely fit under its own separate cap")
        assert returned_dates == sorted(returned_dates, reverse=True), (
            "results must be globally sorted by recency descending, "
            "not source-direction-first then target-direction-second")
        assert (
            "SRC_O0"
            not in returned_names), "Jan1 (oldest) must not survive top-3"
        assert (
            "SRC_O1"
            not in returned_names), "Jan2 must not survive the real top-3"


class TestFilingFallbackNotPersisted:

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ingestion_sources")
    async def test_fallback_summary_is_marked_flagged_and_not_persisted(
            self, monkeypatch):

        class _Router:

            async def complete(self, *args, **kwargs):
                raise LLMError("provider down")

        text = " ".join(f"w{i}" for i in range(300))
        out = await filing_summary.summarize_filing(
            text,
            form="10-Q",
            ticker="AMD",
            llm_router=_Router()  # type: ignore[arg-type]
        )
        assert filing_summary.is_fallback_summary(out.text)
        assert not filing_summary.is_fallback_summary("A real summary.")

        monkeypatch.setattr(
            config.settings, "SEC_EDGAR_USER_AGENT", "t t@example.com")
        monkeypatch.setattr(
            sec_edgar_filings, "_fetch_real_filings_sync",
            lambda ticker, names: [(
                "AMD_10-Q_2026-08-05.txt", "10-Q", "2026-08-05", "raw", None)])

        async def _upload(*args, **kwargs):
            return None

        async def _fallback(raw_text, **kwargs):
            return filing_summary.FilingSummary(
                f"{filing_summary.FALLBACK_SUMMARY_PREFIX} raw")

        monkeypatch.setattr(sec_edgar_filings, "upload_bytes", _upload)
        monkeypatch.setattr(sec_edgar_filings, "summarize_filing", _fallback)
        out = await sec_edgar_filings.list_new_filings(
            "AMD", already_ingested_filenames=set())
        assert len(out) == 1 and out[0].is_fallback

        created: list[str] = []

        async def _create(**kwargs):
            created.append(kwargs["filename"])
            return "id"

        async def _existing(ticker, category):
            return []

        async def _filings(ticker, *, already_ingested_filenames):
            return [
                NewFiling(
                    "AMD_10-Q_a.txt", "excerpt", "10-Q", "2026-08-05",
                    is_fallback=True),
                NewFiling("AMD_10-K_b.txt", "summary", "10-K", "2026-02-01")
            ]

        async def _tickers():
            return ["AMD"]

        async def _no_digest(ticker, *, already_ingested_filenames):
            return None

        for module in (auto_workflow, sec_edgar_filings):
            monkeypatch.setattr(module, "create_system_document", _create)
            monkeypatch.setattr(
                module, "get_existing_document_filenames", _existing)
        monkeypatch.setattr(sec_edgar_filings, "list_new_filings", _filings)
        monkeypatch.setattr(auto_workflow, "get_tracked_tickers", _tickers)
        monkeypatch.setattr(auto_workflow, "generate_news_digest", _no_digest)

        await auto_workflow.run_auto_ingestion_workflow()
        assert created == ["AMD_10-K_b.txt"]

        created.clear()
        context = await nodes_module.load_fresh_filing_context("AMD")
        assert created == ["AMD_10-K_b.txt"]
        assert context is not None and "excerpt" in context


class _FakeTitleMap:

    def __init__(self, data: dict[str, str]) -> None:
        self.data = data
        self.calls = 0

    async def load(self) -> dict[str, str]:
        self.calls += 1
        return self.data


class TestResolveTickerName:

    @pytest.mark.usefixtures("_schema_ready")
    @pytest.mark.asyncio
    async def test_not_found_cached_race_tolerated_and_read_failure_safe(
            self, monkeypatch):
        import ai.providers.sec_edgar as sec_edgar_module
        from ai.knowledge.kg_context import resolve_ticker_name
        from domains.knowledge.model import TickerName
        from domains.knowledge.repository import TickerNameRepository

        ticker = ("Z" + uuid4().hex[:6]).upper()
        http = _FakeTitleMap({"OTHER": "Other Inc"})
        monkeypatch.setattr(
            config.settings, "SEC_EDGAR_USER_AGENT", "t t@example.com")
        monkeypatch.setattr(
            sec_edgar_module, "load_ticker_to_title", http.load)

        async with SessionLocal() as session:
            assert await resolve_ticker_name(session, ticker) == ticker
            await session.rollback()
        async with SessionLocal() as session:
            assert await resolve_ticker_name(session, ticker) == ticker
        assert http.calls == 1

        ticker2 = ("Y" + uuid4().hex[:6]).upper()
        http.data = {ticker2: "Race Corp"}
        async with SessionLocal() as session:
            session.add(TickerName(ticker=ticker2, official_name="Race Corp"))
            await session.commit()

        async def _miss(self, t):
            return None

        monkeypatch.setattr(TickerNameRepository, "get", _miss)
        async with SessionLocal() as session:
            assert await resolve_ticker_name(session, ticker2) == "Race Corp"
            await session.commit()

        async def _boom(self, t):
            raise RuntimeError("db down")

        monkeypatch.setattr(TickerNameRepository, "get", _boom)
        assert (
            await
            resolve_ticker_name(object(),
                                "amd") == "AMD")  # type: ignore[arg-type]


class TestRecycledShare:
    _NOTE = (
        "- (SPY) [agent:macro] (2026-09-01): Federal Reserve policy keeps "
        "borrowing costs elevated while manufacturing demand weakens steadily")
    _SOURCE = (
        "- FRED series FEDFUNDS latest 4.33 percent; payroll growth slowed "
        "to 120,000 jobs")

    def _scratchpad(self):
        return [{
            "action": "get_macro_data",
            "observation": self._SOURCE
        }, {
            "action": "search_research_archive",
            "observation": "\n" + self._NOTE
        }, {
            "action": "finish_answer",
            "observation": "ignored"
        }, {
            "action": "search_news",
            "observation": "x",
            "is_tool_result": False
        }]

    def test_share_of_words_copied_from_prior_notes(self):
        cases = {
            "copied-note": (
                "[Live] Federal Reserve policy keeps borrowing costs elevated "
                "while manufacturing demand weakens steadily.", 1.0),
            "anchored-by-a-source-number": (
                "Federal Reserve policy keeps borrowing costs elevated at "
                "4.33 percent while manufacturing demand weakens.", 0.0),
            "fresh-sentence": (
                "Payroll growth slowed sharply to 120,000 jobs last month.",
                0.0),
            "no-countable-sentence": ("Short.", 0.0)
        }
        for case_id, (answer, expected) in cases.items():
            got = recycled_share(answer, self._scratchpad())
            assert got == expected, case_id

        pad = self._scratchpad()
        pad[0] = {**pad[0], "tool_error": True}
        answer = cases["anchored-by-a-source-number"][0]
        assert recycled_share(answer, pad) == 1.0

        pad = [{"action": "get_macro_data", "observation": self._SOURCE}]
        assert recycled_share("Federal Reserve policy is tight.", pad) == 0.0
        assert recycled_share("anything", None) == 0.0

    def test_tool_header_and_guidance_are_not_a_prior_note(self):
        header = (
            "Prior macro note search results for 'Federal Reserve policy "
            "keeps borrowing costs elevated' (1 passages):\nNote: passages "
            "tagged [agent:...] are PRIOR AI-WRITTEN NOTES from 2026 runs.")
        observation = header + "\n" + self._NOTE
        notes, fresh = note_and_source_passages(observation)
        assert notes == [self._NOTE.removeprefix("- ")]
        assert fresh == []
        assert note_and_source_passages(header) == ([], [header])
        pad = [{"action": "search_prior_macro_notes", "observation": header}]
        answer = "Federal Reserve policy keeps borrowing costs elevated."
        assert recycled_share(answer, pad) == 0.0
        assert prior_note_similarity(answer, pad) is None
        assert split_agent_note_passages(
            [observation]) == (self._NOTE.removeprefix("- "), "")
