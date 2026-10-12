import asyncio
import importlib
import inspect
import io
import json
import math
import re
import runpy
import shutil
import sys
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4
import ai.agents.loop as loop_module
import ai.knowledge.archival as archival_module
import ai.providers.base as provider_base_module
import ai.providers.fred as fred_module
import ai.providers.news_relevance as relevance_module
import ai.rag.indexing as indexing_module
import ai.rag.retriever as retriever_module
import ai.recommendation.position_sizing as sizing_module
import ai.tools.market_data_cache as cache_module
import ai.tools.portfolio_tool as portfolio_tool_module
import ai.tools.risk_tool as risk_tool_module
import ai.tools.technical_tool as technical_tool_module
import ai.tools.web_search_tool as web_search_tool_module
import httpx
import pytest
from ai.benchmark.metrics import max_drawdown
from ai.graph.state import AnalysisState, DebateState
from ai.knowledge import filing_records as frec
from ai.knowledge.extraction import _parse_response
from ai.knowledge.kg_context import format_kg_relations
from ai.llm.base import LLMResponse, LLMUsage
from ai.llm.json_fence import parse_json_list
from ai.providers import sec_edgar
from ai.providers.base import (FundamentalsSnapshot, MacroDataPoint,
                               NewsArticle, PriceBar, PriceQuote,
                               ProviderError, ProviderRequestError,
                               WebSearchResult)
from ai.providers.fred import FredProvider
from ai.rag.chunking import Chunk
from ai.rag.retriever import (FilingRetriever, RetrievedChunk,
                              _payload_content_date, _rank_lexically)
from ai.rag.visibility import ChunkVisibility
from ai.recommendation.engine import position_limit_line
from ai.recommendation.position_sizing import (TierSuggestion,
                                               _PositionContext,
                                               current_position_weight_pct,
                                               generate_position_sizing,
                                               tier_sizing_note)
from ai.tools.base import clamp_int, peer_ticker_label, peer_ticker_of
from ai.tools.filing_tool import SearchFilingsTool
from ai.tools.fundamentals_tool import GetFundamentalsTool
from ai.tools.indicators import (annualized_volatility, atr, bollinger_bands,
                                 ema_series, macd, rsi, sma)
from ai.tools.macro_tool import GetMacroIndicatorsTool
from ai.tools.market_tool import (GetRecentPriceHistoryTool,
                                  describe_high_volume_sessions)
from ai.tools.news_tool import SearchNewsTool, observation_yield
from ai.tools.portfolio_tool import (GetPortfolioCompositionTool,
                                     RecomputeScenarioTool,
                                     committed_buy_cash_elsewhere)
from ai.tools.research_archive_tool import SearchResearchArchiveTool
from ai.tools.risk_tool import GetVolatilityAndDrawdownTool
from ai.tools.technical_tool import GetTechnicalIndicatorsTool
from ai.tools.web_search_tool import WebSearchTool
from helpers import (_BUY, _P, _cash, _FlakyMarket, _msft_amd_holdings,
                     _StubGraph)
from domains.analysis.repository import AnalysisRepository
from domains.knowledge.model import AgentReport, KgEntity, KgRelation
from domains.knowledge.repository import KnowledgeGraphRepository
from domains.market.repository import PriceBarRepository
from shared.enums import (AgentRole, AnalysisType, DocumentCategory,
                          RecommendationAction)
from shared.exceptions import UpstreamServiceError

_SCRIPTS = (
    Path(__file__).resolve().parents[2] / "services" / "ai-service" /
    "scripts")
_RUSSIAN = (
    "\u041d\u0430 \u0444\u043e\u0440\u0443\u043c\u0435 "
    "\u043f\u0440\u0435\u0434\u0441\u0442\u0430\u0432\u0438\u043b\u0438 "
    "\u0431\u0440\u0435\u043d\u0434")
_CHINESE = (
    "\u82f9\u679c\u516c\u53f8\u53d1\u5e03\u4e86"
    "\u65b0\u4ea7\u54c1\u548c\u65b0\u670d\u52a1")


@dataclass
class _Row:
    filename: str
    form: str
    filing_date: date
    report_date: date | None
    records: dict[str, Any] = field(default_factory=dict)


class _Router:

    def __init__(self, script: list[str]) -> None:
        self._script = list(script)
        self.calls: list[Any] = []

    async def complete(self, tier, messages, **kwargs) -> LLMResponse:
        self.calls.append(messages)
        content = (
            self._script.pop(0) if len(self._script) > 1 else self._script[0])
        return LLMResponse(
            content=content, provider="fake", model="fake-model",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1))


class _FakeFundamentalProvider:

    def __init__(
            self, snapshot: FundamentalsSnapshot | None = None,
            error: ProviderError | None = None):
        self._snapshot = snapshot
        self._error = error

    async def get_fundamentals(self, ticker: str) -> FundamentalsSnapshot:
        if self._error is not None:
            raise self._error
        assert self._snapshot is not None
        return self._snapshot


class _FakeRetriever:

    def __init__(self, chunks: list[RetrievedChunk]):
        self.chunks = chunks
        self.calls: list[tuple[str, str | None]] = []

    async def retrieve(self, query: str, *, ticker: str | None = None):
        self.calls.append((query, ticker))
        return self.chunks


class _BrokenRetriever:

    async def retrieve(self, query: str, *, ticker: str | None = None):
        raise RuntimeError("qdrant unreachable")


def _archive_tool(
        retriever: Any, *, name: str = "search_prior_risk_notes",
        category: DocumentCategory = DocumentCategory.RISK_ANALYSIS,
        ticker: str = "MSFT") -> SearchResearchArchiveTool:
    return SearchResearchArchiveTool(
        name=name, description="d", category=category, ticker=ticker,
        retriever=retriever)


def _chunk(
        cid: str, doc: str, text: str = "text", date_: str = "2026-05-01",
        source: str | None = None) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=cid, text=text, score=1.0, category=DocumentCategory.FILING,
        ticker="AMD", document_id=doc, source=source, content_date=date_)


def _web_classified(fn):
    from ai.tools.web_search_tool import ClassifiedResult

    async def wrapper(query, results, **kwargs):
        kept = await fn(query, results)
        return [ClassifiedResult(r, "unclassified") for r in kept]

    return wrapper


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _bar(day: int, close: float, volume: float) -> PriceBar:
    return PriceBar(
        ticker="MSFT", interval="1d",
        timestamp=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(days=day),
        open=close, high=close + 1, low=close - 1, close=close, volume=volume,
        source="fake")


class _FakeWebSearchProvider:

    def __init__(
            self, results: list[WebSearchResult] | None = None,
            error: Exception | None = None):
        self._results = results or []
        self._error = error

    async def search(self, query, *, limit=5):
        if self._error:
            raise self._error
        return self._results


def _fund_provider(ticker: str, spec: Any) -> _FakeFundamentalProvider:
    if isinstance(spec, ProviderError):
        return _FakeFundamentalProvider(error=spec)
    source, fields = spec
    return _FakeFundamentalProvider(
        FundamentalsSnapshot(
            ticker=ticker, as_of=datetime.now(UTC), source=source, **fields))


class _FakeMarketProvider:
    PRICES: ClassVar[dict[str, float]] = {"AAPL": 150.0, "MSFT": 300.0}

    async def get_current_price(self, ticker: str) -> PriceQuote:
        return PriceQuote(
            ticker=ticker, price=self.PRICES[ticker], as_of=datetime.now(UTC),
            source="fake")


class _FlatPriceMarket:

    async def get_current_price(self, ticker: str) -> PriceQuote:
        return PriceQuote(
            ticker=ticker, price=Decimal("100"), as_of=datetime.now(UTC),
            source="fake")


async def _aapl_msft_holdings(portfolio_id: str) -> list[dict]:
    return [{
        "id": "1",
        "portfolio_id": portfolio_id,
        "ticker": "AAPL",
        "quantity": 10,
        "average_cost": 120.0
    }, {
        "id": "2",
        "portfolio_id": portfolio_id,
        "ticker": "MSFT",
        "quantity": 5,
        "average_cost": 250.0
    }]


async def _broken_cash(portfolio_id: str):
    raise UpstreamServiceError("core-service unreachable")


@pytest.fixture
def msft_only_portfolio(monkeypatch):

    async def _holdings(portfolio_id: str) -> list[dict[str, Any]]:
        return [{"ticker": "MSFT", "quantity": 10.0, "average_cost": 90.0}]

    async def _cash_balance(portfolio_id: str) -> float:
        return 1000.0

    monkeypatch.setattr(
        portfolio_tool_module, "get_portfolio_holdings", _holdings)
    monkeypatch.setattr(
        portfolio_tool_module, "get_portfolio_cash_balance", _cash_balance)


@pytest.fixture
def committed_cash_backend(monkeypatch):
    monkeypatch.setattr(
        portfolio_tool_module, "get_portfolio_holdings", _msft_amd_holdings)
    monkeypatch.setattr(
        portfolio_tool_module, "get_portfolio_cash_balance", _cash)
    seen: list[str] = []

    async def _committed(portfolio_id: str, ticker: str) -> Decimal | None:
        seen.append(ticker)
        return Decimal("7697.27")

    monkeypatch.setattr(
        portfolio_tool_module, "committed_buy_cash_elsewhere", _committed)
    return seen


class _CachedBar:

    def __init__(
            self, timestamp: datetime, close: float,
            fetched_at: datetime) -> None:
        self.timestamp = timestamp
        self.close = close
        self.fetched_at = fetched_at


class _MarketService:

    def __init__(
            self, cached: list[_CachedBar], refreshed: list[_CachedBar],
            written: int) -> None:
        self.cached = cached
        self.refreshed = refreshed
        self.written = written
        self.refresh_calls: list[int] = []

    def __call__(self, session: Any) -> "_MarketService":
        return self

    async def get_recent_prices(self, ticker: str,
                                interval: str) -> list[_CachedBar]:
        if self.refresh_calls and self.refreshed is not None:
            return self.refreshed
        return self.cached

    async def refresh_from_external_provider(
            self, ticker: str, *, interval: str, lookback_days: int,
            provider: Any = None) -> int:
        self.refresh_calls.append(lookback_days)
        return self.written


class _Session:

    async def commit(self) -> None:
        pass


class _SessionContext:

    async def __aenter__(self) -> _Session:
        return _Session()

    async def __aexit__(self, *args: Any) -> bool:
        return False


def _weekday_bars(now: datetime, days: int) -> list[_CachedBar]:
    fetched = now - timedelta(minutes=1)
    bars = []
    for i in range(days + 1):
        ts = now - timedelta(days=i)
        if ts.weekday() >= 5:
            continue
        bars.append(_CachedBar(ts, 100.0 + i, fetched))
    return bars


_CACHE_NOW = datetime(2026, 9, 29, 22, 0, tzinfo=UTC)


class _CpiProvider:

    def __init__(self, points: list[MacroDataPoint]) -> None:
        self.points = points
        self.lookbacks: list[int] = []

    async def get_indicator(self, series: str, *,
                            lookback_days: int = 365) -> list[MacroDataPoint]:
        self.lookbacks.append(lookback_days)
        return self.points


def _monthly_cpi(first: date, count: int, start: float,
                 step: float) -> list[MacroDataPoint]:
    points = []
    year, month = first.year, first.month
    for i in range(count):
        points.append(
            MacroDataPoint(
                series_id="CPIAUCSL",
                label="Consumer Price Index (All Urban Consumers)",
                value=round(start + step * i,
                            3), unit=None, observed_at=date(year, month,
                                                            1), source="fred"))
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return points


class _Stop(Exception):
    pass


_RAW_FILING = (
    "(Dollars in millions, except per share data)\n"
    "During the three months ended June 30, 2026, we increased the "
    "reach-forward loss by $ 280 due to higher estimated costs required to "
    "complete the structural work.\n"
    "Restructuring and restructuring-related charges were $ 68 million in "
    "the three months ended June 30, 2026.\n"
    "As of June 30, 2026, net unrealized gains from marketable equity "
    "securities were $ 425 million.\n"
    "Basic earnings per share were $ 0.67 for the three months ended June "
    "30, 2026.\n"
    "If the IRS prevails, the Company estimates $ 14 billion in potential "
    "incremental tax and interest liabilities for 2010 through 2025.\n"
    "The Tax Court held oral argument on June 25, 2026 and a decision is "
    "expected in the first half of 2027.\n"
    "Qualcomm's breach of contract lawsuit against Arm is pending with "
    "trial scheduled to begin on October 5, 2026.\n")


def _legal(**extra: Any) -> dict[str, Any]:
    return {
        "name": "IRS tax litigation",
        "status": "pending",
        "status_date": None,
        "detail": None,
        "quote": "If the IRS prevails, the Company estimates $ 14 billion "
        "in potential incremental tax and interest liabilities",
        **extra
    }


def _fact(
        start: str, end: str, val: float, form: str = "10-Q",
        filed: str = "2026-07-29") -> dict[str, Any]:
    return {
        "start": start,
        "end": end,
        "val": val,
        "form": form,
        "filed": filed
    }


def _facts(**tags: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "facts": {
            "us-gaap": {
                t: {
                    "units": {
                        "USD": f
                    }
                }
                for t, f in tags.items()
            }
        }
    }


_KO_FACTS = _facts(
    Revenues=[
        _fact(
            "2025-01-01", "2025-12-31", 47.1e9, form="10-K",
            filed="2026-02-20"),
        _fact("2025-03-29", "2025-06-27", 12.5e9),
        _fact("2026-04-04", "2026-07-03", 13.4e9),
        _fact("2024-12-28", "2025-06-27", 23.6e9),
        _fact("2026-01-01", "2026-07-03", 25.9e9)
    ], OperatingIncomeLoss=[
        _fact("2025-03-29", "2025-06-27", 4.3e9),
        _fact("2026-04-04", "2026-07-03", 4.7e9),
        _fact("2024-12-28", "2025-06-27", 7.6e9),
        _fact("2026-01-01", "2026-07-03", 9.06e9)
    ], NetIncomeLoss=[
        _fact("2025-03-29", "2025-06-27", 3.8e9),
        _fact("2026-04-04", "2026-07-03", 4.0e9)
    ])


def _sizing_state(**overrides: Any) -> AnalysisState:
    values: dict[str, Any] = {
        "analysis_id": "a1",
        "user_id": "u1",
        "ticker": "MSFT",
        "analysis_type": AnalysisType.FULL
    }
    values.update(overrides)
    return AnalysisState(**values)


class _PricedMarket:

    def __init__(self, prices: dict[str, str]) -> None:
        self.prices = prices

    async def get_current_price(self, ticker):
        return PriceQuote(
            ticker=ticker, price=Decimal(self.prices[ticker]),
            as_of=datetime.now(UTC), source="fake")


class _NoMarket:

    async def get_current_price(self, ticker: str):
        raise AssertionError("no lookup expected for a low-confidence BUY")


@pytest.fixture
def msft_amd_sizing_env(monkeypatch):

    async def _holdings(portfolio_id):
        return [{
            "ticker": "MSFT",
            "quantity": 20.0
        }, {
            "ticker": "AMD",
            "quantity": 5.0
        }]

    async def _cash_balance(portfolio_id):
        return 7699.0

    market = _PricedMarket({"MSFT": "500.00", "AMD": "600.00"})
    monkeypatch.setattr(
        sizing_module, "get_market_data_provider", lambda: market)
    monkeypatch.setattr(
        portfolio_tool_module, "get_portfolio_holdings", _holdings)
    monkeypatch.setattr(
        sizing_module, "get_portfolio_cash_balance", _cash_balance)
    monkeypatch.setattr(
        sizing_module.settings, "MAX_POSITION_WEIGHT_PCT", 50.0)
    monkeypatch.setattr(sizing_module.settings, "TIER_MIN_NOTIONAL_USD", 100.0)
    monkeypatch.setattr(
        sizing_module.settings, "TIER_MIN_NOTIONAL_PORTFOLIO_FRACTION", 0.01)


def _tiers(pairs) -> str:
    return json.dumps({
        "window_days": 14,
        "tiers": [{
            "target_price": p,
            "quantity": q
        } for p, q in pairs]
    })


def _position(
        total: str | None, held: str = "0",
        value: str = "0") -> _PositionContext:
    net = Decimal(total) if total is not None else None
    return _PositionContext(
        held_quantity=Decimal(held), position_value=Decimal(value),
        total_net_value=net,
        current_weight_pct=(Decimal(value) / net * 100) if net else None,
        unpriced=[] if net is not None else ["AMD"])


class _FakeModels:

    def __init__(self, delay: float) -> None:
        self.delay = delay

    async def generate_content(self, **kwargs: Any) -> Any:
        await asyncio.sleep(self.delay)

        class _Usage:
            thoughts_token_count = None
            candidates_token_count = 42

        class _Resp:
            usage_metadata = _Usage()
            candidates: ClassVar[list[Any]] = []

        return _Resp()


class _FakeClient:

    def __init__(self, delay: float) -> None:
        self.aio = type("Aio", (), {"models": _FakeModels(delay)})()


def _seed_tree(tmp_path) -> Any:
    root = tmp_path / "seed"
    (root / "earnings_call").mkdir(parents=True)
    for name in ("META_earnings_call_q2_2026.txt",
                 "AMD_earnings_call_q2_2026.txt"):
        (root / "earnings_call" / name).write_text("call")
    return root


def _core(stored: dict[str, list[str]], seen: list[httpx.Request]):

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            assert request.url.path == "/internal/documents/filenames"
            assert request.url.params["category"] == "earnings_call"
            names = stored.get(request.url.params["ticker"], [])
            return httpx.Response(200, json=names)
        return httpx.Response(
            201, json={
                "id": "d1",
                "status": "uploaded",
                "duplicate": False
            })

    return _handler


class TestSearchFilingsTool:

    @pytest.mark.asyncio
    async def test_search_tools_query_only_the_bound_ticker(self):
        retriever = _FakeRetriever([])
        tool = SearchFilingsTool(ticker="MSFT", retriever=retriever)
        await tool.run(query="margins", ticker="AAPL")
        assert retriever.calls == [("margins", "MSFT")]

        retriever = _FakeRetriever([])
        tool = _archive_tool(retriever)
        await tool.run(query="litigation exposure")
        await tool.run(query="litigation exposure", ticker="AAPL")
        assert retriever.calls == [("litigation exposure", "MSFT"),
                                   ("litigation exposure", "MSFT")]

    @pytest.mark.asyncio
    async def test_old_filing_gets_stale_flag(self):
        chunks = [
            RetrievedChunk(
                chunk_id="c1", text="Guidance for next fiscal year outlook.",
                score=0.9, category=DocumentCategory.FILING, ticker="AAPL",
                source="10-K FY2022", content_date="2022-11-01")
        ]
        tool = SearchFilingsTool(
            ticker="AAPL", retriever=_FakeRetriever(chunks))
        result = await tool.run(query="guidance", ticker="AAPL")
        assert "STALE FILING" in result

    @pytest.mark.asyncio
    async def test_non_english_passage_dropped_and_source_shown(self):
        tool = SearchFilingsTool(
            ticker="AMD", retriever=_FakeRetriever([
                _chunk("1", "d1", f"{_RUSSIAN} {_RUSSIAN}"),
                _chunk(
                    "2", "d2", "Revenue rose 18% in the quarter", "2026-08-05",
                    source="AMD_10-Q_2026-08-05")
            ]))
        obs = await tool.run("revenue", ticker="AMD")
        assert "(1 passages)" in obs
        assert "[AMD_10-Q_2026-08-05] (filed 2026-08-05)" in obs
        assert "\u041d" not in obs

    def test_search_tools_have_no_ticker_argument(self):
        assert set(SearchFilingsTool.parameters["properties"]) == {"query"}
        assert '"ticker"' not in SearchFilingsTool.description
        assert set(
            SearchResearchArchiveTool.parameters["properties"]) == {"query"}

    @pytest.mark.asyncio
    async def test_retriever_failure_becomes_observation_not_crash(self):
        cases = {
            "filings": (
                lambda: SearchFilingsTool(
                    ticker="AAPL", retriever=_BrokenRetriever()), {},
                "Filing search failed"),
            "research-archive": (
                lambda: _archive_tool(_BrokenRetriever(), ticker="AAPL"), {
                    "ticker": "AAPL"
                }, "Research archive search failed")
        }
        for case_id, (make_tool, kwargs, expected) in cases.items():
            result = await make_tool().run(query="anything", **kwargs)
            assert expected in result, case_id


class TestSearchResearchArchiveTool:

    @pytest.mark.asyncio
    async def test_archive_drops_non_english_passages(self):
        retriever = _FakeRetriever([
            RetrievedChunk(
                chunk_id="1", text="Margins held steady.", score=0.9,
                category=DocumentCategory.NEWS, ticker="AAPL"),
            RetrievedChunk(
                chunk_id="2", text=_RUSSIAN, score=0.8,
                category=DocumentCategory.NEWS, ticker="AAPL")
        ])
        tool = _archive_tool(
            retriever, name="search_prior_news_notes",
            category=DocumentCategory.NEWS, ticker="AAPL")
        text = await tool.run("q", ticker="AAPL")
        assert "(1 passages)" in text and "Margins held steady." in text
        assert _RUSSIAN not in text

    @pytest.mark.asyncio
    async def test_transcript_passages_name_their_call(self):
        dated = RetrievedChunk(
            chunk_id="c1",
            text="We expect capital expenditures of $70 billion.", score=0.9,
            category=DocumentCategory.EARNINGS_CALL, ticker="META",
            source="META_earnings_call_q2_2026",
            content_date="2026-07-30T00:00:00")
        undated = RetrievedChunk(
            chunk_id="c2", text="Legal headwinds in the EU remain.", score=0.8,
            category=DocumentCategory.EARNINGS_CALL, ticker="META",
            source="META_earnings_call_q1_2026")

        def transcripts(chunks):
            return _archive_tool(
                _FakeRetriever(chunks),
                name="search_earnings_call_transcripts",
                category=DocumentCategory.EARNINGS_CALL, ticker="META")

        out = await transcripts([dated, undated]).run(query="q")
        assert "[META_earnings_call_q2_2026] (call date 2026-07-30):" in out
        assert (
            "[META_earnings_call_q1_2026] (call date not recorded; the "
            "file name may give the quarter):" in out)
        assert "STALE" not in out
        out = await transcripts([]).run(query="q")
        assert out.startswith("No earnings call transcript passages found")
        assert "do not retry" in out and len(out) < 200


class TestWebSearchTool:

    @pytest.mark.asyncio
    async def test_empty_or_failed_search_is_a_readable_observation(self):
        cases = {
            "no-results": (
                _FakeWebSearchProvider([]), "No web search results"),
            "provider-error": (
                _FakeWebSearchProvider(
                    error=ProviderRequestError("upstream down")),
                "Web search failed")
        }
        for case_id, (provider, expected) in cases.items():
            tool = WebSearchTool(provider=provider)
            observation = await tool.run("anything")
            assert expected in observation, case_id

    @pytest.mark.asyncio
    async def test_stale_source_is_flagged_and_fake_url_dates_ignored(self):
        provider = _FakeWebSearchProvider([
            WebSearchResult(
                title="TEXT-FOMC statement from Aug 9 meeting",
                url="https://www.federalreserve.gov/monetary20070807a.htm",
                snippet="inflation has moderated...",
                published_at=datetime(2007, 8, 7, tzinfo=UTC))
        ])
        observation = await WebSearchTool(provider=provider
                                          ).run("FOMC statement")
        assert "STALE SOURCE" in observation
        assert "2007-08-07" in observation
        assert (
            web_search_tool_module._extract_date_from_url(
                "https://example.com/2026/13/40/not-a-real-date.html") is None)

    @pytest.mark.asyncio
    async def test_web_search_drops_non_english(self, monkeypatch):
        seen: list[int] = []

        async def _relevance(query, results, *, llm_router=None):
            seen.append(len(results))
            return results

        provider = _FakeWebSearchProvider([
            WebSearchResult(
                title="Fed holds rates", url="https://r.com/a",
                snippet="The Fed held."),
            WebSearchResult(
                title=_CHINESE, url="https://x.cn/b", snippet="...")
        ])
        monkeypatch.setattr(
            web_search_tool_module, "classify_results",
            _web_classified(_relevance))
        text = await WebSearchTool(provider=provider).run("fed")
        assert seen == [1]
        assert "Fed holds rates" in text and "x.cn" not in text


class TestNewsTools:

    @pytest.mark.asyncio
    async def test_news_all_non_english_reads_as_zero_yield(self):

        class _Provider:

            async def search_news(self, query, *, since=None, limit=20):
                return [
                    NewsArticle(
                        title=_RUSSIAN, source_name="X", url="u",
                        published_at=datetime.now(UTC))
                ]

        text = await SearchNewsTool(provider=_Provider()).run("AAPL")
        assert text.startswith("No news articles found")
        assert observation_yield(text) == 0

    @pytest.mark.asyncio
    async def test_unknown_ticker_is_cached_but_a_failure_is_not(
            self, monkeypatch):
        calls = {"n": 0}

        async def _titles() -> dict[str, str]:
            calls["n"] += 1
            return {"AAPL": "Apple Inc."}

        monkeypatch.setattr(relevance_module, "_company_name_cache", {})
        monkeypatch.setattr(relevance_module, "load_ticker_to_title", _titles)
        monkeypatch.setattr(
            relevance_module.settings, "SEC_EDGAR_USER_AGENT",
            "Test t@example.com")

        lookup = relevance_module.lookup_company_name
        assert await lookup("ZZZZ") is None
        assert await lookup("ZZZZ") is None
        assert await lookup("AAPL") == "Apple Inc."
        assert await lookup("AAPL") == "Apple Inc."
        assert calls["n"] == 2

        calls = {"n": 0}

        async def _failing_titles() -> dict[str, str]:
            calls["n"] += 1
            raise ProviderRequestError("down")

        monkeypatch.setattr(relevance_module, "_company_name_cache", {})
        monkeypatch.setattr(
            relevance_module, "load_ticker_to_title", _failing_titles)

        assert await relevance_module.lookup_company_name("AAPL") is None
        assert await relevance_module.lookup_company_name("AAPL") is None
        assert calls["n"] == 2

    def test_relevance_reply_parses_object_and_bare_array_shapes(self):
        item = {"index": 0, "reasoning": "r", "relevant": True}
        assert parse_json_list(
            '{"relevant": [{"index": 0, "reasoning": "r", '
            '"relevant": true}]}', log_event="t") == [item]
        assert parse_json_list(
            '[{"index": 0, "reasoning": "r", "relevant": true}]',
            log_event="t") == [item]
        assert (
            parse_json_list('```json\n{"relevant": []}\n```',
                            log_event="t") == [])
        assert parse_json_list('{"something": 1}', log_event="t") is None
        assert parse_json_list("not json", log_event="t") is None


class TestGetFundamentalsTool:

    @pytest.mark.asyncio
    async def test_observation(self):
        cases = {
            "second-provider-fills-missing-fields": (
                "AAPL", [
                    (
                        "sec_edgar", {
                            "gross_margin": 0.784,
                            "operating_margin": 0.53,
                            "net_margin": 0.45,
                            "revenue_growth_yoy": 0.147
                        }),
                    ("yahoo_finance", {
                        "pe_ratio": 35.4,
                        "pb_ratio": 52.1
                    })
                ], [
                    "P/E=35.40", "P/B=52.10",
                    "gross margin (annual, latest FY)=78.4%",
                    "revenue growth YoY (annual, latest FY vs prior FY)=14.7%"
                ], []),
            "first-provider-fields-are-not-overwritten": (
                "AAPL", [("sec_edgar", {
                    "pe_ratio": 20.0
                }), ("yahoo_finance", {
                    "pe_ratio": 99.9
                })], ["P/E=20.00"], ["99.9"]),
            "all-providers-failing-is-reported": (
                "AAPL", [
                    ProviderRequestError("down"),
                    ProviderRequestError("also down")
                ], [
                    "Fundamentals lookup failed for 'AAPL' from all 2 "
                    "provider(s)"
                ], []),
            "gross-margin-below-operating-is-discarded": (
                "JPM", [(
                    "sec_edgar", {
                        "operating_margin": 0.504,
                        "net_margin": 0.313,
                        "revenue_growth_yoy": 0.028
                    }),
                        (
                            "yahoo_finance", {
                                "gross_margin": 0.0,
                                "pe_ratio": 15.27,
                                "pb_ratio": 2.68
                            })],
                [
                    "operating margin (annual, latest FY)=50.4%", "discarded",
                    "came out lower than, or equal to, operating margin"
                ], ["gross margin=", "gross margin ("]),
            "gross-margin-equal-to-operating-is-discarded": (
                "BA", [(
                    "sec_edgar", {
                        "gross_margin": 0.048,
                        "operating_margin": 0.048,
                        "net_margin": 0.021,
                        "revenue_growth_yoy": 0.011
                    })], [
                        "operating margin (annual, latest FY)=4.8%",
                        "discarded",
                        "came out lower than, or equal to, operating margin"
                    ], ["gross margin=", "gross margin ("]),
            "period-labels-follow-the-provider-of-each-field": (
                "GOOGL", [(
                    "sec_edgar", {
                        "operating_margin": 0.32,
                        "net_margin": 0.29,
                        "revenue_growth_yoy": 0.14
                    }),
                          (
                              "yahoo_finance", {
                                  "gross_margin": 0.59,
                                  "pe_ratio": 22.0,
                                  "revenue_growth_yoy": 0.2
                              })],
                [
                    "gross margin (TTM)=59.0%",
                    "operating margin (annual, latest FY)=32.0%",
                    "net margin (annual, latest FY)=29.0%",
                    "revenue growth YoY (annual, latest FY vs prior FY)=14.0%"
                ], []),
            "yahoo-only-growth-is-labelled-quarterly": (
                "GOOGL", [(
                    "yahoo_finance", {
                        "revenue_growth_yoy": 0.12,
                        "net_margin": 0.2
                    })], [
                        "revenue growth YoY (quarterly, latest quarter vs "
                        "year-ago "
                        "quarter)=12.0%", "net margin (TTM)=20.0%"
                    ], [])
        }
        for case_id, (ticker, specs, present, absent) in cases.items():
            await self._check_observation(
                case_id, ticker, specs, present, absent)

    async def _check_observation(
            self, case_id, ticker, specs, present, absent):
        providers = [_fund_provider(ticker, spec) for spec in specs]
        observation = await GetFundamentalsTool(providers=providers
                                                ).run(ticker)
        for text in present:
            assert text in observation, case_id
        for text in absent:
            assert text not in observation, case_id


class TestPortfolioTools:

    @pytest.mark.asyncio
    async def test_no_portfolio_id_returns_clear_message(self):
        cases = {
            "composition": (GetPortfolioCompositionTool, {}),
            "scenario": (
                RecomputeScenarioTool, {
                    "ticker": "AAPL",
                    "additional_shares": 10
                })
        }
        for case_id, (tool_class, kwargs) in cases.items():
            result = await tool_class(portfolio_id=None).run(**kwargs)
            assert "No portfolio is attached" in result, case_id

    @pytest.mark.asyncio
    async def test_core_service_failures_are_reported_never_hidden(
            self, monkeypatch):

        async def _broken_holdings(portfolio_id: str):
            raise UpstreamServiceError("core-service unreachable")

        monkeypatch.setattr(
            portfolio_tool_module, "get_portfolio_holdings", _broken_holdings)
        tool = GetPortfolioCompositionTool(
            portfolio_id="p1", market_provider=_FakeMarketProvider())
        result = await tool.run()
        assert "lookup failed" in result

        monkeypatch.setattr(
            portfolio_tool_module, "get_portfolio_holdings",
            _aapl_msft_holdings)
        monkeypatch.setattr(
            portfolio_tool_module, "get_portfolio_cash_balance", _broken_cash)
        composition = await GetPortfolioCompositionTool(
            portfolio_id="p1", market_provider=_FakeMarketProvider()).run()
        assert "50.0%" in composition
        assert "cash unknown" in composition
        scenario = await RecomputeScenarioTool(
            portfolio_id="p1", market_provider=_FakeMarketProvider()).run(
                ticker="AAPL", additional_shares=10)
        assert "unavailable" in scenario
        assert "%" not in scenario

    @pytest.mark.asyncio
    async def test_missing_live_price_marks_or_refuses_the_total(
            self, _portfolio_backend):
        market = _FlakyMarket(failing={"AMD"})
        scenario = await RecomputeScenarioTool("p1",
                                               market_provider=market).run(
                                                   ticker="MSFT",
                                                   additional_shares=20)
        assert scenario.startswith(
            "Scenario recompute failed: live price unavailable for AMD; "
            "total net value cannot be computed")
        assert "112.9%" not in scenario and "%" not in scenario
        assert loop_module.tool_reported_error(scenario)

        composition = await GetPortfolioCompositionTool(
            "p1", market_provider=_FlakyMarket(failing={"AMD"})).run()
        header = composition.splitlines()[0]
        assert "PARTIAL" in header and "AMD" in header
        assert "weight=n/a" in composition
        assert "%" not in composition.split("\n", 1)[1]

    @pytest.mark.asyncio
    async def test_scenario_share_bounds(self, msft_only_portfolio):
        cases = {
            "selling-up-to-the-held-quantity": (
                "MSFT", -10, "Scenario: MSFT -10 shares", []),
            "selling-an-unheld-ticker": (
                "AMD", -1, "Scenario recompute failed", []),
            "nan-shares": (
                "MSFT", math.nan, "Scenario recompute failed", ["finite"]),
            "infinite-shares": (
                "MSFT", math.inf, "Scenario recompute failed", ["finite"]),
            "non-numeric-shares": (
                "MSFT", "lots", "Scenario recompute failed", ["finite"])
        }
        for case_id, (ticker, shares, prefix, fragments) in cases.items():
            tool = RecomputeScenarioTool(
                "p1", market_provider=_FlatPriceMarket())
            result = await tool.run(ticker=ticker, additional_shares=shares)
            assert result.startswith(prefix), case_id
            for text in fragments:
                assert text in result, case_id

    @pytest.mark.asyncio
    async def test_buy_scenario_shows_the_remaining_buy_budget(
            self, committed_cash_backend):
        tool = RecomputeScenarioTool("p1", market_provider=_FlakyMarket())
        small = await tool.run(ticker="AMD", additional_shares=1)
        large = await tool.run(ticker="AMD", additional_shares=2)

        assert "cost=533.60, available_cash=7698.95, shortfall=0.00" in small
        assert small.endswith(
            "committed_to_other_open_buy_tiers=7697.27, buy_budget=1.68, "
            "budget_shortfall=531.92")
        assert large.endswith("budget_shortfall=1065.52")
        assert committed_cash_backend == ["AMD"]

    @pytest.mark.asyncio
    async def test_committed_cash_excludes_the_scenario_ticker(self, rows):
        pf = uuid4()
        await rows.recommendation(
            ticker="DIS", portfolio_id=pf, action="buy", confidence=0.73,
            tiers=[(_BUY, "101", "10", _P), (_BUY, "97.5", "7", _P)])
        await rows.recommendation(
            ticker="NVDA", portfolio_id=pf, action="buy", confidence=0.8,
            tiers=[(_BUY, "100", "2", _P)])

        assert await committed_buy_cash_elsewhere(
            str(pf), "NVDA") == (Decimal("1692.5"))
        assert await committed_buy_cash_elsewhere(str(pf),
                                                  "DIS") == (Decimal("200"))
        assert await committed_buy_cash_elsewhere("not-a-uuid", "DIS") is None

    def test_clamp_int(self):
        cases = {
            "negative": (-5, 1),
            "zero": (0, 1),
            "too-large": (10_000, 730),
            "numeric-string": ("90", 90),
            "text": ("abc", 30),
            "none": (None, 30),
            "bool": (True, 30),
            "float": (45.7, 45)
        }
        for case_id, (value, expected) in cases.items():
            got = clamp_int(value, default=30, lo=1, hi=730)
            assert got == expected, case_id


class TestIndicators:

    def test_insufficient_data_returns_none(self):
        cases = {
            "sma": lambda: sma([1, 2], 5),
            "rsi": lambda: rsi([1.0, 2.0], period=14),
            "macd": lambda: macd([1.0] * 10, slow=26, signal=9),
            "bollinger": lambda: bollinger_bands([1.0, 2.0], period=20),
            "atr": lambda: atr([1.0], [1.0], [1.0], period=14),
            "annualized-volatility": lambda: annualized_volatility(
                [100.0, 105.0]),
            "max-drawdown": lambda: max_drawdown([100.0])
        }
        for case_id, compute in cases.items():
            assert compute() is None, case_id

    def test_short_ema_is_empty_and_flat_rsi_is_neutral(self):
        assert ema_series([1, 2], 5) == []
        assert rsi([100.0] * 20, period=14) == pytest.approx(50.0)

    def test_high_volume_sessions_carry_their_direction(self):
        bars = [
            _bar(0, 500.0, 20e6),
            _bar(1, 497.75, 21e6),
            _bar(2, 493.78, 39.6e6),
            _bar(3, 495.0, 25e6),
            _bar(4, 495.0, 22e6)
        ]
        lines = describe_high_volume_sessions(bars, shown=4)
        assert lines[0].startswith("Highest-volume sessions shown")
        assert lines[1] == (
            "- 2026-09-03: volume 39,600,000, down day (-0.80%, previous "
            "session 497.75 -> 493.78)")
        assert "up day" in lines[2] and "2026-09-04" in lines[2]
        assert "flat day" in lines[3] and "2026-09-05" in lines[3]
        assert all("2026-09-01" not in line for line in lines)
        no_volume = [_bar(0, 1.0, 0), _bar(1, 2.0, 0)]
        assert describe_high_volume_sessions(no_volume, 2) == []

    @pytest.mark.asyncio
    async def test_technical_tool_prints_latest_close_and_positions(
            self, monkeypatch):
        closes = [100.0 + i
                  for i in range(40)] + [139.0 - 2 * i for i in range(1, 21)]
        start = datetime(2026, 6, 1, tzinfo=UTC)
        bars = [
            PriceBar(
                ticker="AAPL", interval="1d",
                timestamp=start + timedelta(days=i), open=c, high=c + 1,
                low=c - 1, close=c, volume=1000, source="fake")
            for i, c in enumerate(closes)
        ]

        async def _fake_bars(ticker, *, interval, lookback_days, provider):
            return bars

        monkeypatch.setattr(
            technical_tool_module, "get_bars_cache_first", _fake_bars)
        text = await GetTechnicalIndicatorsTool(provider=object()).run("AAPL")
        assert "Latest close: 99.00 (bar date 2026-07-30)" in text
        sma20 = sum(closes[-20:]) / 20
        sma50 = sum(closes[-50:]) / 50
        assert sma20 > 99.0 and sma50 > 99.0
        assert (
            f"Price vs SMA(20): below ({(99.0 - sma20) / sma20 * 100:+.1f}%)"
        ) in text
        assert (
            f"Price vs SMA(50): below ({(99.0 - sma50) / sma50 * 100:+.1f}%)"
        ) in text

    @pytest.mark.asyncio
    async def test_volatility_tool_reports_too_few_bars(self, monkeypatch):
        bars = [
            PriceBar(
                ticker="AAPL", interval="1d",
                timestamp=datetime(2026, 7, 1, tzinfo=UTC), open=100, high=100,
                low=100, close=100, volume=1000, source="fake")
        ]

        async def fake_get_bars_cache_first(
                ticker, *, interval="1d", lookback_days=90, provider=None):
            return bars

        monkeypatch.setattr(
            risk_tool_module, "get_bars_cache_first",
            fake_get_bars_cache_first)
        tool = GetVolatilityAndDrawdownTool(provider=object())
        result = await tool.run("AAPL", lookback_days=90)
        assert "Not enough price history" in result


class TestPeerTickerResults:

    def test_share_class_forms_are_one_ticker(self):
        cases = {
            "BRK.B-BRK-B-None": ("BRK.B", "BRK-B", None),
            "brk/b-BRK.B-None": (" brk/b", "BRK.B", None),
            "brk-a-BRK-B-BRK-A": ("brk-a", "BRK-B", "BRK-A"),
            "SHOP.TO-SHOP-SHOP.TO": ("SHOP.TO", "SHOP", "SHOP.TO"),
            "aapl-AAPL-None": ("aapl", "AAPL", None)
        }
        for case_id, (requested, analysed, peer) in cases.items():
            got = peer_ticker_of({"ticker": requested}, analysed)
            assert got == peer, case_id

    @pytest.mark.asyncio
    async def test_other_ticker_result_is_labelled(self, monkeypatch):
        bars = [
            PriceBar(
                ticker="AMD", interval="1d",
                timestamp=datetime(2026, 7, 1, tzinfo=UTC) + timedelta(days=i),
                open=100 + i, high=101 + i, low=99 + i, close=100 + i,
                volume=1000, source="fake") for i in range(5)
        ]

        async def fake_bars(
                ticker, *, interval="1d", lookback_days=90, provider=None):
            return bars

        monkeypatch.setattr(
            risk_tool_module, "get_bars_cache_first", fake_bars)
        tool = GetVolatilityAndDrawdownTool(
            provider=object(), analysis_ticker="NVDA")
        peer = await tool.run("amd")
        assert peer.endswith(peer_ticker_label("AMD", "NVDA"))
        assert "not for NVDA" in peer
        own = await tool.run(" nvda ")
        assert "PEER TICKER" not in own
        assert tool.peer_ticker({"ticker": "AMD"}) == "AMD"
        assert tool.peer_ticker({"ticker": "nvda"}) is None
        assert GetVolatilityAndDrawdownTool(provider=object()).peer_ticker(
            {"ticker": "AMD"}) is None

    @pytest.mark.asyncio
    async def test_unknown_price_interval_is_refused(self):
        tool = GetRecentPriceHistoryTool(provider=object())
        enum = tool.parameters["properties"]["interval"]["enum"]
        assert enum == ["1d", "1wk", "1mo"]
        text = await tool.run("AAPL", interval="5m")
        assert loop_module.tool_reported_error(text)
        assert "unsupported interval '5m'" in text


class TestMarketDataCache:

    @pytest.fixture(autouse=True)
    def _frozen_cache_clock(self, monkeypatch):
        monkeypatch.setattr(cache_module, "_utcnow", lambda: _CACHE_NOW)

    @pytest.mark.asyncio
    async def test_short_prefetch_does_not_satisfy_a_longer_request(
            self, monkeypatch):
        monkeypatch.setattr(
            cache_module, "SessionLocal", lambda: _SessionContext())
        refreshed = _weekday_bars(_CACHE_NOW, 90)
        service = _MarketService(
            _weekday_bars(_CACHE_NOW, 30), refreshed, written=len(refreshed))
        monkeypatch.setattr(cache_module, "MarketService", service)

        bars = await cache_module.get_bars_cache_first("AMD", lookback_days=90)

        assert service.refresh_calls == [90]
        assert len(bars) == len(refreshed)
        assert bars[0].timestamp < bars[-1].timestamp
        limit = inspect.signature(
            PriceBarRepository.list_recent).parameters["limit"]
        assert limit.default >= 730


class TestMacroData:

    @pytest.mark.asyncio
    async def test_fred_retries_5xx_and_transport_errors(self, monkeypatch):
        attempts: list[Any] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            attempts.append(request.extensions.get("timeout"))
            if len(attempts) == 1:
                return httpx.Response(503, json={})
            return httpx.Response(
                200, json={
                    "observations": [{
                        "date": "2026-08-01",
                        "value": "4.33"
                    }]
                })

        async def _no_sleep(_: float) -> None:
            return None

        monkeypatch.setattr(fred_module.settings, "FRED_API_KEY", "secret-key")
        monkeypatch.setattr(
            fred_module, "get_http_client", lambda: _mock_client(_handler))
        monkeypatch.setattr(provider_base_module.asyncio, "sleep", _no_sleep)

        points = await FredProvider().get_indicator("fed_funds_rate")

        assert points[0].value == 4.33
        assert len(attempts) == 2
        assert attempts[0]["read"] == 15.0

        calls = {"n": 0}

        def _timeout_handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.ReadTimeout("timed out", request=request)

        monkeypatch.setattr(fred_module.settings, "FRED_API_KEY", "k")
        monkeypatch.setattr(
            fred_module, "get_http_client",
            lambda: _mock_client(_timeout_handler))

        with pytest.raises(ProviderRequestError,
                           match="ReadTimeout: timed out"):
            await FredProvider().get_indicator("fed_funds_rate")
        assert calls["n"] == provider_base_module.DEFAULT_MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_fred_bad_body_raises_provider_request_error(
            self, monkeypatch):
        cases = {
            "non-json-body": httpx.Response(
                200, text="<html>maintenance</html>"),
            "malformed-observation": httpx.Response(
                200, json={"observations": [{
                    "value": "abc"
                }]})
        }
        monkeypatch.setattr(fred_module.settings, "FRED_API_KEY", "k")
        for response in cases.values():
            monkeypatch.setattr(
                fred_module, "get_http_client",
                lambda r=response: _mock_client(lambda request: r))
            with pytest.raises(ProviderRequestError):
                await FredProvider().get_indicator("cpi")

    @pytest.mark.asyncio
    async def test_short_cpi_range_says_so_and_other_series_are_unchanged(
            self):
        short = _CpiProvider(_monthly_cpi(date(2026, 3, 1), 6, 330.0, 0.5))
        text = await GetMacroIndicatorsTool(short).run("cpi")
        assert "Year-over-year change: not available from this range" in text
        dff = _CpiProvider([
            MacroDataPoint(
                series_id="DFF", label="Effective Federal Funds Rate (daily)",
                value=3.88, unit="%", observed_at=date(2026, 9,
                                                       24), source="fred")
        ])
        text = await GetMacroIndicatorsTool(dff).run("fed_funds_rate")
        assert "Year-over-year" not in text and dff.lookbacks == [365]


class TestRetrievalAndArchive:

    def test_lexical_rank_and_agent_note_dates(self):
        texts = [
            "cash flow statement", "risk factors: export controls on MI308",
            "overview"
        ]
        assert _rank_lexically("export controls", texts)[0] == 1
        assert _rank_lexically("zzz", texts) == [0, 1, 2]
        assert (
            _payload_content_date({
                "source": "agent:macro",
                "created_at": "2026-07-01T12:00:00+00:00"
            }) == "2026-07-01T12:00:00+00:00")
        assert (
            _payload_content_date({
                "source": "AMD_10-Q",
                "created_at": "2026-09-01"
            }) is None)
        assert (
            _payload_content_date({
                "content_date": "2026-08-05",
                "source": "agent:x"
            }) == "2026-08-05")

    @pytest.mark.asyncio
    async def test_filing_retriever_reserves_newest_filing(self, monkeypatch):

        class _Point:

            def __init__(
                    self, pid: str, doc: str, day: str, idx: int,
                    text: str) -> None:
                self.id = pid
                self.payload = {
                    "document_id": doc,
                    "content_date": day,
                    "chunk_index": idx,
                    "text": text,
                    "ticker": "AMD",
                    "category": "filing",
                    "source": f"AMD_{day}"
                }

        points = [
            _Point(f"k{i}", "k", "2026-02-04", i, f"10-K text {i}")
            for i in range(10)
        ]
        points += [
            _Point("q0", "q", "2026-08-05", 0, "Quarter overview"),
            _Point(
                "q1", "q", "2026-08-05", 1,
                "Data center revenue guidance raised")
        ]

        ten_k_points = [p for p in points if p.payload["document_id"] == "k"]

        class _Qdrant:

            async def query_points(self, **kwargs):
                return type("_R", (), {"points": ten_k_points})()

            async def retrieve(self, **kwargs):
                return []

            async def scroll(self, **kwargs):
                return points, None

        class _BM25:

            def search(self, query, top_k, **scope):
                return []

        async def _embed(q):
            return [0.1]

        monkeypatch.setattr(
            retriever_module, "get_qdrant_client", lambda: _Qdrant())
        monkeypatch.setattr(
            retriever_module, "get_bm25_index", lambda: _BM25())
        monkeypatch.setattr(retriever_module, "embed_query", _embed)
        retriever = FilingRetriever(top_k=8, use_query_expansion=False)

        out = await retriever.retrieve("revenue guidance", ticker="AMD")

        assert len(out) == 8
        assert [c.chunk_id for c in out[-2:]] == ["q1", "q0"]
        assert out[-1].content_date == "2026-08-05"
        pinned = await retriever.retrieve(
            "revenue guidance", ticker="AMD", sources=frozenset({"vector"}))
        assert all(c.document_id == "k" for c in pinned)

    @pytest.mark.asyncio
    async def test_non_english_report_and_triples_are_never_stored(
            self, monkeypatch):
        from ai.knowledge.extraction import KgTriple

        async def _fail(**kwargs):
            raise AssertionError("must not reach the duplicate check")

        monkeypatch.setattr(
            archival_module, "_find_near_duplicate_of_recent_report", _fail)
        await archival_module.archive_agent_report(
            analysis_id=uuid4(), role=AgentRole.MACRO, ticker="MSFT",
            content=" ".join([_RUSSIAN] * 20))

        async def _extract(*args, **kwargs):
            return [
                KgTriple(
                    source="MSFT", source_type="company", relation=_RUSSIAN,
                    target=_RUSSIAN, target_type="company", confidence=0.9)
            ]

        class _NoSession:

            def __call__(self):
                raise AssertionError("no DB write expected")

        monkeypatch.setattr(
            archival_module, "extract_entities_relations", _extract)
        monkeypatch.setattr(archival_module, "SessionLocal", _NoSession())
        await archival_module._extract_and_store_kg(
            report_id=uuid4(), content="x", ticker=None)

    @pytest.mark.asyncio
    async def test_archived_and_indexed_chunks_carry_a_content_date(
            self, monkeypatch):
        captured: dict[str, Any] = {}

        async def _no_dup(**kwargs):
            return None

        async def _embed(texts):
            return [[0.0] for _ in texts]

        async def _index(**kwargs):
            captured.update(kwargs)
            raise _Stop

        monkeypatch.setattr(
            archival_module, "_find_near_duplicate_of_recent_report", _no_dup)
        monkeypatch.setattr(archival_module, "embed_texts", _embed)
        monkeypatch.setattr(archival_module, "index_chunks", _index)
        with pytest.raises(_Stop):
            await archival_module.archive_agent_report(
                analysis_id=uuid4(), role=AgentRole.MACRO, ticker="MSFT",
                content="The Federal Reserve held rates steady this month. " *
                10)
        assert captured["content_date"] == (
            datetime.now(UTC).date().isoformat())
        assert captured["source"] == "agent:macro"

        upserts: list[Any] = []

        class _Qdrant:

            async def upsert(self, **kwargs):
                upserts.extend(kwargs["points"])

        class _BM25:

            def add_many(self, items):
                pass

        monkeypatch.setattr(
            indexing_module, "get_qdrant_client", lambda: _Qdrant())
        monkeypatch.setattr(indexing_module, "get_bm25_index", lambda: _BM25())
        macro_chunk = Chunk(
            text="t", chunk_index=0, category=DocumentCategory.MACRO_RESEARCH,
            metadata={"agent_role": "macro"})
        await indexing_module.index_chunks(
            document_id="d1", ticker="MSFT",
            category=DocumentCategory.MACRO_RESEARCH, chunks=[macro_chunk],
            embeddings=[[0.0]], visibility=ChunkVisibility.system(),
            source="agent:macro", content_date="2026-09-24")
        assert upserts[0].payload["content_date"] == "2026-09-24"
        upserts.clear()
        filing_chunk = Chunk(
            text="t", chunk_index=0, category=DocumentCategory.FILING,
            metadata={"content_date": None})
        await indexing_module.index_chunks(
            document_id="d2", ticker="AMD", category=DocumentCategory.FILING,
            chunks=[filing_chunk], embeddings=[[0.0]],
            visibility=ChunkVisibility.system(), content_date="2026-08-05")
        assert upserts[0].payload["content_date"] == "2026-08-05"


class TestKnowledgeGraph:

    def test_agent_derived_relations_are_not_corroboration(self):
        t = datetime(2026, 8, 1, tzinfo=UTC)

        def row(other: str, count: int, categories: str):
            return (
                other, "faces_probe_by", "->", count, 0.9, categories, t, t)

        text = format_kg_relations(
            "MSFT", [row("European Commission", 6, "risk_analysis")], "MSFT")
        assert "confirmed" not in text
        assert (
            "re-extracted 6x from the same agent role's reports, not "
            "corroboration") in text
        assert "[agent-derived, not independently sourced]" in text
        text = format_kg_relations(
            "MSFT", [row("OpenAI", 3, "analyst_report, risk_analysis")],
            "MSFT")
        assert "stated by 2 different agent roles, 3 extractions" in text

    def test_extraction_reads_kind_and_as_of(self):
        triples = _parse_response(
            json.dumps([{
                "source": "XOM",
                "source_type": "company",
                "relation": "faces_legal_matter",
                "target": "Louisiana coastal suit",
                "target_type": "event",
                "confidence": 0.9,
                "kind": "event",
                "as_of": "2026-06-30"
            }, {
                "source": "XOM",
                "source_type": "company",
                "relation": "competes_with",
                "target": "CVX",
                "target_type": "company",
                "kind": "Stable",
                "as_of": None
            }, {
                "source": "XOM",
                "source_type": "company",
                "relation": "x",
                "target": "Y",
                "target_type": "company",
                "kind": "rumor",
                "as_of": "soon"
            }]))
        assert [(t.kind, t.as_of)
                for t in triples] == [("event", date(2026, 6, 30)),
                                      ("stable", None), (None, None)]

    @pytest.mark.asyncio
    async def test_old_events_are_left_out(self, db_session):
        analysis = await AnalysisRepository(db_session).create(
            user_id=None, portfolio_id=None, ticker="KGEXP")
        report = AgentReport(
            id=uuid4(), analysis_id=analysis.id, agent_role=AgentRole.RISK,
            ticker="KGEXP", category=DocumentCategory.RISK_ANALYSIS,
            content="c")
        db_session.add(report)
        source = KgEntity(
            name="KGEXP", normalized_name="KGEXP", entity_type="company")
        db_session.add(source)
        await db_session.flush()

        async def relation(target_name, *, kind, as_of, created_at):
            target = KgEntity(
                name=target_name, normalized_name=target_name.upper(),
                entity_type="event")
            db_session.add(target)
            await db_session.flush()
            db_session.add(
                KgRelation(
                    source_entity_id=source.id, target_entity_id=target.id,
                    relation_type="faces_legal_matter",
                    source_report_id=report.id, confidence=0.9, kind=kind,
                    as_of=as_of, created_at=created_at))
            await db_session.flush()

        early = datetime(2026, 6, 1, tzinfo=UTC)
        late = datetime(2026, 9, 1, tzinfo=UTC)
        await relation(
            "Old suit", kind="event", as_of=date(2026, 6, 1), created_at=late)
        await relation(
            "Old dated by row", kind="event", as_of=None, created_at=early)
        await relation(
            "New suit", kind="event", as_of=date(2026, 8, 15), created_at=late)
        await relation(
            "Supplier", kind="stable", as_of=date(2025, 1, 1),
            created_at=early)
        await relation("Legacy row", kind=None, as_of=None, created_at=early)
        repo = KnowledgeGraphRepository(db_session)

        def names(found: list[Any]) -> set[str]:
            return {row[0] for row in found}

        everything = names(await repo.find_relations_for_entity_name("KGEXP"))
        assert everything == {
            "Old suit", "Old dated by row", "New suit", "Supplier",
            "Legacy row"
        }
        current = names(
            await repo.find_relations_for_entity_name(
                "KGEXP", events_expire_before=date(2026, 7, 29)))
        assert current == {"New suit", "Supplier", "Legacy row"}


class TestFilingRecords:

    def test_amount_without_a_scale_word(self):
        cases = {
            "bare-amount": ("$280", True),
            "bare-amount-with-comma": ("$1,423", True),
            "million": ("$280 million", False),
            "spaced-million": ("$ 68 million", False),
            "billion": ("$1.00 billion", False),
            "per-share": ("$0.67 per share", False),
            "dash-range": ("$450-$900 million", False),
            "to-range": ("$2 to $3 billion", False),
            "percent": ("12%", False),
            "suffix-range": ("$450M-$900M", False)
        }
        for case_id, (text, flagged) in cases.items():
            assert frec.amount_without_unit(text) is flagged, case_id

    def test_records_prompt_wording(self):
        prompt = frec.RECORDS_ONLY_PROMPT
        assert 'never a bare "$1,423"' in prompt
        assert "is discarded" in prompt and "scale word" in prompt
        assert "an amount recorded in the period's income statement" in (
            prompt)
        assert "Never a balance as of a date" in prompt
        assert '"exposure_text"' in prompt
        for name in ('"line"', '"next_event"', '"next_event_date"'):
            assert name not in prompt
        assert (
            "never a recurring charge the company records every period"
        ) in prompt
        assert "amortization of acquired intangibles" in prompt
        assert "the period that the quote itself states for this amount" in (
            prompt)
        assert "one-off" not in prompt.replace('"one_off_items"', "")

    def test_exposure_is_amount_checked_and_next_event_is_ignored(
            self, capsys):
        kept = frec.validate_records({
            "legal_matters": [
                _legal(
                    exposure_text="$14 billion",
                    next_event="Tax Court decision expected.",
                    next_event_date="2027", status_date="2026-09-01"),
                _legal(name="Wrong exposure", exposure_text="$15 billion"),
                _legal(name="Bare exposure", exposure_text="$14"),
                _legal(name="Bad date", next_event_date="soon")
            ]
        }, _RAW_FILING, form="10-Q", filing_date="2026-07-28",
                                     report_date="2026-06-30")
        assert kept is not None
        names = [r["name"] for r in kept["legal_matters"]]
        assert names == ["IRS tax litigation", "Bad date"]
        first, bad = kept["legal_matters"]
        assert first["status_date"] is None
        assert "next_event_date" not in first
        assert "next_event" not in bad
        out = capsys.readouterr().out
        for reason in ("amount_not_in_filing", "amount_without_unit",
                       "future_date_cleared"):
            assert reason in out
        assert "bad_next_event_date_cleared" not in out

    def test_exposure_is_rendered_and_stored_next_events_are_not(self):
        row = _Row(
            "QCOM_10-Q_2026-07-29.txt", "10-Q", date(2026, 7, 29),
            date(2026, 6, 28), {
                "legal_matters": [{
                    "name": "QUALCOMM v. Arm",
                    "status": "pending",
                    "status_date": None,
                    "detail": None,
                    "exposure_text": "$ 1.2 billion",
                    "next_event": "Trial scheduled to begin.",
                    "next_event_date": "2026-10-05",
                    "quote": ""
                }],
                "transactions": [{
                    "name": "Alphawave",
                    "type": "acquisition",
                    "status": "pending",
                    "date": None,
                    "detail": None,
                    "next_event": "Expected closing",
                    "next_event_date": None,
                    "quote": ""
                }]
            })
        lines = frec.render_filing_records([row]).split("\n")
        assert lines[3] == (
            "  F1.L1 legal matter: QUALCOMM v. Arm -- status: pending. "
            "Amount stated (claimed, sought, assessed or awarded): "
            "$1.2 billion.")
        assert lines[2] == (
            "  F1.T1 transaction: Alphawave (acquisition) -- status: "
            "pending.")

    def test_record_title(self):
        assert frec.record_title({
            "category": "transactions",
            "name": " A  deal "
        }) == ("transaction: A deal")
        assert (
            frec.record_title({
                "category": "one_off_items",
                "description": "Impairment"
            }) == "unusual item: Impairment")
        assert frec.record_title({"category": "guidance", "metric": ""}) == ""
        assert frec.record_title({}) == ""

    def test_year_to_date_line_for_a_second_quarter(self):
        q = sec_edgar._quarter_results_from_facts(_KO_FACTS, "KO")
        assert q is not None and q.year_to_date is not None
        payload = sec_edgar.quarter_results_payload(q)
        ytd = payload["year_to_date"]
        assert (ytd["period_start"], ytd["period_end"],
                ytd["months"]) == ("2026-01-01", "2026-07-03", 6)
        assert ytd["prior_period_end"] == "2025-06-27"
        assert ytd["revenue_yoy_pct"] == 9.7
        assert payload["text"].split("\n")[-1] == (
            "Year to date (6 months 2026-01-01 to 2026-07-03, compared with "
            "2024-12-28 to 2025-06-27): revenue $25.90B vs $23.60B (+10%); "
            "operating income $9.06B vs $7.60B (+19%); net income: not "
            "reported by the filer")

    def test_no_year_to_date_for_a_first_quarter_10k_or_unconfirmed_start(
            self):
        first = _facts(
            Revenues=[
                _fact(
                    "2025-01-01", "2025-12-31", 47e9, form="10-K",
                    filed="2026-02-20"),
                _fact("2025-01-01", "2025-03-28", 11e9),
                _fact("2026-01-01", "2026-04-03", 12e9)
            ])
        q = sec_edgar._quarter_results_from_facts(first, "KO")
        assert q is not None and q.year_to_date is None
        assert (
            "Year to date" not in sec_edgar.quarter_results_payload(q)["text"])
        no_annual = _facts(
            Revenues=[
                f for f in _KO_FACTS["facts"]["us-gaap"]["Revenues"]["units"]
                ["USD"] if f["form"] == "10-Q"
            ])
        q = sec_edgar._quarter_results_from_facts(no_annual, "KO")
        assert q is not None and q.year_to_date is None
        annual = _facts(
            NetIncomeLoss=[
                _fact("2024-07-01", "2025-03-31", 74e9, filed="2025-04-30"),
                _fact(
                    "2024-07-01", "2025-06-30", 101e9, form="10-K",
                    filed="2025-07-30"),
                _fact("2025-07-01", "2026-03-31", 90e9, filed="2026-04-29"),
                _fact(
                    "2025-07-01", "2026-06-30", 125e9, form="10-K",
                    filed="2026-07-29")
            ])
        q = sec_edgar._quarter_results_from_facts(annual, "MSFT")
        assert q is not None and q.form == "10-K" and q.year_to_date is None


class TestPositionSizing:

    def test_judge_line_names_the_position_limit(self):
        assert position_limit_line(_sizing_state()) == ""
        below = position_limit_line(_sizing_state(position_weight_pct=48.8))
        assert "48.8% of net value" in below and "limit: 50%" in below
        assert "do not recommend BUY" not in below
        above = position_limit_line(_sizing_state(position_weight_pct=55.0))
        assert "do not recommend BUY" in above
        assert "SELL remain allowed" in above

    @pytest.mark.asyncio
    async def test_weight_lookup(self, msft_amd_sizing_env):
        weight = await current_position_weight_pct("p", "MSFT")
        assert weight == pytest.approx(10000 / 20699 * 100)
        assert await current_position_weight_pct("p", "NVDA") == 0.0

    @pytest.mark.asyncio
    async def test_position_limit_rejects_or_skips_the_tiers(
            self, msft_amd_sizing_env, monkeypatch):
        router = _Router([_tiers([(512.0, 2.5), (498.0, 3.5)])])
        result, note = await generate_position_sizing(
            ticker="MSFT", action=RecommendationAction.BUY, confidence=0.78,
            reasoning="r", portfolio_id="p", llm_router=router)
        assert result is None
        assert "capped by the 50% single-position limit" in note

        async def _holdings(portfolio_id):
            return [{
                "ticker": "MSFT",
                "quantity": 21.0
            }, {
                "ticker": "AMD",
                "quantity": 5.0
            }]

        monkeypatch.setattr(
            portfolio_tool_module, "get_portfolio_holdings", _holdings)
        router = _Router([_tiers([(500.0, 0.1)])])
        result, note = await generate_position_sizing(
            ticker="MSFT", action=RecommendationAction.BUY, confidence=0.78,
            reasoning="r", portfolio_id="p", llm_router=router)
        assert result is None
        assert "single-position limit leaves room for" in note
        assert router.calls == []

    @pytest.mark.asyncio
    async def test_threshold_is_inclusive_and_sell_is_not_affected(
            self, monkeypatch):
        cases = {
            "buy-at-the-threshold": (RecommendationAction.BUY, 0.50),
            "low-confidence-sell": (RecommendationAction.SELL, 0.30)
        }
        monkeypatch.setattr(
            sizing_module, "get_market_data_provider", _NoMarket)
        for case_id, (action, confidence) in cases.items():
            with pytest.raises(AssertionError) as exc_info:
                await generate_position_sizing(
                    ticker="UNH", action=action, confidence=confidence,
                    reasoning="r", portfolio_id=str(uuid4()))
            assert "no lookup expected" in str(exc_info.value), case_id

    @pytest.mark.asyncio
    async def test_low_confidence_buy_keeps_the_open_tiers(
            self, workflow_env, rows, monkeypatch):
        from database import SessionLocal
        from sqlalchemy import select
        from domains.analysis.model import Recommendation

        wf, events = workflow_env
        pf = uuid4()
        _, old_rec, _ = await rows.recommendation(
            ticker="UNH", portfolio_id=pf, action="buy", confidence=0.65,
            tiers=[(_BUY, "350.0", "3", _P)])

        final = {
            "ticker": "UNH",
            "action": "buy",
            "confidence": 0.3,
            "reasoning": "r",
            "confidence_adjustment_note": None,
            "debate_converged": False
        }
        monkeypatch.setattr(
            wf, "build_analysis_graph", lambda: _StubGraph({
                "agent_outputs": {},
                "debate": DebateState(),
                "reflection_history": [],
                "final_recommendation": final
            }))
        analysis_id = await rows.analysis(
            ticker="UNH", portfolio_id=pf, status="pending")
        await wf.run_analysis_workflow(
            str(analysis_id), analysis_type=AnalysisType.FULL)

        assert await rows.tier_statuses(old_rec) == [_P]
        async with SessionLocal() as s:
            new = (
                await s.execute(
                    select(Recommendation).where(
                        Recommendation.analysis_id == analysis_id)
                )).scalar_one()
        assert "below the minimum of 0.50 for BUY tiers" in (
            new.tier_generation_note)
        assert (
            "Kept 1 earlier unfilled BUY tier(s) for UNH (not superseded): "
            "this BUY produced no tiers because its confidence is below the "
            "minimum for tiers.") in new.tier_generation_note
        assert "tier.cancelled" not in [n for n, _ in events]

    def test_tier_sizing_note_for_buy_and_sell(self):
        note = tier_sizing_note(
            action=RecommendationAction.BUY, ticker="DIS", tiers=[
                TierSuggestion(
                    target_price=Decimal("100"), quantity=Decimal("5"))
            ], position=_PositionContext(
                held_quantity=Decimal(0), position_value=Decimal(0),
                total_net_value=Decimal("21219.10"),
                current_weight_pct=Decimal(0), unpriced=[]),
            budget=Decimal("7698.95"))
        assert "Size cap" not in note and "totalling 500.00" in note
        assert "smallest of the remaining BUY cash" not in (
            sizing_module._SYSTEM_PROMPT)
        note = tier_sizing_note(
            action=RecommendationAction.SELL, ticker="MSFT", tiers=[
                TierSuggestion(
                    target_price=Decimal("500"), quantity=Decimal("5"))
            ], position=_position("20000", held="20", value="10000"),
            budget=Decimal(0))
        assert "1 SELL tier(s) totalling 2500.00 = 12.5%" in note
        assert note.endswith("They sell 5 of the 20 shares held (25%).")


class TestServiceScripts:

    def test_script_starts_in_the_flattened_container_layout(
            self, tmp_path: Path) -> None:
        for name in ("probe_model_latency.py", "measure_rag_recall.py",
                     "verify_portfolio_backtest.py",
                     "generate_rag_golden_set.py"):
            flat = tmp_path / name / "app" / "scripts"
            flat.mkdir(parents=True)
            shutil.copy(_SCRIPTS / name, flat / name)
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(sys, "argv", [name, "--help"])
                mp.setattr(sys, "path", list(sys.path))
                with pytest.raises(SystemExit) as exit_info:
                    runpy.run_path(str(flat / name), run_name="__main__")
            assert exit_info.value.code == 0, name

    def test_script_walks_up_at_most_to_the_service_dir(self) -> None:
        names = sorted(
            p.name for p in _SCRIPTS.glob("*.py") if p.name != "__init__.py")
        for name in names:
            code = "\n".join(
                line.split("#", 1)[0]
                for line in (_SCRIPTS / name).read_text().splitlines())
            unguarded = [
                line for line in code.splitlines()
                if re.search(r"parents\[(?:[2-9]|\d\d+)\]", line)
                and "if len(" not in line
            ]
            assert not unguarded, name

    @pytest.mark.asyncio
    async def test_latency_probe_records_time_or_cap_with_judge_prompt(
            self) -> None:
        probe = importlib.import_module("scripts.probe_model_latency")
        ok = await probe._one(_FakeClient(0.01), "gemini-3.8-flash", None, 1.0)
        assert ok["status"] == "ok" and ok["output"] == 42
        assert ok["seconds"] < 1.0
        slow = await probe._one(
            _FakeClient(1.0), "gemini-3.8-flash", None, 0.05)
        assert slow["status"] == "no answer within 0s"
        content = probe._user_content()
        assert len(content) > 7000 and "Bull argument:" in content

    @pytest.mark.asyncio
    async def test_file_uploaded_by_another_owner_is_skipped(self, tmp_path):
        for dry_run in (True, False):
            await self._skips_another_owners_file(tmp_path, dry_run)

    async def _skips_another_owners_file(self, tmp_path, dry_run):
        from scripts.upload_seed_documents import (collect_seed_files,
                                                   run_as_system)
        from shared.http_client import build_http_client

        seed = _seed_tree(tmp_path / f"dry_run_{dry_run}")
        files, _ = collect_seed_files({"earnings_call"}, seed)
        seen: list[httpx.Request] = []
        client = build_http_client(
            transport=httpx.MockTransport(
                _core({"META": ["META_earnings_call_q2_2026.txt"]}, seen)))
        out = io.StringIO()
        with redirect_stdout(out):
            counts = await run_as_system(
                files, dry_run=dry_run, client=client,
                core_url="http://core:8000")
        await client.aclose()
        text = out.getvalue()
        assert (
            "SKIP META_earnings_call_q2_2026.txt (already uploaded by "
            "another owner)" in text), dry_run
        assert counts["skipped"] == 1, dry_run
        posts = [r for r in seen if r.method == "POST"]
        if dry_run:
            assert posts == [] and counts["planned"] == 1
            assert "DRY-RUN AMD_earnings_call_q2_2026.txt" in text
        else:
            assert len(posts) == 1
            assert b"AMD_earnings_call_q2_2026" in posts[0].content
            assert counts["uploaded"] == 1
