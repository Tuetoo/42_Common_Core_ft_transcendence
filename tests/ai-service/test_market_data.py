import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4
import ai.tools.market_data_cache as cache_module
import httpx
import pandas as pd
import pytest
from ai.agents.portfolio import _SYSTEM_PROMPT as PORTFOLIO_PROMPT
from ai.agents.portfolio import _prefetch_scenarios
from ai.ingestion import sec_edgar_filings
from ai.providers import fred, newsapi, sec_edgar, tavily, yahoo_finance
from ai.providers.base import (InstrumentMetadata, MarketDataProvider,
                               PriceBar, PriceQuote, ProviderError,
                               ProviderRequestError, ProviderUnavailableError)
from ai.providers.fred import FredProvider
from ai.providers.newsapi import NewsApiProvider
from ai.providers.sec_edgar import SecEdgarProvider, _latest_two_annual_values
from ai.providers.tavily import TavilyProvider
from ai.providers.yahoo_finance import YahooFinanceProvider
from ai.tools.portfolio_tool import RecomputeScenarioTool
from helpers import _FlakyMarket
from structlog.testing import capture_logs
from domains.analysis.repository import AnalysisRepository
from domains.knowledge.model import AgentReport, KgRelation
from domains.knowledge.repository import KnowledgeGraphRepository
from domains.market.service import MarketService
from shared.enums import AgentRole, AssetType, DocumentCategory
from shared.exceptions import NotFoundError

_SEC_USER_AGENT = "Test test@example.com"
_NOW = datetime(2026, 9, 29, 22, 0, tzinfo=UTC)
_WED_OPEN = datetime(2026, 9, 30, 13, 33, tzinfo=UTC)


async def _run_steps(*steps) -> None:
    for step in steps:
        with pytest.MonkeyPatch.context() as mp:
            await step(mp)


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _annual_fact(
        *, fy: int, filed: str, start: str, end: str, val: int) -> dict:
    return {
        "form": "10-K",
        "fp": "FY",
        "fy": fy,
        "filed": filed,
        "start": start,
        "end": end,
        "val": val
    }


def _fy2024_fact(val: int) -> dict:
    return _annual_fact(
        fy=2024, filed="2024-11-01", start="2023-10-01", end="2024-09-28",
        val=val)


async def _aapl_only_ticker_map():
    return {"AAPL": 320193}


def _sec_edgar_backend(monkeypatch, handler=None) -> None:
    monkeypatch.setattr(
        sec_edgar, "_load_ticker_to_cik", _aapl_only_ticker_map)
    monkeypatch.setattr(
        sec_edgar.settings, "SEC_EDGAR_USER_AGENT", _SEC_USER_AGENT)
    if handler is not None:
        monkeypatch.setattr(
            sec_edgar, "get_http_client", lambda: _mock_client(handler))


def _one_history_row() -> pd.DataFrame:
    return pd.DataFrame({
        "Open": [1.0],
        "High": [1.0],
        "Low": [1.0],
        "Close": [1.0],
        "Volume": [100]
    })


class TestYahooFinanceProvider:

    async def _get_current_price_maps_info_dict(self, monkeypatch):
        monkeypatch.setattr(
            yahoo_finance, "_sync_get_info", lambda ticker: {
                "currentPrice": 187.5,
                "currency": "USD"
            })
        quote = await YahooFinanceProvider().get_current_price("AAPL")
        assert quote.price == 187.5
        assert quote.ticker == "AAPL"
        assert quote.source == "yahoo_finance"

    async def _get_current_price_without_a_price_field_raises(
            self, monkeypatch):
        cases = {
            "no-history-means-invalid-ticker": (
                pd.DataFrame, "NOTATICKER", "invalid/delisted"),
            "real-history-means-transient-gap": (
                _one_history_row, "GAPPY", "transient data gap")
        }
        monkeypatch.setattr(yahoo_finance, "_sync_get_info", lambda ticker: {})
        monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
        for case_id, (make_history, symbol, match) in cases.items():
            history = make_history()
            monkeypatch.setattr(
                yahoo_finance, "_sync_get_history",
                lambda ticker, *, interval, lookback_days: history)
            with pytest.raises(ProviderRequestError) as exc_info:
                await YahooFinanceProvider().get_current_price(symbol)
            assert match in str(exc_info.value), case_id

    async def _get_current_price_exception_does_not_double_retry(
            self, monkeypatch):
        calls = {"n": 0}

        def _always_raises(ticker):
            calls["n"] += 1
            raise RuntimeError("network exploded")

        monkeypatch.setattr(yahoo_finance, "_sync_get_info", _always_raises)
        monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
        with pytest.raises(ProviderRequestError, match="info lookup failed"):
            await YahooFinanceProvider().get_current_price("BROKEN")
        assert calls["n"] == 3

    async def _get_recent_price_history_maps_dataframe_rows(self, monkeypatch):
        idx = pd.to_datetime(["2026-01-02", "2026-01-03"], utc=True)
        df = pd.DataFrame({
            "Open": [100.0, 101.0],
            "High": [102.0, 103.0],
            "Low": [99.0, 100.0],
            "Close": [101.5, 102.5],
            "Volume": [1_000_000, 1_200_000]
        }, index=idx)
        monkeypatch.setattr(
            yahoo_finance, "_sync_get_history",
            lambda ticker, *, interval, lookback_days: df)
        bars = await YahooFinanceProvider().get_recent_price_history(
            "AAPL", lookback_days=5)
        assert len(bars) == 2
        assert bars[0].close == 101.5
        assert bars[1].volume == 1_200_000

    async def _get_price_history_range_passes_inclusive_end(self, monkeypatch):
        captured: dict[str, object] = {}

        class _FakeTicker:

            def __init__(self, ticker):
                captured["ticker"] = ticker

            def history(self, *, start, end, interval):
                captured["start"] = start
                captured["end"] = end
                captured["interval"] = interval
                return pd.DataFrame()

        monkeypatch.setattr(
            yahoo_finance, "yf",
            type("_FakeYFModule", (), {"Ticker": _FakeTicker}))
        await YahooFinanceProvider().get_price_history_range(
            "AAPL", start=date(2026, 8, 11), end=date(2026, 8, 26))
        assert captured["start"] == date(2026, 8, 11)
        assert captured["end"] == date(2026, 8, 27)

    async def _get_fundamentals_maps_ratios(self, monkeypatch):
        monkeypatch.setattr(
            yahoo_finance, "_sync_get_info", lambda ticker: {
                "trailingPE": 28.4,
                "forwardPE": 25.1,
                "priceToBook": 45.2,
                "revenueGrowth": 0.06,
                "grossMargins": 0.46,
                "operatingMargins": 0.31,
                "profitMargins": 0.25
            })
        snap = await YahooFinanceProvider().get_fundamentals("AAPL")
        assert snap.pe_ratio == 28.4
        assert snap.net_margin == 0.25
        assert snap.source == "yahoo_finance"

    async def test_current_price(self):
        await _run_steps(
            self._get_current_price_maps_info_dict,
            self._get_current_price_without_a_price_field_raises,
            self._get_current_price_exception_does_not_double_retry)

    async def test_history_and_fundamentals(self):
        await _run_steps(
            self._get_recent_price_history_maps_dataframe_rows,
            self._get_price_history_range_passes_inclusive_end,
            self._get_fundamentals_maps_ratios)


class TestSecEdgarProvider:

    def _picks_latest_two_fiscal_years_and_dedupes_amendments(self):
        facts = [
            _annual_fact(
                fy=2023, filed="2023-11-01", start="2022-10-01",
                end="2023-09-30", val=383_000),
            _fy2024_fact(391_000),
            _annual_fact(
                fy=2024, filed="2024-12-15", start="2023-10-01",
                end="2024-09-28", val=392_000), {
                    "form": "10-Q",
                    "fp": "Q1",
                    "fy": 2025,
                    "filed": "2025-02-01",
                    "val": 100_000
                }
        ]
        latest, prior = _latest_two_annual_values(facts)
        assert latest == 392_000
        assert prior == 383_000

    async def _get_fundamentals_computes_growth_and_margins(self, monkeypatch):
        snap = await self._fundamentals_with_net_income(monkeypatch, 96_000)
        assert snap.revenue_growth_yoy == pytest.approx(
            (400_000 - 380_000) / 380_000)
        assert snap.gross_margin == pytest.approx(180_000 / 400_000)
        assert snap.net_margin == pytest.approx(96_000 / 400_000)
        assert snap.source == "sec_edgar"
        assert not any("is below" in n for n in snap.unavailable_notes)

    async def _margin_note_names_the_fiscal_year(self, monkeypatch):
        snap = await self._fundamentals_with_net_income(monkeypatch, 140_000)
        assert snap.unavailable_notes == [
            "note: operating margin (30.0%) is below net margin (35.0%) for "
            "the fiscal year ended 2024-09-28 -- can be legitimate (e.g. "
            "one-time gains, tax adjustments) but worth double-checking "
            "against the filing directly"
        ]

    async def _fundamentals_with_net_income(self, monkeypatch, net: int):

        def _handler(request: httpx.Request) -> httpx.Response:
            assert "CIK0000320193" in str(request.url)
            return httpx.Response(
                200, json={
                    "facts": {
                        "us-gaap": {
                            "Revenues": {
                                "units": {
                                    "USD": [
                                        _annual_fact(
                                            fy=2023, filed="2023-11-01",
                                            start="2022-10-01",
                                            end="2023-09-30", val=380_000),
                                        _fy2024_fact(400_000)
                                    ]
                                }
                            },
                            "GrossProfit": {
                                "units": {
                                    "USD": [_fy2024_fact(180_000)]
                                }
                            },
                            "OperatingIncomeLoss": {
                                "units": {
                                    "USD": [_fy2024_fact(120_000)]
                                }
                            },
                            "NetIncomeLoss": {
                                "units": {
                                    "USD": [_fy2024_fact(net)]
                                }
                            }
                        }
                    }
                })

        _sec_edgar_backend(monkeypatch, _handler)
        return await SecEdgarProvider().get_fundamentals("AAPL")

    async def _unknown_ticker_raises_request_error(self, monkeypatch):
        _sec_edgar_backend(monkeypatch)
        with pytest.raises(ProviderRequestError):
            await SecEdgarProvider().get_fundamentals("NOTAREALTICKER")

    async def _company_facts_5xx_retries_then_succeeds(self, monkeypatch):
        calls = {"n": 0}

        def _handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 2:
                return httpx.Response(500)
            return httpx.Response(200, json={"facts": {"us-gaap": {}}})

        _sec_edgar_backend(monkeypatch, _handler)
        monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
        snap = await SecEdgarProvider().get_fundamentals("AAPL")
        assert calls["n"] == 2
        assert snap.source == "sec_edgar"

    async def _company_facts_404_fails_fast_without_retry(self, monkeypatch):
        calls = {"n": 0}

        def _handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(404)

        _sec_edgar_backend(monkeypatch, _handler)
        monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
        with pytest.raises(ProviderRequestError):
            await SecEdgarProvider().get_fundamentals("AAPL")
        assert calls["n"] == 1

    async def test_annual_values_growth_and_margins(self):
        self._picks_latest_two_fiscal_years_and_dedupes_amendments()
        await _run_steps(
            self._get_fundamentals_computes_growth_and_margins,
            self._margin_note_names_the_fiscal_year)

    async def test_lookup_errors_and_retries(self):
        await _run_steps(
            self._unknown_ticker_raises_request_error,
            self._company_facts_5xx_retries_then_succeeds,
            self._company_facts_404_fails_fast_without_retry)


async def test_fred_indicator_resolves_alias_and_skips_missing_values(
        monkeypatch):

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["series_id"] == "DFF"
        return httpx.Response(
            200, json={
                "observations": [{
                    "date": "2026-01-01",
                    "value": "5.33"
                }, {
                    "date": "2026-02-01",
                    "value": "."
                }, {
                    "date": "2026-03-01",
                    "value": "5.30"
                }]
            })

    monkeypatch.setattr(fred.settings, "FRED_API_KEY", "test-key")
    monkeypatch.setattr(
        fred, "get_http_client", lambda: _mock_client(_handler))
    points = await FredProvider().get_indicator("fed_funds_rate")
    assert len(points) == 2
    assert points[0].value == 5.33
    assert points[0].label == "Effective Federal Funds Rate (daily)"
    assert points[0].observed_at == date(2026, 1, 1)


class TestNewsAndWebSearchProviders:

    async def test_results_are_mapped(self, monkeypatch):

        def _handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["x-api-key"] == "test-key"
            return httpx.Response(
                200, json={
                    "status": "ok",
                    "articles": [{
                        "source": {
                            "name": "Reuters"
                        },
                        "title": "Tesla beats estimates",
                        "url": "https://example.com/a",
                        "publishedAt": "2026-07-30T12:00:00Z",
                        "description": (
                            "Tesla reported strong quarterly results.")
                    }]
                })

        monkeypatch.setattr(newsapi.settings, "NEWSAPI_API_KEY", "test-key")
        monkeypatch.setattr(
            newsapi, "get_http_client", lambda: _mock_client(_handler))
        articles = await NewsApiProvider().search_news("TSLA")
        assert len(articles) == 1
        assert articles[0].source_name == "Reuters"
        assert articles[0].published_at == datetime(
            2026, 7, 30, 12, 0, tzinfo=UTC)

        captured_body: dict = {}

        def _tavily(request: httpx.Request) -> httpx.Response:
            assert request.headers["authorization"] == "Bearer test-key"
            captured_body.update(json.loads(request.content))
            return httpx.Response(
                200, json={
                    "results": [{
                        "title": "Fed holds rates steady",
                        "url": "https://example.com/fed",
                        "content": "The Fed kept rates unchanged in July.",
                        "published_date": "2026-07-29"
                    }]
                })

        monkeypatch.setattr(tavily.settings, "TAVILY_API_KEY", "test-key")
        monkeypatch.setattr(
            tavily, "get_http_client", lambda: _mock_client(_tavily))
        results = await TavilyProvider().search("Fed rate decision", limit=3)
        assert len(results) == 1
        assert results[0].title == "Fed holds rates steady"
        assert results[0].snippet == "The Fed kept rates unchanged in July."
        assert results[0].published_at == datetime(2026, 7, 29, 0, 0)
        assert captured_body["include_raw_content"] is False
        assert captured_body["search_depth"] == "basic"

    async def test_missing_key_or_failed_request_raises(self, monkeypatch):
        searches = {
            "newsapi": (
                newsapi, "NEWSAPI_API_KEY",
                lambda: NewsApiProvider().search_news("TSLA"),
                lambda: httpx.Response(
                    200, json={
                        "status": "error",
                        "message": "rate limited"
                    })),
            "tavily": (
                tavily, "TAVILY_API_KEY",
                lambda: TavilyProvider().search("anything"),
                lambda: httpx.Response(500, json={"error": "boom"}))
        }
        for case_id, (module, key_setting, search, reply) in searches.items():
            monkeypatch.setattr(module.settings, key_setting, "")
            with pytest.raises(ProviderUnavailableError):
                await search()

            def _handler(request: httpx.Request, reply=reply):
                return reply()

            monkeypatch.setattr(module.settings, key_setting, "test-key")
            monkeypatch.setattr(
                module, "get_http_client",
                lambda handler=_handler: _mock_client(handler))
            with pytest.raises(ProviderRequestError):
                await search()


class _FakeBar:

    def __init__(self, fetched_at, close, timestamp=None):
        self.fetched_at = fetched_at
        self.close = close
        self.timestamp = timestamp if timestamp is not None else fetched_at


def _et_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 4, 0, tzinfo=UTC)


class _SessionBar:

    def __init__(self, day: date, close: float, fetched_at: datetime) -> None:
        self.timestamp = _et_midnight(day)
        self.close = close
        self.fetched_at = fetched_at


def _weekday_series(last: date, fetched_at: datetime,
                    days: int = 10) -> list[_SessionBar]:
    bars, day = [], last
    while len(bars) < days:
        if day.weekday() < 5:
            bars.append(_SessionBar(day, 100.0 + len(bars), fetched_at))
        day -= timedelta(days=1)
    return bars


class _FakeMarketService:

    def __init__(self, cached_bars, *, refresh_raises=None):
        self._cached_bars = cached_bars
        self._refresh_raises = refresh_raises
        self.get_recent_prices_calls = 0
        self.refresh_calls = 0

    def __call__(self, session):
        return self

    async def get_recent_prices(self, ticker, interval):
        self.get_recent_prices_calls += 1
        if self._cached_bars is None:
            raise NotFoundError("no instrument")
        return self._cached_bars

    async def refresh_from_external_provider(
            self, ticker, *, interval, lookback_days, provider=None):
        self.refresh_calls += 1
        if self._refresh_raises:
            raise self._refresh_raises
        return len(self._cached_bars or [])


class _RefreshingMarketService:

    def __init__(
            self, before: list[_SessionBar], after: list[_SessionBar]) -> None:
        self.before, self.after = before, after
        self.refresh_calls = 0

    def __call__(self, session):
        return self

    async def get_recent_prices(self, ticker, interval):
        return self.after if self.refresh_calls else self.before

    async def refresh_from_external_provider(
            self, ticker, *, interval, lookback_days, provider=None):
        self.refresh_calls += 1
        return len(self.after)


class _FakeSession:

    async def commit(self):
        pass


class _FakeSessionContextManager:

    async def __aenter__(self):
        return _FakeSession()

    async def __aexit__(self, *args):
        return False


@pytest.fixture
def cache_session(monkeypatch):
    monkeypatch.setattr(
        cache_module, "SessionLocal", lambda: _FakeSessionContextManager())


class TestBarsCacheFirst:

    @pytest.fixture(autouse=True)
    def _frozen_cache_clock(self, monkeypatch):
        monkeypatch.setattr(cache_module, "_utcnow", lambda: _NOW)

    async def _missing_cache_triggers_refresh(self, monkeypatch):
        fake_service = _FakeMarketService(None)
        fresh_cached = [_FakeBar(_NOW, 200.0)]
        call_count = {"n": 0}

        async def _get_recent_prices_sequenced(ticker, interval):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise NotFoundError("no instrument")
            return fresh_cached

        fake_service.get_recent_prices = _get_recent_prices_sequenced
        monkeypatch.setattr(cache_module, "MarketService", fake_service)
        bars = await cache_module.get_bars_cache_first(
            "NEWCO", interval="1d", lookback_days=30)
        assert fake_service.refresh_calls == 1
        assert [b.close for b in bars] == [200.0]

    async def _refresh_failure_falls_back_to_stale_cache(self, monkeypatch):
        stale_cached = [_FakeBar(_NOW - timedelta(days=10), 90.0)]
        fake_service = _FakeMarketService(
            stale_cached, refresh_raises=ProviderError("down"))
        monkeypatch.setattr(cache_module, "MarketService", fake_service)
        bars = await cache_module.get_bars_cache_first(
            "AMD", interval="1d", lookback_days=30)
        assert fake_service.refresh_calls == 1
        assert [b.close for b in bars] == [90.0]

    async def _refresh_failure_with_no_cache_at_all_raises(self, monkeypatch):
        fake_service = _FakeMarketService(
            None, refresh_raises=ProviderError("down"))
        monkeypatch.setattr(cache_module, "MarketService", fake_service)
        with pytest.raises(ProviderError):
            await cache_module.get_bars_cache_first(
                "BROKEN", interval="1d", lookback_days=30)

    async def _non_daily_interval_still_uses_plain_elapsed_time(
            self, monkeypatch):
        cached = [
            _FakeBar(
                _NOW - timedelta(days=5), 300.0 - i,
                timestamp=_NOW - timedelta(weeks=i)) for i in range(27)
        ]
        fake_service = _FakeMarketService(cached)
        monkeypatch.setattr(cache_module, "MarketService", fake_service)
        bars = await cache_module.get_bars_cache_first(
            "AMD", interval="1wk", lookback_days=180)
        assert fake_service.refresh_calls == 0
        assert bars[-1].close == 300.0
        assert len(bars) >= 25

    async def test_refresh_and_stale_fallback(self, cache_session):
        await _run_steps(
            self._missing_cache_triggers_refresh,
            self._refresh_failure_falls_back_to_stale_cache,
            self._refresh_failure_with_no_cache_at_all_raises,
            self._non_daily_interval_still_uses_plain_elapsed_time)


class TestMissingMarketSession:

    async def test_no_note_when_current_and_refresh_when_missing(
            self, monkeypatch, cache_session):
        bars = [_SessionBar(date(2026, 9, 29), 1.0, _WED_OPEN)]
        assert cache_module.missing_session_note(bars, _WED_OPEN) is None
        assert cache_module.missing_session_note([], _WED_OPEN) is None

        fetched = datetime(2026, 9, 30, 0, 53, tzinfo=UTC)
        service = _RefreshingMarketService(
            before=_weekday_series(date(2026, 9, 28), fetched),
            after=_weekday_series(date(2026, 9, 29), _WED_OPEN))
        monkeypatch.setattr(cache_module, "MarketService", service)
        monkeypatch.setattr(cache_module, "_utcnow", lambda: _WED_OPEN)
        bars = await cache_module.get_bars_cache_first("AMD", lookback_days=10)
        assert service.refresh_calls == 1
        assert cache_module.bar_session_date(bars[-1]) == date(2026, 9, 29)


class _FixedAssetTypeProvider(MarketDataProvider):

    def __init__(self, *, quote_type: str | None) -> None:
        self._quote_type = quote_type

    async def get_current_price(self, ticker: str) -> PriceQuote:
        raise NotImplementedError

    async def get_recent_price_history(
            self, ticker: str, *, interval: str = "1d",
            lookback_days: int = 30) -> list[PriceBar]:
        return [
            PriceBar(
                ticker=ticker, interval=interval, timestamp=datetime.now(UTC),
                open=100.0, high=101.0, low=99.0, close=100.5, volume=1000,
                source="fake")
        ]

    async def get_price_history_range(
            self, ticker: str, *, start: date, end: date,
            interval: str = "1d") -> list[PriceBar]:
        raise NotImplementedError

    async def get_instrument_metadata(
            self, ticker: str) -> InstrumentMetadata | None:
        return InstrumentMetadata(
            name=f"{ticker} Inc.", asset_type=self._quote_type)


async def test_refresh_sets_real_asset_type_from_provider(db_session):
    service = MarketService(db_session)
    provider = _FixedAssetTypeProvider(quote_type="ETF")
    await service.refresh_from_external_provider("SPY", provider=provider)
    instrument = await service.instruments.get_by_ticker("SPY")
    assert instrument.asset_type == AssetType.ETF.value


async def test_fx_rate_survives_a_redis_error(client, monkeypatch):
    import ai.providers.registry as registry
    import api.internal as internal

    class _Down:

        async def get(self, key):
            raise ConnectionError("redis down")

        async def set(self, *args, **kwargs):
            raise ConnectionError("redis down")

    class _Provider:

        async def get_current_price(self, ticker):
            return SimpleNamespace(price=Decimal("0.79"))

    monkeypatch.setattr(internal, "get_cache_client", lambda _s: _Down())
    monkeypatch.setattr(
        registry, "get_market_data_provider", lambda: _Provider())
    response = await client.get(
        "/internal/fx-rate", params={
            "from_currency": "usd",
            "to_currency": "gbp"
        })
    assert response.status_code == 200, response.text
    assert response.json() == {"rate": 0.79}


def _scenario_tool(
        market: _FlakyMarket | None = None) -> RecomputeScenarioTool:
    provider: Any = market or _FlakyMarket()
    return RecomputeScenarioTool("p1", market_provider=provider)


class TestPortfolioScenarioPrefetch:

    async def test_scenarios_are_prefetched_only_with_a_price(
            self, _portfolio_backend, monkeypatch):
        tool = _scenario_tool(_FlakyMarket(failing={"AMD"}))
        assert await tool.suggested_share_counts("AMD") is None
        assert await _prefetch_scenarios(tool, "AMD") == []

        async def _committed(portfolio_id: str, ticker: str) -> Any:
            return None

        monkeypatch.setattr(
            "ai.tools.portfolio_tool.committed_buy_cash_elsewhere", _committed)
        entries = await _prefetch_scenarios(_scenario_tool(), "AMD")
        assert [e.action_input for e in entries] == [{
            "ticker": "AMD",
            "additional_shares": 1
        }, {
            "ticker": "AMD",
            "additional_shares": 3
        }]
        assert all(
            e.prefetched and e.action == "recompute_scenario" for e in entries)
        assert entries[0].observation.startswith(
            "Scenario: AMD +1 shares at 533.60.")
        assert entries[1].observation.startswith(
            "Scenario: AMD +3 shares at 533.60.")
        assert "computed in code before your" in PORTFOLIO_PROMPT
        assert "never a share count already computed" in PORTFOLIO_PROMPT


class TestKnowledgeGraphVisibility:

    async def _relation(self, db_session, *, user_id, target: str) -> None:
        analysis = await AnalysisRepository(db_session).create(
            user_id=user_id, portfolio_id=None, ticker="KGVIS")
        report = AgentReport(
            id=uuid4(), analysis_id=analysis.id,
            agent_role=AgentRole.FUNDAMENTAL, ticker="KGVIS",
            category=DocumentCategory.ANALYST_REPORT, content="note")
        db_session.add(report)
        await db_session.flush()
        repo = KnowledgeGraphRepository(db_session)
        source = await repo.get_or_create_entity(
            name="KGVIS", entity_type="company")
        other = await repo.get_or_create_entity(
            name=target, entity_type="company")
        db_session.add(
            KgRelation(
                source_entity_id=source.id, target_entity_id=other.id,
                relation_type="competes_with", source_report_id=report.id,
                confidence=0.9))
        await db_session.flush()

    async def test_each_user_sees_system_and_own_relations_only(
            self, db_session):
        alice, bob = uuid4(), uuid4()
        await self._relation(db_session, user_id=None, target="SYSCO")
        await self._relation(db_session, user_id=alice, target="ALICECO")
        await self._relation(db_session, user_id=bob, target="BOBCO")
        repo = KnowledgeGraphRepository(db_session)

        def names(rows):
            return {r[0] for r in rows}

        assert names(
            await repo.find_relations_for_entity_name(
                "KGVIS", viewer_user_id=str(alice))) == {"SYSCO", "ALICECO"}
        assert names(
            await repo.find_relations_for_entity_name(
                "KGVIS", viewer_user_id=bob)) == {"SYSCO", "BOBCO"}
        assert names(
            await repo.find_relations_for_entity_name("KGVIS")) == {"SYSCO"}
        assert names(
            await repo.find_relations_for_entity_name(
                "KGVIS", viewer_user_id="not-a-uuid")) == {"SYSCO"}


class _Filing:

    def __init__(self, form: str, filing_date: str) -> None:
        self.form = form
        self.filing_date = filing_date


class _Company:
    cik = 19617
    name = "TEST CO"

    def __init__(self, recent: list[_Filing]) -> None:
        self.recent = recent
        self.calls: list[tuple[str, bool]] = []

    def get_filings(self, form: str, trigger_full_load: bool = True):
        self.calls.append((form, trigger_full_load))
        if trigger_full_load:
            raise AssertionError("the full history must not be loaded")
        return [f for f in self.recent if f.form.startswith(form)]


class TestSecFormListing:

    def test_form_listing_pages_amendments_and_failures(self, monkeypatch):
        import edgar
        import edgar.httprequests as httprequests

        pages = {
            "CIK0000019617.json": {
                "filings": {
                    "files": [{
                        "name": "p1.json"
                    }, {
                        "name": "p2.json"
                    }, {
                        "name": "p3.json"
                    }]
                }
            },
            "p1.json": {
                "form": ["424B2", "FWP"],
                "filingDate": ["2025-09-30", "2025-09-29"],
                "accessionNumber": ["a1", "a2"]
            },
            "p2.json": {
                "form": ["424B2", "10-Q", "10-Q"],
                "filingDate": ["2025-08-29", "2025-08-05", "2025-05-02"],
                "accessionNumber": ["b1", "b2", "b3"]
            }
        }
        fetched: list[str] = []

        def _download_json(url: str) -> dict[str, Any]:
            name = url.rsplit("/", 1)[-1]
            fetched.append(name)
            return pages[name]

        monkeypatch.setattr(httprequests, "download_json", _download_json)
        monkeypatch.setattr(
            edgar, "Filing", lambda **kw: SimpleNamespace(**kw))
        company = _Company([
            _Filing("10-Q", "2026-08-06"),
            _Filing("10-Q", "2026-05-01"),
            _Filing("10-Q", "2025-11-04")
        ])
        listing = sec_edgar_filings.list_form_filings(company, "10-Q", 4)
        assert [str(f.filing_date) for f in listing.filings
                ] == ["2026-08-06", "2026-05-01", "2025-11-04", "2025-08-05"]
        assert listing.filings[-1].accession_no == "b2"
        assert listing.filings[-1].form == "10-Q"
        assert listing.older_pages_read == 2
        assert fetched == ["CIK0000019617.json", "p1.json", "p2.json"]

        company = _Company([_Filing("10-K/A", "2026-04-30")])
        found = sec_edgar_filings._amendment_listing(
            company, full_history=False)
        assert len(found) == 1
        assert company.calls == [("10-K/A", False)]
        with pytest.raises(AssertionError):
            sec_edgar_filings._amendment_listing(company, full_history=True)

        class _Hangs(_Company):

            def get_filings(self, form: str, trigger_full_load: bool = True):
                raise TimeoutError()

        monkeypatch.setattr(edgar, "set_identity", lambda *_a, **_kw: None)
        monkeypatch.setattr(edgar, "Company", lambda _t: _Hangs([]))
        with capture_logs() as logs:
            out = sec_edgar_filings._fetch_real_filings_sync("JPM", set())
        assert out == []
        failures = [
            e for e in logs
            if e["event"] == "sec_edgar_filings.form_listing_failed"
        ]
        assert [e["form"] for e in failures] == ["10-K", "10-Q"]
        assert failures[0]["error"] == "TimeoutError()"
        assert "elapsed_seconds" in failures[0]
        timeout = sec_edgar_filings._EDGAR_API_TIMEOUT_SECONDS
        assert failures[0]["timeout_seconds"] == timeout
