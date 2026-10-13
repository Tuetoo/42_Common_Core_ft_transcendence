import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4
import ai.ingestion.sec_edgar_filings as sec_filings_module
import ai.workflows.auto_ingestion_workflow as auto_ingestion_workflow
import ai.workflows.backtest_workflow as backtest_workflow
import ai.workflows.review_workflow as review_workflow
import ai.workflows.tier_monitoring_workflow as tier_workflow
import pytest
from ai.ingestion.sec_edgar_filings import NewFiling
from ai.providers.base import PriceBar, ProviderError
from ai.tools import indicators as metrics_module
from database import SessionLocal
from helpers import _headers
from sqlalchemy import delete, select, text, update
from structlog.testing import capture_logs
from domains.analysis.model import Analysis, Recommendation, RecommendationTier
from domains.analysis.repository import (AgentCatalogRepository,
                                         AnalysisPersistenceRepository,
                                         AnalysisRepository)
from domains.analytics.model import RecommendationMetric
from domains.analytics.repository import AnalyticsRepository
from domains.backtest.repository import BacktestRepository
from domains.decision.model import DecisionOutcome
from domains.decision.repository import InvestmentDecisionRepository
from domains.memory.repository import EpisodicMemoryRepository
from shared import events as shared_events
from shared.enums import (AgentRole, AgentStatus, RecommendationAction,
                          TierSide, TierStatus)
from shared.exceptions import UpstreamServiceError, ValidationAppError

TODAY = date(2026, 8, 13)
TODAY_TS = datetime(2026, 8, 13, 4, tzinfo=UTC)
SERIES_START = date(2026, 1, 5)
CYRILLIC = "".join(chr(c) for c in range(0x0430, 0x0440))
RUSSIAN_SENTENCE = f"{CYRILLIC} {CYRILLIC} {CYRILLIC}"
_PENDING = TierStatus.PENDING_ACCEPTANCE.value
_ACCEPTED = TierStatus.ACCEPTED_AWAITING_TRIGGER.value


def _price_bars(
        ticker: str, closes: list[float], start: datetime,
        interval: str = "1d") -> list[PriceBar]:
    return [
        PriceBar(
            ticker=ticker, interval=interval,
            timestamp=start + timedelta(days=i), open=close, high=close,
            low=close, close=close, volume=1000, source="fake")
        for i, close in enumerate(closes)
    ]


class _FakeMultiSymbolProvider:

    def __init__(
            self, closes_by_symbol: dict[str, list[float]], *, start: date):
        self._closes_by_symbol = {
            k.upper(): v
            for k, v in closes_by_symbol.items()
        }
        self._start = start

    async def get_price_history_range(
            self, ticker: str, *, start: date, end: date,
            interval: str = "1d") -> list[PriceBar]:
        base = datetime.combine(self._start, datetime.min.time(), tzinfo=UTC)
        return _price_bars(
            ticker, self._closes_by_symbol[ticker.upper()], base, interval)


class _FakeBenchmarkProvider:

    def __init__(self, closes: list[float], *, start: date):
        self._closes = closes
        self._start = start

    async def get_price_history_range(
            self, ticker: str, *, start: date, end: date,
            interval: str = "1d") -> list[PriceBar]:
        base = datetime.combine(self._start, datetime.min.time(), tzinfo=UTC)
        return _price_bars(ticker, self._closes, base, interval)


class _FakeSeriesProvider:

    def __init__(self, series: dict[str, dict[date, float]]):
        self.series = series
        self.requested: list[str] = []

    async def get_price_history_range(
            self, ticker, *, start, end, interval="1d"):
        self.requested.append(ticker)
        closes = {
            d: c
            for d, c in self.series.get(ticker, {}).items()
            if start <= d <= end
        }
        return [
            PriceBar(
                ticker=ticker, interval="1d", timestamp=datetime.combine(
                    day, datetime.min.time(), tzinfo=UTC), open=close,
                high=close, low=close, close=close, volume=1, source="fake")
            for day, close in sorted(closes.items())
        ]


class _FakeMarketProvider:

    def __init__(self, closes_by_ticker: dict[str, list[float]]):
        self._closes_by_ticker = closes_by_ticker

    async def get_price_history_range(
            self, ticker: str, *, start: date, end: date,
            interval: str = "1d") -> list[PriceBar]:
        closes = self._closes_by_ticker.get(ticker, [])
        base = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
        return _price_bars(ticker, closes, base, interval)


@dataclass
class _StubAnalysis:
    ticker: str = "AMD"
    portfolio_id: UUID | None = field(default_factory=uuid4)
    user_id: UUID | None = None


@dataclass
class _StubRecommendation:
    window_expires_at: datetime | None
    analysis: _StubAnalysis = field(default_factory=_StubAnalysis)
    accepted_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class _StubTier:
    id: UUID = field(default_factory=uuid4)
    status: str = TierStatus.ACCEPTED_AWAITING_TRIGGER.value
    side: str = TierSide.BUY.value
    target_price: Decimal = Decimal("495")
    quantity: Decimal = Decimal("10")
    recommendation: _StubRecommendation = field(
        default_factory=lambda: _StubRecommendation(
            window_expires_at=datetime.now(UTC) + timedelta(days=7)))
    triggered_at: datetime | None = None
    triggered_price: float | None = None


def _bar(
        *, ts: datetime, open_: float, high: float, low: float,
        close: float) -> PriceBar:
    return PriceBar(
        ticker="AMD", interval="1d", timestamp=ts, open=open_, high=high,
        low=low, close=close, volume=1_000_000, source="fake")


@pytest.fixture
def fake_provider(monkeypatch, _schema_ready):
    provider = _FakeMarketProvider({})
    monkeypatch.setattr(
        review_workflow, "get_market_data_provider", lambda: provider)
    return provider


@pytest.fixture
def captured_events(monkeypatch):
    events: list[tuple[str, dict]] = []

    async def _fake_publish(settings, event_name, payload):
        events.append((event_name, payload))

    monkeypatch.setattr(shared_events, "publish_cross_service", _fake_publish)
    return events


async def _backdate(session, row, *, days: int) -> None:
    row.created_at = datetime.now(UTC) - timedelta(days=days)
    await session.flush()


async def _create_single_ticker_backtest(
        *, start_date: date, end_date: date, initial_capital: float) -> UUID:
    async with SessionLocal() as session:
        backtest = await BacktestRepository(session).create(
            user_id=None, strategy_name="single_ticker_replay",
            benchmark_symbol="SPY", start_date=start_date, end_date=end_date,
            initial_capital=initial_capital)
        await session.commit()
        return backtest.id


async def _create_portfolio_backtest(
        *, start_date: date, end_date: date, initial_capital: float) -> UUID:
    async with SessionLocal() as session:
        backtest = await BacktestRepository(session).create(
            user_id=None, strategy_name="portfolio_snapshot_backtest",
            benchmark_symbol="SPY", start_date=start_date, end_date=end_date,
            initial_capital=initial_capital)
        await session.commit()
        return backtest.id


async def _create_triggered_tier(
        *, ticker: str, side: str, quantity: float, triggered_price: float,
        triggered_at: datetime, portfolio_id: UUID | None = None) -> UUID:
    async with SessionLocal() as session:
        analysis = Analysis(ticker=ticker.upper(), portfolio_id=portfolio_id)
        session.add(analysis)
        await session.flush()

        recommendation = Recommendation(
            analysis_id=analysis.id, recommendation=side, confidence=0.8,
            reasoning="test fixture", debate_converged=True)
        session.add(recommendation)
        await session.flush()

        tier = RecommendationTier(
            recommendation_id=recommendation.id, side=side,
            target_price=triggered_price, quantity=quantity,
            status=TierStatus.TRIGGERED.value, triggered_at=triggered_at,
            triggered_price=triggered_price)
        session.add(tier)
        await session.commit()
        return tier.id


async def _delete_backtest(backtest_id: UUID) -> None:
    async with SessionLocal() as session:
        backtest = await BacktestRepository(session).get_by_id(backtest_id)
        await session.delete(backtest)
        await session.commit()


async def _no_cash_flows(portfolio_id, *, start_date, end_date) -> list[dict]:
    return []


def _cash_flows(amounts_by_day: dict[int, float], *,
                start: date) -> list[dict]:
    return [{
        "transaction_date": (start + timedelta(days=offset)).isoformat(),
        "amount": amount
    } for offset, amount in amounts_by_day.items()]


def _snapshot_rows(total_values: list[float], *, start: date) -> list[dict]:
    return [{
        "snapshot_date": (start + timedelta(days=i)).isoformat(),
        "total_value": value,
        "cash_balance": 0.0
    } for i, value in enumerate(total_values)]


async def _analysis_with_tiers(
        session, *, user_id, side=TierSide.BUY.value,
        prices=((Decimal("100"), Decimal("2")),
                (Decimal("95"), Decimal("3")))):
    analysis = await AnalysisRepository(session).create(
        user_id=user_id, portfolio_id=None, ticker="AMD")
    persist = AnalysisPersistenceRepository(session)
    rec = await persist.save_recommendation(
        analysis_id=analysis.id, recommendation="buy", confidence=0.7,
        reasoning="r", confidence_adjustment_note=None, debate_converged=True)
    if prices:
        await persist.save_recommendation_tiers(
            recommendation_id=rec.id, side=side, window_days=5,
            tiers=list(prices))
    return analysis, rec


async def _tier_statuses(session, rec_id) -> list[str]:
    rows = await session.execute(
        text(
            "SELECT status FROM ai.recommendation_tiers "
            "WHERE recommendation_id = :r ORDER BY target_price"),
        {"r": rec_id})
    return [r[0] for r in rows.all()]


def _kg_row(other: str, relation: str = "depends_on") -> tuple:
    at = datetime(2026, 9, 1, tzinfo=UTC)
    return (other, relation, "->", 1, 0.8, "risk_analysis", at, at)


class _CapturingRouter:

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.prompts: list[str] = []

    async def complete(self, tier, messages, **kwargs):
        self.prompts.append(messages[0].content)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(content="A macro narrative sentence. " * 20)


def _month_start(day: date, months_back: int) -> date:
    index = day.year * 12 + day.month - 1 - months_back
    return date(index // 12, index % 12 + 1, 1)


def _macro_series(series: str, lookback_days: int) -> list:
    from ai.providers.base import MacroDataPoint

    today = date.today()
    if series == "gdp":
        newest = _month_start(today, 6 + (today.month - 1) % 3)
        dates = [_month_start(newest, 3 * i) for i in range(12)]
        values = [30000.0 + i for i in range(12)]
    else:
        newest = _month_start(today, 2)
        dates = [_month_start(newest, i) for i in range(30)]
        values = [300.0 * 1.03**(-i / 12) for i in range(30)]
    start = (today - timedelta(days=lookback_days)).replace(day=1)
    series_id = "CPIAUCSL" if series == "cpi" else series.upper()
    return sorted((
        MacroDataPoint(
            series_id=series_id, label=series, value=value, observed_at=day,
            source="fred")
        for day, value in zip(dates, values, strict=True) if day >= start),
                  key=lambda p: p.observed_at)


@pytest.mark.usefixtures("ingestion_sources")
class TestAutoIngestionWorkflow:

    @pytest.mark.asyncio
    async def test_failing_fallback_and_quota_deferred_tickers(
            self, monkeypatch):
        from ai.providers.base import ProviderQuotaError

        async def _fake_get_tracked_tickers():
            return ["BROKEN", "AMD"]

        async def _fake_get_existing_filenames(ticker, category):
            if ticker == "BROKEN":
                raise RuntimeError("simulated SEC EDGAR failure")
            return []

        async def _fake_list_new_filings(
                ticker, *, already_ingested_filenames):
            return [
                NewFiling(
                    filename=f"{ticker}_10-K_2026-01-01.txt",
                    text="real filing text", form="10-K",
                    filing_date="2026-01-01")
            ]

        async def _fake_generate_news_digest(
                ticker, *, already_ingested_filenames):
            return None

        created_documents = []

        async def _fake_create_system_document(
                *, filename, content, category, ticker, content_date=None):
            created_documents.append(filename)
            return "some-uuid"

        monkeypatch.setattr(
            auto_ingestion_workflow, "get_tracked_tickers",
            _fake_get_tracked_tickers)
        for module in (auto_ingestion_workflow, sec_filings_module):
            monkeypatch.setattr(
                module, "get_existing_document_filenames",
                _fake_get_existing_filenames)
            monkeypatch.setattr(
                module, "create_system_document", _fake_create_system_document)
        monkeypatch.setattr(
            sec_filings_module, "list_new_filings", _fake_list_new_filings)
        monkeypatch.setattr(
            auto_ingestion_workflow, "generate_news_digest",
            _fake_generate_news_digest)

        result = await auto_ingestion_workflow.run_auto_ingestion_workflow()

        assert result["tickers_processed"] == 1
        assert result["documents_created"] == 1
        assert result["failed_tickers"] == ["BROKEN"]
        assert created_documents == ["AMD_10-K_2026-01-01.txt"]

        async def _existing(ticker, category):
            return []

        async def _filings(ticker, *, already_ingested_filenames):
            return [
                NewFiling(
                    "AMD_10-K_2026-02-01.txt", "excerpt", "10-K", "2026-02-01",
                    is_fallback=True)
            ]

        async def _no_digest(ticker, *, already_ingested_filenames):
            return None

        async def _amd_only():
            return ["AMD"]

        for module in (auto_ingestion_workflow, sec_filings_module):
            monkeypatch.setattr(
                module, "get_existing_document_filenames", _existing)
        monkeypatch.setattr(sec_filings_module, "list_new_filings", _filings)
        monkeypatch.setattr(
            auto_ingestion_workflow, "get_tracked_tickers", _amd_only)
        monkeypatch.setattr(
            auto_ingestion_workflow, "generate_news_digest", _no_digest)

        result = await auto_ingestion_workflow.run_auto_ingestion_workflow()

        assert result["failed_tickers"] == ["AMD"]

        async def _tickers():
            return ["AAA", "BBB", "CCC"]

        async def _no_filings(ticker):
            return sec_filings_module.FilingIngestResult(new_filings=[])

        async def _digest(ticker, *, already_ingested_filenames):
            if ticker == "CCC":
                raise RuntimeError("core-service down")
            raise ProviderQuotaError("NewsAPI request limit reached")

        monkeypatch.setattr(
            auto_ingestion_workflow, "get_tracked_tickers", _tickers)
        monkeypatch.setattr(
            auto_ingestion_workflow, "get_existing_document_filenames",
            _existing)
        monkeypatch.setattr(
            auto_ingestion_workflow, "ingest_new_filings", _no_filings)
        monkeypatch.setattr(
            auto_ingestion_workflow, "generate_news_digest", _digest)

        result = await auto_ingestion_workflow.run_auto_ingestion_workflow()

        assert result["news"] == {
            "attempted": 1,
            "failed_tickers": ["CCC"],
            "quota_deferred": ["AAA", "BBB"]
        }
        assert result["filings"]["failed_tickers"] == []
        assert result["failed_tickers"] == ["CCC"]

    @pytest.mark.asyncio
    async def test_outages_fail_the_ticker_but_missing_filings_do_not(
            self, monkeypatch):
        import ai.ingestion.digests as digests
        import config
        from edgar import CompanyNotFoundError

        monkeypatch.setattr(
            config.settings, "SEC_EDGAR_USER_AGENT", "Test test@x.com")

        def _not_found(ticker, already_ingested_filenames):
            raise CompanyNotFoundError(ticker)

        async def _no_name(ticker):
            return None

        async def _empty_map():
            return {}

        async def _spy():
            return ["SPY"]

        async def _existing(ticker, category):
            return []

        for module in (auto_ingestion_workflow, sec_filings_module):
            monkeypatch.setattr(
                module, "get_existing_document_filenames", _existing)
        monkeypatch.setattr(
            sec_filings_module, "_fetch_real_filings_sync", _not_found)
        monkeypatch.setattr(
            auto_ingestion_workflow, "get_tracked_tickers", _spy)
        monkeypatch.setattr(digests, "lookup_company_name", _no_name)
        monkeypatch.setattr(digests, "load_ticker_to_title", _empty_map)

        result = await auto_ingestion_workflow.run_auto_ingestion_workflow()

        assert result["tickers_processed"] == 1
        assert result["failed_tickers"] == []
        assert result["filings"]["attempted"] == 1
        assert result["filings"]["failed_tickers"] == []

        cases = {
            "sec-and-news-down": (
                ConnectionError("SEC down"), ProviderError("NewsAPI down"),
                None, ["AAA", "BBB"]),
            "news-down": (
                None, ProviderError("NewsAPI down"), None, ["AAA", "BBB"]),
            "sec-down": (
                ConnectionError("SEC down"), None, None, ["AAA", "BBB"]),
            "company-name-lookup-down": (
                None, None, ProviderError("ticker map down"), ["AAA", "BBB"]),
            "nothing-new": (None, None, None, [])
        }

        async def _tickers():
            return ["AAA", "BBB"]

        monkeypatch.setattr(
            auto_ingestion_workflow, "get_tracked_tickers", _tickers)
        for case_id, case in cases.items():
            sec_error, news_error, map_error, failed = case

            def _fetch(ticker, already_ingested_filenames, error=sec_error):
                if error is not None:
                    raise error
                return []

            class _News:

                async def search_news(self, query, error=news_error, **kw):
                    if error is not None:
                        raise error
                    return []

            async def _name(ticker, error=map_error):
                return None if error is not None else "Example Corp"

            async def _ticker_map(error=map_error):
                raise error

            monkeypatch.setattr(
                sec_filings_module, "_fetch_real_filings_sync", _fetch)
            monkeypatch.setattr(digests, "lookup_company_name", _name)
            monkeypatch.setattr(
                digests, "load_ticker_to_title", _ticker_map)
            monkeypatch.setattr(digests, "NewsApiProvider", _News)

            result = (
                await auto_ingestion_workflow.run_auto_ingestion_workflow())

            assert result["failed_tickers"] == failed, case_id
            assert result["tickers_processed"] == 2 - len(failed), case_id

    @pytest.mark.asyncio
    async def test_unconfigured_sources_are_disabled_not_failed(
            self, monkeypatch):
        import config

        for name in ("SEC_EDGAR_USER_AGENT", "NEWSAPI_API_KEY",
                     "FRED_API_KEY"):
            monkeypatch.setattr(config.settings, name, "")

        async def _tickers():
            return ["AAA", "BBB"]

        async def _must_not_run(*args, **kwargs):
            raise AssertionError("a disabled source must not be called")

        monkeypatch.setattr(
            auto_ingestion_workflow, "get_tracked_tickers", _tickers)
        for name in ("ingest_new_filings", "generate_news_digest",
                     "generate_macro_report"):
            monkeypatch.setattr(auto_ingestion_workflow, name, _must_not_run)

        workflow = auto_ingestion_workflow
        with capture_logs() as logs:
            tickers = await workflow.run_auto_ingestion_workflow()
            macro = await workflow.run_macro_auto_ingestion_workflow()

        assert tickers["failed_tickers"] == []
        assert tickers["filings"]["attempted"] == 0
        assert tickers["news"]["attempted"] == 0
        assert macro == {
            "documents_created": 0,
            "failed": False,
            "attempted": False
        }
        disabled = [
            log["source"] for log in logs
            if log["event"] == "auto_ingestion.source_not_configured"
        ]
        assert disabled == ["sec_edgar", "newsapi", "fred"]

    @pytest.mark.asyncio
    async def test_macro_step_outcomes_and_report_facts(self, monkeypatch):
        import ai.ingestion.digests as digests
        from ai.ingestion.digests import MacroReport

        async def _existing(ticker, category):
            return []

        monkeypatch.setattr(
            auto_ingestion_workflow, "get_existing_document_filenames",
            _existing)
        cases = {
            "report-stored": (None, None, False, False, 1),
            "every-indicator-failed": (
                ProviderError("FRED down"), None, False, True, 0),
            "narrative-call-failed": (
                None, RuntimeError("LLM down"), False, True, 0),
            "no-data-published": (None, None, True, False, 0)
        }
        for case_id, case in cases.items():
            fred_error, llm_error, empty, failed, created = case

            class _Fred:

                async def get_indicator(
                        self, series, *, lookback_days, error=fred_error,
                        no=empty):
                    if error is not None:
                        raise error
                    return [] if no else _macro_series(series, lookback_days)

            router = _CapturingRouter(error=llm_error)
            stored: list[str] = []

            async def _create(stored=stored, **kwargs):
                stored.append(kwargs["content"])
                return "doc-id"

            monkeypatch.setattr(digests, "FredProvider", _Fred)
            monkeypatch.setattr(
                digests, "LLMRouter", lambda r=router: r)
            monkeypatch.setattr(
                auto_ingestion_workflow, "create_system_document", _create)

            workflow = auto_ingestion_workflow
            result = await workflow.run_macro_auto_ingestion_workflow()

            assert result == {
                "documents_created": created,
                "failed": failed,
                "attempted": True
            }, case_id
            assert len(stored) == created, case_id

        class _FullFred:

            async def get_indicator(self, series, *, lookback_days):
                return _macro_series(series, lookback_days)

        router = _CapturingRouter()
        monkeypatch.setattr(digests, "FredProvider", _FullFred)
        monkeypatch.setattr(digests, "LLMRouter", lambda: router)

        report = await digests.generate_macro_report(
            already_ingested_filenames=set())

        assert report is not None
        facts = router.prompts[0].splitlines()[-5:]
        assert [line.split(":")[0] for line in facts] == [
            "fed_funds_rate", "cpi", "unemployment_rate", "gdp",
            "10y_treasury_yield"
        ]
        cpi = facts[1]
        assert "year-over-year inflation 3.00%" in cpi
        assert "index level" not in cpi

        async def _report(*, already_ingested_filenames):
            return MacroReport(
                filename="macro.txt", text="m", content_date="2026-10-01")

        async def _create_fails(**kwargs):
            raise RuntimeError("core-service down")

        monkeypatch.setattr(
            auto_ingestion_workflow, "generate_macro_report", _report)
        monkeypatch.setattr(
            auto_ingestion_workflow, "create_system_document", _create_fails)

        result = (
            await auto_ingestion_workflow.run_macro_auto_ingestion_workflow())

        assert result == {
            "documents_created": 0,
            "failed": True,
            "attempted": True
        }

    @pytest.mark.asyncio
    async def test_failed_bookkeeping_and_archival_remove_written_chunks(
            self, monkeypatch):
        import ai.knowledge.archival as archival
        import ai.workflows.rag_ingestion_workflow as wf

        removed: list[str] = []

        async def _delete(document_id):
            removed.append(document_id)
            return 0

        monkeypatch.setattr(wf, "delete_document_chunks", _delete)
        doc = str(uuid4())
        await wf._mark_failed_bookkeeping(doc, None, points_deleted=True)
        assert removed == [doc]

        removed.clear()

        async def _embed(texts):
            return [[0.1] * 8 for _ in texts]

        async def _index(**kwargs):
            return None

        async def _fail(**kwargs):
            raise RuntimeError("database down")

        async def _delete(document_id):
            removed.append(document_id)
            return 0

        async def _owner(analysis_id):
            return None

        async def _no_duplicate(**kwargs):
            return None

        monkeypatch.setattr(archival, "embed_texts", _embed)
        monkeypatch.setattr(archival, "index_chunks", _index)
        monkeypatch.setattr(archival, "_persist_report_rows", _fail)
        monkeypatch.setattr(archival, "delete_document_chunks", _delete)
        monkeypatch.setattr(archival, "_archive_owner", _owner)
        monkeypatch.setattr(
            archival, "_find_near_duplicate_of_recent_report", _no_duplicate)
        with pytest.raises(RuntimeError):
            await archival.archive_agent_report(
                analysis_id=uuid4(), role=AgentRole.NEWS, ticker="AMD",
                content="AMD reported record data center revenue this "
                "quarter. " * 20)
        assert len(removed) == 1


class TestBacktestWorkflow:

    @pytest.mark.asyncio
    async def test_no_tiers_fails_scoping_and_unloadable_backtest(
            self, monkeypatch, _schema_ready):
        ticker = f"ZZZ{uuid4().hex[:6].upper()}"
        start = date(2026, 2, 1)
        end = date(2026, 2, 5)
        backtest_id = await _create_single_ticker_backtest(
            start_date=start, end_date=end, initial_capital=10_000.0)

        await backtest_workflow.run_backtest_workflow(
            str(backtest_id), ticker=ticker)

        async with SessionLocal() as session:
            backtest = await BacktestRepository(session).get_by_id(backtest_id)
            assert backtest.status == "failed"

        await _delete_backtest(backtest_id)

        ticker = f"ZZZ{uuid4().hex[:6].upper()}"
        start = date(2026, 5, 1)
        end = date(2026, 5, 5)
        target_portfolio = uuid4()
        other_portfolio = uuid4()

        await _create_triggered_tier(
            ticker=ticker, side="buy", quantity=5, triggered_price=100.0,
            triggered_at=datetime(2026, 5, 2,
                                  tzinfo=UTC), portfolio_id=target_portfolio)
        await _create_triggered_tier(
            ticker=ticker, side="buy", quantity=999, triggered_price=1.0,
            triggered_at=datetime(2026, 5, 2,
                                  tzinfo=UTC), portfolio_id=other_portfolio)

        initial_capital = 10_000.0
        backtest_id = await _create_single_ticker_backtest(
            start_date=start, end_date=end, initial_capital=initial_capital)

        ticker_closes = [100.0, 101.0, 102.0, 103.0, 104.0]
        spy_closes = [400.0, 402.0, 404.0, 406.0, 408.0]
        provider = _FakeMultiSymbolProvider({
            ticker: ticker_closes,
            "SPY": spy_closes
        }, start=start)
        monkeypatch.setattr(
            backtest_workflow, "get_market_data_provider", lambda: provider)

        await backtest_workflow.run_backtest_workflow(
            str(backtest_id), ticker=ticker,
            portfolio_id=str(target_portfolio))

        async with SessionLocal() as session:
            backtest = await BacktestRepository(session).get_by_id(
                backtest_id, with_details=True)
            assert backtest.status == "completed"
            assert float(backtest.ending_capital) == pytest.approx(
                initial_capital - 5 * 100.0 + 5 * 104.0)

        await _delete_backtest(backtest_id)

        backtest_id = await _create_single_ticker_backtest(
            start_date=start, end_date=end, initial_capital=initial_capital)
        published: list[str | None] = []

        async def _publish(backtest_uuid, *, completed, error=None):
            published.append(error)

        monkeypatch.setattr(
            backtest_workflow, "get_market_data_provider",
            lambda: _FakeMarketProvider({"SPY": spy_closes}))
        monkeypatch.setattr(
            backtest_workflow, "publish_backtest_finished", _publish)
        await backtest_workflow.run_backtest_workflow(
            str(backtest_id), ticker=ticker,
            portfolio_id=str(target_portfolio))

        async with SessionLocal() as session:
            backtest = await BacktestRepository(session).get_by_id(backtest_id)
            assert backtest.status == "failed"
        assert len(published) == 1
        assert f"no price history for {ticker}" in (published[0] or "")
        await _delete_backtest(backtest_id)

        await backtest_workflow.run_backtest_workflow(
            str(uuid4()), ticker="AMD")
        await backtest_workflow.mark_backtest_failed(uuid4())

        failed: list[UUID] = []

        async def _boom(backtest_uuid):
            raise RuntimeError("db hiccup")

        async def _mark(backtest_uuid, error=None):
            failed.append(backtest_uuid)

        monkeypatch.setattr(
            backtest_workflow, "load_backtest_and_mark_running", _boom)
        monkeypatch.setattr(backtest_workflow, "mark_backtest_failed", _mark)
        ids = [uuid4() for _ in range(3)]
        await backtest_workflow.run_backtest_workflow(
            str(ids[0]), ticker="AMD")
        await backtest_workflow.run_portfolio_backtest_workflow(
            str(ids[1]), portfolio_id=str(ids[1]))
        await backtest_workflow.run_recommendation_replay_workflow(
            str(ids[2]))
        assert failed == ids

    def test_replay_positions_capital_limit_and_paired_returns(self):
        from ai.workflows.backtest_workflow import (InsufficientCapitalError,
                                                    replay_trade_events)

        def _at(day: int) -> datetime:
            return datetime(2026, 4, day, 10, 0, tzinfo=UTC)

        days = [date(2026, 4, d) for d in (1, 2, 3)]
        events = [("AAA", "buy", 10.0, 100.0, _at(1)),
                  ("BBB", "buy", 5.0, 50.0, _at(2)),
                  ("AAA", "sell", 10.0, 110.0, _at(3)),
                  ("BBB", "sell", 6.0, 60.0, _at(3))]
        closes = {
            "AAA": {
                days[0]: 101.0,
                days[1]: 102.0,
                days[2]: 111.0
            },
            "BBB": {
                days[1]: 51.0
            }
        }
        result = replay_trade_events(
            events, closes_by_ticker=closes, trading_days=days,
            initial_capital=10_000.0, log_prefix="test")
        cash_day1 = 10_000.0 - 1_000.0
        cash_day2 = cash_day1 - 250.0
        cash_day3 = cash_day2 + 1_100.0 + 360.0
        assert result.equity_curve == pytest.approx([
            cash_day1 + 10 * 101.0, cash_day2 + 10 * 102.0 + 5 * 51.0,
            cash_day3 + (-1.0) * 51.0
        ])
        assert result.realized_trade_returns == pytest.approx([0.10, 0.20])

        day = date(2026, 4, 1)
        events = [
            ("AAA", "buy", 10.0, 100.0, datetime(2026, 4, 1, tzinfo=UTC)),
            ("AAA", "buy", 10.0, 100.0, datetime(2026, 4, 1, tzinfo=UTC))
        ]
        with pytest.raises(InsufficientCapitalError, match="2,000.00"):
            replay_trade_events(
                events, closes_by_ticker={"AAA": {
                    day: 100.0
                }}, trading_days=[day], initial_capital=1_500.0,
                log_prefix="test")

        strategy, benchmark = metrics_module.paired_simple_returns(
            [100.0, 0.0, 50.0, 55.0], [10.0, 11.0, 12.0, 6.0])
        assert strategy == pytest.approx([-1.0, 0.1])
        assert benchmark == pytest.approx([0.1, -0.5])


class TestPortfolioBacktestWorkflow:

    @pytest.mark.asyncio
    async def test_too_few_snapshots_and_missing_benchmark_close_fail(
            self, monkeypatch, _schema_ready):
        from ai.workflows.backtest_workflow import backtest_error_text

        start = date(2026, 1, 1)
        backtest_id = await _create_portfolio_backtest(
            start_date=start, end_date=start, initial_capital=100.0)

        async def _one_snapshot(
                portfolio_id, *, start_date=None, end_date=None):
            return _snapshot_rows([100.0], start=start)

        monkeypatch.setattr(
            backtest_workflow, "get_portfolio_snapshots",
            _one_snapshot)

        await backtest_workflow.run_portfolio_backtest_workflow(
            str(backtest_id), portfolio_id=str(uuid4()))

        async with SessionLocal() as session:
            backtest = await BacktestRepository(session).get_by_id(backtest_id)
            assert backtest.status == "failed"

        await _delete_backtest(backtest_id)

        start = date(2026, 9, 28)
        backtest_id = await _create_portfolio_backtest(
            start_date=start, end_date=date(2026, 9, 29),
            initial_capital=10580.0)

        async def _snap(portfolio_id, *, start_date=None, end_date=None):
            return _snapshot_rows([10580.0, 10580.0], start=start)

        errors: list[str | None] = []

        async def _failed(backtest_uuid, *, error=None):
            errors.append(error)

        provider = _FakeBenchmarkProvider([765.61, float("nan")], start=start)
        workflow = backtest_workflow
        monkeypatch.setattr(workflow, "get_portfolio_snapshots", _snap)
        monkeypatch.setattr(
            workflow, "get_market_data_provider", lambda: provider)
        monkeypatch.setattr(
            workflow, "get_portfolio_cash_flows", _no_cash_flows)
        monkeypatch.setattr(workflow, "mark_backtest_failed", _failed)
        await workflow.run_portfolio_backtest_workflow(
            str(backtest_id), portfolio_id=str(uuid4()))
        await _delete_backtest(backtest_id)

        assert len(errors) == 1
        text = errors[0] or ""
        assert "1 aligned days" in text
        assert "no usable SPY close for 2026-09-29" in text
        assert text.endswith("hours after the close)")
        assert backtest_error_text(ValueError("x" *
                                              400)).startswith("ValueError: ")

    @pytest.mark.asyncio
    async def test_deposits_and_adjustments_are_not_strategy_return(
            self, monkeypatch, _schema_ready):
        start = date(2026, 1, 1)
        end = date(2026, 1, 4)
        total_values = [100_000.0, 101_000.0, 151_500.0, 153_015.0]
        spy_closes = [400.0, 402.0, 404.0, 406.0]

        backtest_id = await _create_portfolio_backtest(
            start_date=start, end_date=end, initial_capital=total_values[0])

        async def _snap(portfolio_id, *, start_date=None, end_date=None):
            return _snapshot_rows(total_values, start=start)

        async def _flows(portfolio_id, *, start_date, end_date):
            return _cash_flows({2: 50_000.0}, start=start)

        provider = _FakeBenchmarkProvider(spy_closes, start=start)
        monkeypatch.setattr(
            backtest_workflow, "get_portfolio_snapshots", _snap)
        monkeypatch.setattr(
            backtest_workflow, "get_market_data_provider",
            lambda: provider)
        monkeypatch.setattr(
            backtest_workflow, "get_portfolio_cash_flows", _flows)

        await backtest_workflow.run_portfolio_backtest_workflow(
            str(backtest_id), portfolio_id=str(uuid4()))

        raw_total_return = metrics_module.total_return(total_values)

        async with SessionLocal() as session:
            backtest = await BacktestRepository(session).get_by_id(backtest_id)
            assert backtest.status == "completed"
            strategy_return = float(backtest.strategy_return)
            ending_capital = float(backtest.ending_capital)
            assert strategy_return == pytest.approx(0.02515, abs=1e-6)
            assert ending_capital == pytest.approx(102_515.0)
            assert strategy_return != pytest.approx(raw_total_return)

        await _delete_backtest(backtest_id)

        flows = [{
            "transaction_date": "2026-01-10",
            "transaction_type": "adjustment",
            "ticker": "AMD",
            "quantity": 1,
            "price": 50.0
        }, {
            "transaction_date": "2026-01-10",
            "transaction_type": "ADJUSTMENT",
            "ticker": "NOPE",
            "quantity": 2,
            "price": 7.5
        }]
        closes = {"AMD": {date(2026, 1, 9): 101.0}}
        assert backtest_workflow._net_flow_by_date(
            flows, closes) == {
                date(2026, 1, 10): 116.0
            }

        days = [SERIES_START + timedelta(days=i) for i in range(4)]
        values = [10_000.0, 10_100.0, 12_201.0, 12_323.01]
        provider = _FakeSeriesProvider({
            "SPY": {
                d: 400.0 + i
                for i, d in enumerate(days)
            },
            "AMD": dict.fromkeys(days, 200.0)
        })
        backtest_id = await _create_portfolio_backtest(
            start_date=days[0], end_date=days[-1], initial_capital=values[0])

        async def _snapshots(portfolio_id, *, start_date=None, end_date=None):
            return [{
                "snapshot_date": d.isoformat(),
                "total_value": v,
                "cash_balance": 0.0
            } for d, v in zip(days, values, strict=True)]

        async def _flows(portfolio_id, *, start_date, end_date):
            return [{
                "transaction_date": days[2].isoformat(),
                "transaction_type": "adjustment",
                "ticker": "AMD",
                "quantity": 10.0,
                "price": 50.0
            }]

        published: list = []

        async def _publish(*args, **kwargs):
            published.append(args)

        monkeypatch.setattr(
            backtest_workflow, "get_portfolio_snapshots", _snapshots)
        monkeypatch.setattr(
            backtest_workflow, "get_portfolio_cash_flows", _flows)
        monkeypatch.setattr(
            backtest_workflow, "get_market_data_provider",
            lambda: provider)
        monkeypatch.setattr(
            backtest_workflow, "publish_backtest_finished", _publish)
        try:
            await backtest_workflow.run_portfolio_backtest_workflow(
                str(backtest_id), portfolio_id=str(uuid4()))
            async with SessionLocal() as session:
                row = await BacktestRepository(session).get_by_id(backtest_id)
                assert row.status == "completed"
                assert float(row.strategy_return) == pytest.approx(
                    0.030301, abs=1e-6)
                assert float(row.ending_capital) == pytest.approx(
                    10_303.01, abs=1e-2)
            assert "AMD" in provider.requested
            assert published
        finally:
            await _delete_backtest(backtest_id)

    @pytest.mark.asyncio
    async def test_leading_zero_value_snapshot_days_are_skipped(
            self, monkeypatch, _schema_ready):
        start = date(2026, 3, 2)
        values = [0.0, 100_000.0, 101_000.0]
        provider = _FakeBenchmarkProvider([400.0, 402.0, 404.0], start=start)

        async def _flows(portfolio_id, *, start_date, end_date):
            return _cash_flows({1: 100_000.0}, start=start)

        monkeypatch.setattr(
            backtest_workflow, "get_market_data_provider", lambda: provider)
        monkeypatch.setattr(
            backtest_workflow, "get_portfolio_cash_flows", _flows)
        for rows, status in ((values, "completed"), (values[:2], "failed")):

            async def _snap(portfolio_id, *, start_date=None, end_date=None,
                            rows=rows):
                return _snapshot_rows(rows, start=start)

            monkeypatch.setattr(
                backtest_workflow, "get_portfolio_snapshots", _snap)
            backtest_id = await _create_portfolio_backtest(
                start_date=start, end_date=start + timedelta(days=2),
                initial_capital=100_000.0)
            try:
                await backtest_workflow.run_portfolio_backtest_workflow(
                    str(backtest_id), portfolio_id=str(uuid4()))
                async with SessionLocal() as session:
                    row = await BacktestRepository(session).get_by_id(
                        backtest_id)
                    assert row.status == status
                    if status == "completed":
                        assert float(row.ending_capital) == pytest.approx(
                            101_000.0)
                        assert float(row.strategy_return) == pytest.approx(
                            0.01)
            finally:
                await _delete_backtest(backtest_id)


class TestReviewAiAccuracy:

    @pytest.mark.asyncio
    async def test_scores_once_updates_memory_and_skips_missing_prices(
            self, fake_provider):
        fake_provider._closes_by_ticker["AAPL"] = [100.0, 105.0, 112.0]

        async with SessionLocal() as session:
            analysis = await AnalysisRepository(session).create(
                user_id=None, portfolio_id=None, ticker="AAPL")
            rec = await AnalysisPersistenceRepository(
                session).save_recommendation(
                    analysis_id=analysis.id,
                    recommendation=RecommendationAction.BUY.value,
                    confidence=0.8, reasoning="strong momentum",
                    confidence_adjustment_note=None, debate_converged=True)
            await _backdate(session, rec, days=35)
            memory = await EpisodicMemoryRepository(session).create(
                user_id=None, analysis_id=analysis.id, ticker="AAPL",
                recommendation="buy", confidence=0.8, summary="momentum play")
            await session.commit()
            analysis_id, recommendation_id, memory_id = (
                analysis.id, rec.id, memory.id)

        scored = (await review_workflow.review_ai_accuracy()).done
        assert scored == 1

        async with SessionLocal() as session:
            metric_rows = ((
                await session.execute(
                    select(RecommendationMetric).where(
                        RecommendationMetric.recommendation_id ==
                        recommendation_id))).scalars().all())
            assert len(metric_rows) == 1
            assert metric_rows[0].accuracy_score == 1.0

            memory = await EpisodicMemoryRepository(
                session).get_by_analysis_id(analysis_id)
            assert memory.outcome == "correct"
            assert memory.outcome_return == pytest.approx(0.12)

        scored_again = (await review_workflow.review_ai_accuracy()).done
        assert scored_again == 0

        await self._cleanup(analysis_id, recommendation_id, memory_id)

        async with SessionLocal() as session:
            analysis = await AnalysisRepository(session).create(
                user_id=None, portfolio_id=None, ticker="ZZZZ")
            rec = await AnalysisPersistenceRepository(
                session).save_recommendation(
                    analysis_id=analysis.id,
                    recommendation=RecommendationAction.SELL.value,
                    confidence=0.6, reasoning="x",
                    confidence_adjustment_note=None, debate_converged=True)
            await _backdate(session, rec, days=40)
            await session.commit()
            analysis_id, recommendation_id = analysis.id, rec.id

        scored = (await review_workflow.review_ai_accuracy()).done
        assert scored == 0

        async with SessionLocal() as session:
            metric_rows = ((
                await session.execute(
                    select(RecommendationMetric).where(
                        RecommendationMetric.recommendation_id ==
                        recommendation_id))).scalars().all())
            assert metric_rows == []

        await self._cleanup(analysis_id, recommendation_id, None)

    @staticmethod
    async def _cleanup(analysis_id, recommendation_id, memory_id) -> None:
        async with SessionLocal() as session:
            await session.execute(
                delete(RecommendationMetric).where(
                    RecommendationMetric.recommendation_id ==
                    recommendation_id))
            if memory_id is not None:
                from domains.memory.model import EpisodicMemory

                await session.execute(
                    delete(EpisodicMemory).where(
                        EpisodicMemory.id == memory_id))
            await session.execute(
                delete(Recommendation).where(
                    Recommendation.id == recommendation_id))
            await session.execute(
                delete(Analysis).where(Analysis.id == analysis_id))
            await session.commit()


class TestReviewDecisionOutcomes:

    @pytest.mark.asyncio
    async def test_multi_window_no_double_count_and_missing_series(
            self, fake_provider, captured_events, monkeypatch):
        fake_provider._closes_by_ticker.update({
            "NFLX": [400.0, 440.0],
            "SPY": [400.0, 404.0]
        })

        async with SessionLocal() as session:
            analysis = await AnalysisRepository(session).create(
                user_id=None, portfolio_id=None, ticker="NFLX")
            rec = await AnalysisPersistenceRepository(
                session).save_recommendation(
                    analysis_id=analysis.id,
                    recommendation=RecommendationAction.BUY.value,
                    confidence=0.7, reasoning="x",
                    confidence_adjustment_note=None, debate_converged=True)
            user_id = uuid4()
            decision = await InvestmentDecisionRepository(session).create(
                user_id=user_id, portfolio_id=None, recommendation_id=rec.id,
                ticker="NFLX", action_taken="followed", notes=None)
            await _backdate(session, decision, days=8)
            await session.commit()
            analysis_id = analysis.id
            recommendation_id, decision_id = rec.id, decision.id

        evaluated = (await review_workflow.review_decision_outcomes()).done
        assert evaluated == 1

        async with SessionLocal() as session:
            rows = ((
                await session.execute(
                    select(DecisionOutcome).where(
                        DecisionOutcome.decision_id == decision_id).order_by(
                            DecisionOutcome.evaluation_days))).scalars().all())
            assert [r.evaluation_days for r in rows] == [7]

        assert [name for name, _ in captured_events
                ] == ["decision.outcome.evaluated"]
        payload = captured_events[0][1]
        assert payload["user_id"] == str(user_id)
        assert payload["analysis_id"] == str(analysis_id)
        assert payload["ticker"] == "NFLX"
        assert payload["evaluation_days"] == 7
        assert payload["outperformed"] is True

        evaluated_again = (
            await review_workflow.review_decision_outcomes()).done
        assert evaluated_again == 0

        async with SessionLocal() as session:
            rows = ((
                await session.execute(
                    select(DecisionOutcome).where(
                        DecisionOutcome.decision_id == decision_id)
                )).scalars().all())
            assert len(rows) == 1

        async with SessionLocal() as session:
            decision_row = await InvestmentDecisionRepository(
                session).get_by_id(decision_id)
            await _backdate(session, decision_row, days=31)
            await session.commit()

        evaluated_third = (
            await review_workflow.review_decision_outcomes()).done
        assert evaluated_third == 1

        async with SessionLocal() as session:
            rows = ((
                await session.execute(
                    select(DecisionOutcome).where(
                        DecisionOutcome.decision_id == decision_id).order_by(
                            DecisionOutcome.evaluation_days))).scalars().all())
            assert [r.evaluation_days for r in rows] == [7, 30]
            for row in rows:
                assert row.strategy_return == pytest.approx(0.10)
                assert row.benchmark_return == pytest.approx(0.01)
                assert row.outperformed is True

        await self._cleanup(analysis_id, recommendation_id, decision_id)

        async def _ret(ticker, since, window_days=None):
            return None if ticker == "SPY" else 0.05

        class _Repo:

            def __init__(self, session):
                raise AssertionError("nothing may be written")

        monkeypatch.setattr(review_workflow, "_realized_return", _ret)
        monkeypatch.setattr(
            review_workflow, "DecisionOutcomeRepository", _Repo)
        written = await review_workflow._evaluate_one_decision(
            uuid4(), "AMD", date(2026, 1, 1), "followed", 7, user_id=uuid4(),
            analysis_id=uuid4())
        assert written is None

    @staticmethod
    async def _cleanup(analysis_id, recommendation_id, decision_id) -> None:
        async with SessionLocal() as session:
            await session.execute(
                delete(DecisionOutcome).where(
                    DecisionOutcome.decision_id == decision_id))
            from domains.decision.model import InvestmentDecision

            await session.execute(
                delete(InvestmentDecision).where(
                    InvestmentDecision.id == decision_id))
            await session.execute(
                delete(Recommendation).where(
                    Recommendation.id == recommendation_id))
            await session.execute(
                delete(Analysis).where(Analysis.id == analysis_id))
            await session.commit()


class TestReviewStepCounts:

    @staticmethod
    def _items(count: int):
        recs = [
            SimpleNamespace(
                id=uuid4(), analysis_id=uuid4(), recommendation="buy")
            for _ in range(count)
        ]
        decisions = [
            SimpleNamespace(
                id=uuid4(), ticker="AMD", created_at=datetime.now(UTC),
                user_id=uuid4(), recommendation_id=uuid4())
            for _ in range(count)
        ]

        class _Analytics:

            def __init__(self, session):
                pass

            async def list_recommendations_pending_metric(self, **kwargs):
                return recs

        class _Decisions:

            def __init__(self, session):
                pass

            async def list_pending_review(self, **kwargs):
                return decisions if kwargs["min_age_days"] == 7 else []

        return recs, decisions, _Analytics, _Decisions

    @pytest.mark.asyncio
    async def test_failures_and_missing_prices_are_counted_not_fatal(
            self, monkeypatch):
        rw = review_workflow
        recs, decisions, analytics, decision_repo = self._items(2)

        async def _score(recommendation_id, analysis_id, action):
            if recommendation_id == recs[0].id:
                raise RuntimeError("provider 503")
            return True

        async def _evaluate(decision_id, *args, **kwargs):
            if decision_id == decisions[0].id:
                raise RuntimeError("provider 503")
            return True

        monkeypatch.setattr(rw, "AnalyticsRepository", analytics)
        monkeypatch.setattr(rw, "InvestmentDecisionRepository", decision_repo)
        monkeypatch.setattr(rw, "_score_one_recommendation", _score)
        monkeypatch.setattr(rw, "_evaluate_one_decision", _evaluate)

        assert await rw.review_ai_accuracy() == (1, 2, 1)
        assert await rw.review_decision_outcomes() == (1, 2, 1)

        recs, decisions, analytics, decision_repo = self._items(3)

        async def _score(recommendation_id, analysis_id, action):
            if recommendation_id == recs[0].id:
                raise RuntimeError("provider 503")
            return None if recommendation_id == recs[1].id else True

        async def _evaluate(decision_id, *args, **kwargs):
            if decision_id == decisions[0].id:
                return None
            return decision_id == decisions[2].id

        monkeypatch.setattr(rw, "AnalyticsRepository", analytics)
        monkeypatch.setattr(rw, "InvestmentDecisionRepository", decision_repo)
        monkeypatch.setattr(rw, "_score_one_recommendation", _score)
        monkeypatch.setattr(rw, "_evaluate_one_decision", _evaluate)

        assert await rw.review_ai_accuracy() == rw.StepCounts(1, 3, 2)
        assert await rw.review_decision_outcomes() == rw.StepCounts(1, 3, 1)


class TestReviewRollup:

    @pytest.mark.asyncio
    async def test_agent_metrics_exclude_skipped_runs_and_all_counts(
            self, fake_provider, captured_events):
        async with SessionLocal() as session:
            agent = await AgentCatalogRepository(session).get_or_create(
                AgentRole.RISK.value)
            analysis = await AnalysisRepository(session).create(
                user_id=None, portfolio_id=None, ticker="NFLX")
            persist = AnalysisPersistenceRepository(session)
            for status, score in [(AgentStatus.COMPLETED.value, 0.8),
                                  (AgentStatus.COMPLETED.value, 0.4),
                                  (AgentStatus.FAILED.value, None),
                                  ("skipped", None)]:
                await persist.save_agent_run(
                    analysis_id=analysis.id, agent_id=agent.id, status=status,
                    started_at=None, completed_at=None, duration_ms=None,
                    score=score, summary=status, output=None)
            await session.commit()
            agent_id, analysis_id = agent.id, analysis.id

        updated = await review_workflow.run_agent_metrics_rollup()
        assert updated >= 1

        async with SessionLocal() as session:
            metrics = await AnalyticsRepository(session).list_agent_metrics()
            metric = next(m for m in metrics if m.agent_id == agent_id)
            assert metric.avg_score == pytest.approx((0.8 + 0.4) / 2)
            assert metric.success_rate == pytest.approx(2 / 3)

        await review_workflow.run_agent_metrics_rollup()
        async with SessionLocal() as session:
            metrics = await AnalyticsRepository(session).list_agent_metrics()
            matching = [m for m in metrics if m.agent_id == agent_id]
            assert len(matching) == 1

        async with SessionLocal() as session:
            from domains.analysis.model import AgentResult, AgentRun

            run_ids = ((
                await session.execute(
                    select(AgentRun.id).where(
                        AgentRun.analysis_id == analysis_id))).scalars().all())
            await session.execute(
                delete(AgentResult).where(
                    AgentResult.agent_run_id.in_(run_ids)))
            await session.execute(
                delete(AgentRun).where(AgentRun.analysis_id == analysis_id))
            await session.execute(
                delete(Analysis).where(Analysis.id == analysis_id))
            await session.commit()

        result = await review_workflow.run_review_workflow()
        assert set(result.keys()) == {
            "recommendations_scored", "recommendations_attempted",
            "recommendations_failed", "decisions_evaluated",
            "decisions_attempted", "decisions_failed", "agent_metrics_updated"
        }
        for value in result.values():
            assert isinstance(value, int)
            assert value >= 0


class TestTierMonitoringGating:

    @pytest.mark.asyncio
    async def test_gated_untriggered_and_sell_trigger_outcomes(
            self, monkeypatch):
        called = {"yes": False}

        async def _should_not_be_called(*args, **kwargs):
            called["yes"] = True
            raise AssertionError("must not look up price for this tier")

        monkeypatch.setattr(
            tier_workflow, "get_bars_cache_first", _should_not_be_called)
        gated = {
            "expired-window": (
                -1, TierStatus.ACCEPTED_AWAITING_TRIGGER.value, "expired",
                TierStatus.EXPIRED.value),
            "not-yet-accepted": (
                7, TierStatus.PENDING_ACCEPTANCE.value, "pending",
                TierStatus.PENDING_ACCEPTANCE.value)
        }
        for case_id, case in gated.items():
            window_days, status, expected_outcome, expected_status = case
            tier = _StubTier(
                status=status, recommendation=_StubRecommendation(
                    window_expires_at=datetime.now(UTC) +
                    timedelta(days=window_days)))
            outcome = await tier_workflow._check_one_tier(tier, today=TODAY)

            assert outcome == expected_outcome, case_id
            assert tier.status == expected_status, case_id
            assert not called["yes"], case_id

        untriggered = {
            "target-not-touched": ([
                _bar(
                    ts=TODAY_TS, open_=500.0, high=505.0, low=498.0,
                    close=502.0)
            ], "still_open"),
            "provider-error": (
                ProviderError("live provider and cache both unavailable"),
                "price_lookup_failed"),
            "no-bar-for-today": ([
                _bar(
                    ts=datetime(2026, 8, 12, tzinfo=UTC), open_=480.0,
                    high=485.0, low=478.0, close=482.0)
            ], "no_fresh_data")
        }
        for case_id, (source, expected) in untriggered.items():

            async def _fake_cache_first(
                    ticker, *, interval, lookback_days, source=source):
                if isinstance(source, Exception):
                    raise source
                return source

            monkeypatch.setattr(
                tier_workflow, "get_bars_cache_first", _fake_cache_first)

            tier = _StubTier(
                side=TierSide.BUY.value, target_price=Decimal("495"))
            outcome = await tier_workflow._check_one_tier(tier, today=TODAY)

            assert outcome == expected, case_id
            assert (
                tier.status == TierStatus.ACCEPTED_AWAITING_TRIGGER.value
            ), case_id

        for outcome in ("expired", "still_open", "no_fresh_data", "pending",
                        "price_lookup_failed"):
            assert (
                tier_workflow._tier_outcome_event(object(), outcome)
                is None), outcome  # type: ignore[arg-type]

        bar = _bar(
            ts=TODAY_TS, open_=498.0, high=502.0, low=496.0, close=500.0)

        async def _fake_cache_first(ticker, *, interval, lookback_days):
            return [bar]

        monkeypatch.setattr(
            tier_workflow, "get_bars_cache_first", _fake_cache_first)

        async def _fake_record_transaction(portfolio_id, **kwargs):
            pass

        monkeypatch.setattr(
            "clients.core_client.record_transaction", _fake_record_transaction)

        tier = _StubTier(side=TierSide.SELL.value, target_price=Decimal("500"))
        outcome = await tier_workflow._check_one_tier(tier, today=TODAY)

        assert outcome == "triggered"
        assert tier.triggered_price == 500.0

        opened_bar = PriceBar(
            ticker="AMD", interval="1d",
            timestamp=datetime(2026, 8, 13, 4, tzinfo=UTC),
            open=Decimal("105"), high=Decimal("106"), low=Decimal("95"),
            close=Decimal("99"), volume=1, source="fake")
        executed: list = []

        async def _bars(*args, **kwargs):
            return [opened_bar]

        async def _execute(tier, **kwargs):
            executed.append(kwargs["price"])
            return "triggered"

        monkeypatch.setattr(tier_workflow, "get_bars_cache_first", _bars)
        monkeypatch.setattr(
            tier_workflow, "_execute_and_mark_triggered", _execute)
        cases = {
            "accepted-after-the-bar-opened": (
                datetime(2026, 8, 13, 19, 55, tzinfo=UTC), "still_open"),
            "accepted-before-the-bar-opened": (
                datetime(2026, 8, 13, 13, 0, tzinfo=UTC), "triggered")
        }
        for case_id, (accepted, expected) in cases.items():
            executed.clear()
            recommendation = SimpleNamespace(
                window_expires_at=datetime.now(UTC) + timedelta(days=5),
                accepted_at=accepted,
                analysis=SimpleNamespace(ticker="AMD", portfolio_id=uuid4()))
            tier = SimpleNamespace(
                id=uuid4(), status=TierStatus.ACCEPTED_AWAITING_TRIGGER.value,
                side=TierSide.BUY.value, target_price=Decimal("100"),
                quantity=Decimal("1"), recommendation=recommendation)
            outcome = await tier_workflow._check_one_tier(tier, today=TODAY)
            assert outcome == expected, case_id
            assert executed == ([Decimal("100")]
                                if expected == "triggered" else []), case_id


class TestTierMonitoringRun:

    @pytest.mark.asyncio
    async def test_rejected_is_terminal_and_5xx_is_retried(
            self, monkeypatch, _schema_ready):
        import clients.core_client as cc

        ticker = "R" + uuid4().hex[:5].upper()
        async with SessionLocal() as session:
            analysis, rec = await _analysis_with_tiers(
                session, user_id=uuid4(),
                prices=[(Decimal("100"), Decimal("1")),
                        (Decimal("90"), Decimal("1"))])
            await session.execute(
                text(
                    "UPDATE ai.analyses SET ticker = :t, portfolio_id = :p, "
                    "status = 'completed' WHERE id = :a"), {
                        "t": ticker,
                        "p": uuid4(),
                        "a": analysis.id
                    })
            await session.execute(
                text(
                    "UPDATE ai.recommendation_tiers SET status = :s "
                    "WHERE recommendation_id = :r"), {
                        "s": TierStatus.ACCEPTED_AWAITING_TRIGGER.value,
                        "r": rec.id
                    })
            await session.commit()
            analysis_id, rec_id = analysis.id, rec.id

        now = datetime.now(UTC)

        async def _bars(t, **kwargs):
            if t != ticker:
                return []
            return [
                PriceBar(
                    ticker=t, interval="1d", timestamp=now,
                    open=Decimal("101"), high=Decimal("102"),
                    low=Decimal("85"), close=Decimal("99"), volume=1,
                    source="fake")
            ]

        attempts: list[str] = []

        async def _record(portfolio_id, **kwargs):
            attempts.append(kwargs["idempotency_key"])
            if kwargs["price"] == 100.0:
                raise cc.TransactionRejectedError(422, "Insufficient cash")
            raise UpstreamServiceError("core 503")

        monkeypatch.setattr(tier_workflow, "get_bars_cache_first", _bars)
        monkeypatch.setattr(cc, "record_transaction", _record)
        try:
            first = await tier_workflow.run_tier_monitoring_workflow()
            second = await tier_workflow.run_tier_monitoring_workflow()
            async with SessionLocal() as session:
                statuses = await _tier_statuses(session, rec_id)
                note = (
                    await session.execute(
                        text(
                            "SELECT tier_generation_note FROM "
                            "ai.recommendations "
                            "WHERE id = :r"), {"r": rec_id})).scalar_one()
        finally:
            async with SessionLocal() as session:
                for sql in ("DELETE FROM ai.recommendation_tiers "
                            "WHERE recommendation_id = :r",
                            "DELETE FROM ai.recommendations WHERE id = :r"):
                    await session.execute(text(sql), {"r": rec_id})
                await session.execute(
                    text("DELETE FROM ai.analyses WHERE id = :a"),
                    {"a": analysis_id})
                await session.commit()

        assert statuses == [
            TierStatus.ACCEPTED_AWAITING_TRIGGER.value, "execution_failed"
        ]
        assert first["execution_rejected"] == 1
        assert first["execution_failed"] == 1
        assert second["execution_rejected"] == 0
        assert second["execution_failed"] == 1
        assert len(attempts) == 3
        assert len(set(attempts)) == 2
        assert "Insufficient cash" in note


class TestTierRoutes:

    @pytest.mark.asyncio
    async def test_tiers_are_visible_through_the_analysis_routes(
            self, client, db_session):
        owner, stranger = uuid4(), uuid4()
        analysis, _rec = await _analysis_with_tiers(db_session, user_id=owner)
        base = f"/api/v1/analyses/{analysis.id}"

        detail = await client.get(base, headers=_headers(owner))
        assert detail.status_code == 200
        tiers = detail.json()["data"]["recommendation"]["tiers"]
        assert len(tiers) == 2
        assert {t["side"] for t in tiers} == {"buy"}
        assert {Decimal(t["target_price"])
                for t in tiers} == {Decimal("100"),
                                    Decimal("95")}
        assert all(t["status"] == "pending_acceptance" for t in tiers)
        assert all(t["window_expires_at"] for t in tiers)
        assert all(t["id"] for t in tiers)

        recommendation = await client.get(
            f"{base}/recommendation", headers=_headers(owner))
        assert len(recommendation.json()["data"]["tiers"]) == 2
        assert recommendation.json()["data"]["window_expires_at"] is not None

        listed = await client.get(f"{base}/tiers", headers=_headers(owner))
        assert listed.status_code == 200
        assert [Decimal(t["quantity"]) for t in listed.json()["data"]
                ] == [Decimal("3"), Decimal("2")]

        assert (
            await client.get(f"{base}/tiers",
                             headers=_headers(stranger))).status_code == 404
        assert (await client.get(f"{base}/tiers")).status_code == 401

        without_tiers = await AnalysisRepository(db_session).create(
            user_id=owner, portfolio_id=None, ticker="AMD")
        response = await client.get(
            f"/api/v1/analyses/{without_tiers.id}/tiers",
            headers=_headers(owner))
        assert response.status_code == 200
        assert response.json()["data"] == []


class TestDecisionUpdates:

    async def _post(
            self, db_session, user_id, analysis_id, action, notes=None):
        from domains.decision.schema import InvestmentDecisionCreate
        from domains.decision.service import DecisionService
        from shared.security import CurrentUser

        return await DecisionService(db_session).record_decision(
            user=CurrentUser(user_id, "user"), analysis_id=analysis_id,
            data=InvestmentDecisionCreate(action_taken=action, notes=notes))

    @pytest.mark.asyncio
    async def test_repost_ignore_decline_and_note_bounds(self, db_session):
        from domains.decision.repository import DecisionOutcomeRepository

        owner = uuid4()
        analysis, rec = await _analysis_with_tiers(db_session, user_id=owner)
        first = await self._post(
            db_session, owner, analysis.id, "partial", "a")
        await DecisionOutcomeRepository(db_session).create_if_absent(
            decision_id=first.id, evaluation_days=7, strategy_return=0.1,
            benchmark_return=0.0, alpha=0.1, outperformed=True)

        second = await self._post(
            db_session, owner, analysis.id, "ignored", "b")

        assert second.id == first.id
        assert second.action_taken == "ignored"
        assert second.notes == "b"
        assert second.created_at >= first.created_at
        count = (
            await db_session.execute(
                text(
                    "SELECT count(*) FROM ai.investment_decisions "
                    "WHERE recommendation_id = :r"),
                {"r": rec.id})).scalar_one()
        outcomes = (
            await db_session.execute(
                text(
                    "SELECT count(*) FROM ai.decision_outcomes "
                    "WHERE decision_id = :d"), {"d": first.id})).scalar_one()
        assert count == 1
        assert outcomes == 0

        analysis, rec = await _analysis_with_tiers(db_session, user_id=owner)
        await self._post(db_session, owner, analysis.id, "followed")
        again = await self._post(
            db_session, owner, analysis.id, "followed", "note")
        assert again.notes == "note"
        await self._post(db_session, owner, analysis.id, "partial")
        await db_session.execute(
            update(RecommendationTier).where(
                RecommendationTier.recommendation_id == rec.id,
                RecommendationTier.target_price == Decimal("95")).values(
                    status=TierStatus.TRIGGERED.value).execution_options(
                        synchronize_session="fetch"))
        back = await self._post(db_session, owner, analysis.id, "followed")
        assert back.action_taken == "followed"
        assert await _tier_statuses(db_session, rec.id) == [
            TierStatus.TRIGGERED.value, _ACCEPTED]
        await self._post(db_session, owner, analysis.id, "ignored")
        assert await _tier_statuses(db_session, rec.id) == [
            TierStatus.TRIGGERED.value, TierStatus.DECLINED.value]

        from pydantic import ValidationError
        from domains.decision.schema import InvestmentDecisionCreate

        owner = uuid4()
        analysis, _ = await _analysis_with_tiers(db_session, user_id=owner)
        await self._post(db_session, owner, analysis.id, "declined")
        with pytest.raises(ValidationAppError):
            await self._post(db_session, owner, analysis.id, "followed")

        InvestmentDecisionCreate(action_taken="ignored", notes="x" * 2000)
        with pytest.raises(ValidationError):
            InvestmentDecisionCreate(action_taken="ignored", notes="x" * 2001)


async def _tier_set(
        session, *, portfolio_id, ticker, statuses, user_id=None,
        side=TierSide.BUY.value, window_days=5, age_hours=0,
        price=Decimal("100"), quantity=Decimal("2")):
    analysis = await AnalysisRepository(session).create(
        user_id=user_id, portfolio_id=portfolio_id, ticker=ticker,
        status="completed")
    rec = Recommendation(
        analysis_id=analysis.id, recommendation="buy", confidence=0.7,
        reasoning="r", debate_converged=True,
        created_at=datetime.now(UTC) - timedelta(hours=age_hours),
        window_expires_at=datetime.now(UTC) + timedelta(days=window_days))
    session.add(rec)
    await session.flush()
    session.add_all([
        RecommendationTier(
            recommendation_id=rec.id, side=side, target_price=price - i,
            quantity=quantity, status=status)
        for i, status in enumerate(statuses)
    ])
    await session.flush()
    return analysis, rec


class TestTierBudgetAndAcceptance:

    @pytest.mark.asyncio
    async def test_committed_cash_counts_only_open_buy_tiers(
            self, db_session):
        from domains.analysis.repository import RecommendationTierRepository

        pf, other_pf = uuid4(), uuid4()
        system, _ = await _tier_set(
            db_session, portfolio_id=pf, ticker="SYS", statuses=[_PENDING])
        await _tier_set(
            db_session, portfolio_id=pf, ticker="USR", user_id=uuid4(),
            statuses=[_ACCEPTED], price=Decimal("50"))
        await _tier_set(
            db_session, portfolio_id=pf, ticker="DONE", statuses=[
                TierStatus.DECLINED.value, TierStatus.SUPERSEDED.value,
                TierStatus.EXPIRED.value, TierStatus.TRIGGERED.value,
                TierStatus.EXECUTION_FAILED.value
            ])
        await _tier_set(
            db_session, portfolio_id=pf, ticker="SHUT", statuses=[_PENDING],
            window_days=-1)
        await _tier_set(
            db_session, portfolio_id=pf, ticker="SELL", statuses=[_PENDING],
            side=TierSide.SELL.value)
        await _tier_set(
            db_session, portfolio_id=pf, ticker="SELF", statuses=[_PENDING])
        await _tier_set(
            db_session, portfolio_id=other_pf, ticker="ELSE",
            statuses=[_PENDING])
        repo = RecommendationTierRepository(db_session)

        assert await repo.committed_buy_cash(
            portfolio_id=pf, exclude_ticker="self") == Decimal("300")
        holders = await repo.committed_buy_holders(
            portfolio_id=pf, exclude_ticker="SELF")
        assert [(h.ticker, h.status) for h in holders
                ] == [("SYS", _PENDING), ("USR", _ACCEPTED)]
        assert holders[0].describe() == (
            f"SYS 200.00 (analysis {system.id}, pending acceptance)")

    async def test_weekend_deadline_and_system_tiers_lapse_after_it(
            self, db_session, owners):
        from config import AIServiceSettings
        from domains.analysis.repository import acceptance_lapsed

        assert AIServiceSettings.model_fields[
            "SYSTEM_TIER_ACCEPTANCE_HOURS"].default == 72
        friday_evening = datetime(2026, 10, 9, 22, 0, tzinfo=UTC)
        monday_open = datetime(2026, 10, 12, 13, 30, tzinfo=UTC)
        tuesday_evening = datetime(2026, 10, 13, 22, 0, tzinfo=UTC)

        def lapsed(now):
            return acceptance_lapsed(
                status=_PENDING, user_id=None, issued_at=friday_evening,
                now=now)

        assert not lapsed(monday_open)
        assert lapsed(tuesday_evening)

        from config import settings
        from domains.analysis.repository import RecommendationTierRepository
        from domains.analysis.service import tier_reads

        owner = uuid4()
        pf = owners(owner)
        stale = settings.SYSTEM_TIER_ACCEPTANCE_HOURS + 6
        lapsed, lapsed_rec = await _tier_set(
            db_session, portfolio_id=pf, ticker="OLD", statuses=[_PENDING],
            age_hours=stale)
        fresh, _ = await _tier_set(
            db_session, portfolio_id=pf, ticker="NEW", statuses=[_PENDING],
            age_hours=1)
        await _tier_set(
            db_session, portfolio_id=pf, ticker="ACC", statuses=[_ACCEPTED],
            age_hours=stale, price=Decimal("50"))
        users, users_rec = await _tier_set(
            db_session, portfolio_id=pf, ticker="USR", user_id=owner,
            statuses=[_PENDING], age_hours=stale, price=Decimal("25"))
        tiers = RecommendationTierRepository(db_session)
        analyses = AnalysisRepository(db_session)

        assert await tiers.committed_buy_cash(
            portfolio_id=pf, exclude_ticker="X") == Decimal("350")
        assert await tiers.unfilled_buy_tier_count(
            portfolio_id=pf, ticker="OLD") == 0
        assert await analyses.get_open_recommendation_for(
            portfolio_id=pf, ticker="OLD") is None
        assert (
            await analyses.get_open_recommendation_for(
                portfolio_id=pf, ticker="USR")).id == users_rec.id
        assert await tiers.invalidate_unfilled_tiers_for_new_recommendation(
            portfolio_id=pf, ticker="OLD",
            new_action=RecommendationAction.HOLD, new_prices=[]) == []

        loaded = await analyses.get_by_id(lapsed.id, with_details=True)
        assert [t.status for t in tier_reads(loaded)
                ] == [TierStatus.EXPIRED.value]
        assert [t.status for t in tier_reads(
            await analyses.get_by_id(fresh.id, with_details=True))
                ] == [_PENDING]
        for status in (_PENDING, _ACCEPTED):
            shut, _ = await _tier_set(
                db_session, portfolio_id=pf, ticker="SHUT", user_id=owner,
                statuses=[status], window_days=-1)
            assert [t.status for t in tier_reads(
                await analyses.get_by_id(shut.id, with_details=True))
                    ] == [TierStatus.EXPIRED.value]

        with pytest.raises(ValidationAppError, match="already expired"):
            await TestDecisionUpdates()._post(
                db_session, owner, lapsed.id, "followed")
        assert await _tier_statuses(db_session, lapsed_rec.id) == [_PENDING]
        await TestDecisionUpdates()._post(
            db_session, owner, users.id, "followed")
        assert await _tier_statuses(db_session, users_rec.id) == [_ACCEPTED]

        tier_id = (
            await db_session.execute(
                select(RecommendationTier.id).where(
                    RecommendationTier.recommendation_id ==
                    lapsed_rec.id))).scalar_one()
        tier = await tiers.get_tier_with_details(tier_id)
        assert await tier_workflow._check_one_tier(
            tier, today=TODAY) == "expired"
        assert tier.status == TierStatus.EXPIRED.value

    @pytest.mark.asyncio
    async def test_accept_that_updates_no_tier_is_rejected_and_not_stored(
            self, db_session, monkeypatch):
        from domains.analysis.repository import RecommendationTierRepository

        owner = uuid4()
        analysis, rec = await _analysis_with_tiers(db_session, user_id=owner)
        original = RecommendationTierRepository.accept_pending

        async def _expired_meanwhile(self, recommendation_id):
            await self.session.execute(
                text(
                    "UPDATE ai.recommendation_tiers SET status = 'expired' "
                    "WHERE recommendation_id = :r"), {"r": recommendation_id})
            return await original(self, recommendation_id)

        monkeypatch.setattr(
            RecommendationTierRepository, "accept_pending", _expired_meanwhile)
        with pytest.raises(ValidationAppError, match="no tier still awaiting"):
            await TestDecisionUpdates()._post(
                db_session, owner, analysis.id, "followed")

        stored = (
            await db_session.execute(
                text(
                    "SELECT count(*) FROM ai.investment_decisions "
                    "WHERE recommendation_id = :r"),
                {"r": rec.id})).scalar_one()
        accepted_at = (
            await db_session.execute(
                text("SELECT accepted_at FROM ai.recommendations "
                     "WHERE id = :r"), {"r": rec.id})).scalar_one()
        assert stored == 0
        assert accepted_at is None

    @pytest.mark.asyncio
    async def test_budget_blocked_note_names_the_cash_holders(
            self, db_session, monkeypatch):
        import ai.workflows.analysis_workflow as wf
        from ai.recommendation.position_sizing import BudgetBlockedNote

        pf = uuid4()
        from shared.market_calendar import market_today

        holder, holder_rec = await _tier_set(
            db_session, portfolio_id=pf, ticker="JPM", statuses=[_PENDING])
        seen: dict = {}

        async def _blocked(**kwargs):
            seen["committed"] = kwargs["committed_buy_cash"]
            return None, BudgetBlockedNote("No entry tiers: no budget.")

        monkeypatch.setattr(wf, "generate_position_sizing", _blocked)
        target = await AnalysisRepository(db_session).create(
            user_id=None, portfolio_id=pf, ticker="AMD")
        rec = await wf._save_recommendation_and_tiers(
            db_session, AnalysisPersistenceRepository(db_session),
            analysis_id=str(target.id), analysis_uuid=target.id,
            ticker="AMD", portfolio_id=str(pf), final_rec={
                "action": "buy",
                "confidence": 0.8,
                "reasoning": "r",
                "debate_converged": True
            })

        assert seen["committed"] == Decimal("200")
        issued = market_today(holder_rec.created_at)
        assert rec.tier_generation_note == (
            "No entry tiers: no budget. The committed cash is held by open "
            f"BUY tiers on: JPM (issued {issued}).")
        assert str(holder.id) not in rec.tier_generation_note


class TestMarketCalendar:

    def test_market_today_uses_the_new_york_midnight(self):
        from shared.market_calendar import market_today

        cases = {
            "before-the-new-york-midnight": (
                datetime(2026, 10, 1, 3, 59, tzinfo=UTC), date(2026, 9, 30)),
            "at-the-new-york-midnight": (
                datetime(2026, 10, 1, 4, 0, tzinfo=UTC), date(2026, 10, 1)),
            "naive-moment-is-utc": (
                datetime(2026, 12, 1, 4, 30), date(2026, 11, 30))
        }
        for case_id, (moment, expected) in cases.items():
            assert market_today(moment) == expected, case_id


class TestKnowledgeGraphGuards:

    async def test_triples_scenarios_and_non_english_kg_rows(
            self, monkeypatch):
        import ai.agents.portfolio as portfolio
        from ai.knowledge.archival import _parse_response

        def _t(**kw):
            base = {
                "source": "AMD",
                "source_type": "company",
                "relation": "competes_with",
                "target": "NVIDIA",
                "target_type": "company",
                "confidence": 0.8
            }
            base.update(kw)
            return base

        raw = json.dumps([
            _t(),
            _t(source=""),
            _t(target="   "),
            _t(source="x" * 201),
            _t(relation="r" * 101),
            _t(confidence=7),
            _t(confidence=-1),
            _t(confidence="high")
        ])
        triples = _parse_response(raw)
        assert [t.confidence for t in triples] == [0.8, 1.0, 0.0]

        entries = [
            SimpleNamespace(
                action="recompute_scenario", observation="   \n  ",
                action_input={
                    "ticker": "AMD",
                    "additional_shares": 5
                }),
            SimpleNamespace(
                action="recompute_scenario", observation="weight 12%\nmore",
                action_input={
                    "ticker": "AMD",
                    "additional_shares": 7
                })
        ]
        monkeypatch.setattr(portfolio, "_successful", lambda entry: True)
        lines = portfolio._computed_scenarios(entries, "AMD")
        assert lines == ["additional_shares=7 -> weight 12%"]

        from ai.knowledge import kg_context

        async def fake_fetch(
                entity_name, ticker_filter, session, viewer_user_id=None,
                events_expire_before=None):
            return [
                _kg_row("TSMC"),
                _kg_row(RUSSIAN_SENTENCE),
                _kg_row("Apple", relation=CYRILLIC)
            ]

        monkeypatch.setattr(kg_context, "fetch_kg_relations", fake_fetch)
        out = await kg_context.get_kg_context_for_ticker("AMD")
        assert out is not None
        assert "TSMC" in out
        assert "(1 distinct facts found)" in out
        assert CYRILLIC not in out

        async def all_russian_fetch(
                entity_name, ticker_filter, session, viewer_user_id=None,
                events_expire_before=None):
            return [_kg_row(RUSSIAN_SENTENCE)]

        monkeypatch.setattr(
            kg_context, "fetch_kg_relations", all_russian_fetch)
        assert await kg_context.get_kg_context_for_ticker("AMD") is None
        assert (
            await kg_context.get_kg_context_for_portfolio(["AMD", "MSFT"])
            is None)

        async def mixed_fetch(
                entity_name, ticker_filter, session, viewer_user_id=None,
                events_expire_before=None):
            if entity_name == "AMD":
                return [_kg_row(RUSSIAN_SENTENCE)]
            return [_kg_row("OpenAI"), _kg_row(RUSSIAN_SENTENCE)]

        monkeypatch.setattr(kg_context, "fetch_kg_relations", mixed_fetch)
        out = await kg_context.get_kg_context_for_portfolio(["AMD", "MSFT"])
        assert out is not None
        assert "'AMD'" not in out
        assert "OpenAI" in out
        assert CYRILLIC not in out
