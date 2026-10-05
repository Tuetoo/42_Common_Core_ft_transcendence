from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
import structlog
from ai.providers.base import MarketDataProvider, PriceBar, ProviderError
from database import SessionLocal
from domains.market.schema import PriceBarRead
from domains.market.service import MarketService
from shared.exceptions import NotFoundError
from shared.market_calendar import (MARKET_TZ, is_trading_day,
                                    previous_trading_day)

logger = structlog.get_logger(__name__)

_FRESHNESS_BY_INTERVAL: dict[str, timedelta] = {
    "1wk": timedelta(days=6),
    "1mo": timedelta(days=27)
}
_DEFAULT_FRESHNESS = timedelta(hours=20)

_MARKET_CLOSE_ET = (16, 0)

_INTRADAY_REUSE_GRACE = timedelta(minutes=15)


def _most_recent_session_close(now_utc: datetime) -> datetime:
    session = last_completed_session(now_utc)
    close = datetime.combine(
        session, time(_MARKET_CLOSE_ET[0], _MARKET_CLOSE_ET[1]),
        tzinfo=MARKET_TZ)
    return close.astimezone(UTC)


def _is_fresh_daily_bar(fetched_at: datetime, now: datetime) -> bool:
    if fetched_at >= _most_recent_session_close(now):
        return True
    return (now - fetched_at) < _INTRADAY_REUSE_GRACE


def _utcnow() -> datetime:
    return datetime.now(UTC)


def last_completed_session(now: datetime) -> date:
    now_et = now.astimezone(MARKET_TZ)
    today = now_et.date()
    if (is_trading_day(today)
            and (now_et.hour, now_et.minute) >= _MARKET_CLOSE_ET):
        return today
    return previous_trading_day(today)


def bar_session_date(bar: PriceBar | PriceBarRead) -> date:
    return _as_utc(bar.timestamp).astimezone(MARKET_TZ).date()


def completed_daily_bars(
        bars: Sequence[PriceBar | PriceBarRead],
        now: datetime) -> list[PriceBar | PriceBarRead]:
    last = last_completed_session(now)
    return [
        b for b in bars
        if bar_session_date(b) <= last and not _fetched_before_its_close(b)
    ]


def _fetched_before_its_close(bar: PriceBar | PriceBarRead) -> bool:
    fetched_at = getattr(bar, "fetched_at", None)
    if fetched_at is None:
        return False
    session_close = datetime.combine(
        bar_session_date(bar), time(_MARKET_CLOSE_ET[0], _MARKET_CLOSE_ET[1]),
        tzinfo=MARKET_TZ)
    return _as_utc(fetched_at) < session_close


def missing_session_note(
        bars: Sequence[PriceBar | PriceBarRead],
        now: datetime | None = None) -> str | None:
    if not bars:
        return None
    expected = last_completed_session(now or _utcnow())
    newest = max(bar_session_date(b) for b in bars)
    if newest >= expected:
        return None
    return (
        f"(Note: the price data ends at the {newest} session; the latest "
        f"completed session, {expected}, is not available from the market "
        "data provider yet. Say that the latest close is from "
        f"{newest}, not {expected}.)")


_MAX_GAP_BY_INTERVAL: dict[str, timedelta] = {
    "1d": timedelta(days=5),
    "1wk": timedelta(days=10),
    "1mo": timedelta(days=35)
}


def _as_utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _window_start(now: datetime, lookback_days: int) -> datetime:
    start = now - timedelta(days=max(lookback_days, 1))
    return start.replace(hour=0, minute=0, second=0, microsecond=0)


def _bars_in_window(bars: list[PriceBarRead],
                    start: datetime) -> list[PriceBarRead]:
    return sorted((b for b in bars if _as_utc(b.timestamp) >= start),
                  key=lambda b: _as_utc(b.timestamp))


def _covers_window(
        window_bars: list[PriceBarRead], start: datetime,
        interval: str) -> bool:
    if not window_bars:
        return False
    max_gap = _MAX_GAP_BY_INTERVAL.get(interval, _MAX_GAP_BY_INTERVAL["1d"])
    if _as_utc(window_bars[0].timestamp) - start > max_gap:
        return False
    return all(
        _as_utc(later.timestamp) - _as_utc(earlier.timestamp) <= max_gap
        for earlier, later in pairwise(window_bars))


def _served(bars: list[PriceBar | PriceBarRead], interval: str,
            now: datetime) -> list[PriceBar | PriceBarRead]:
    if interval != "1d":
        return bars
    return completed_daily_bars(bars, now)


async def get_bars_cache_first(
    ticker: str, *, interval: str = "1d", lookback_days: int = 30,
    provider: MarketDataProvider | None = None
) -> list[PriceBar | PriceBarRead]:
    async with SessionLocal() as session:
        service = MarketService(session)
        try:
            cached = await service.get_recent_prices(ticker, interval)
        except NotFoundError:
            cached = []

        now = _utcnow()
        if interval == "1d":
            is_fresh = bool(cached) and _is_fresh_daily_bar(
                cached[0].fetched_at, now)
            if (is_fresh
                    and (now - cached[0].fetched_at) >= _INTRADAY_REUSE_GRACE
                    and max((bar_session_date(b)
                             for b in completed_daily_bars(cached, now)),
                            default=None) != last_completed_session(now)):
                is_fresh = False
        else:
            freshness = _FRESHNESS_BY_INTERVAL.get(
                interval, _DEFAULT_FRESHNESS)
            is_fresh = (
                bool(cached) and (now - cached[0].fetched_at) < freshness)
        start = _window_start(now, lookback_days)
        window_bars = _bars_in_window(cached, start)
        if is_fresh and _covers_window(window_bars, start, interval):
            logger.debug("market_cache.hit", ticker=ticker, interval=interval)
            return _served(list(window_bars), interval, now)

        logger.debug(
            "market_cache.miss_or_stale", ticker=ticker, interval=interval,
            had_cached_entries=bool(cached), fresh_but_incomplete=is_fresh)
        try:
            written = await service.refresh_from_external_provider(
                ticker, interval=interval, lookback_days=lookback_days,
                provider=provider)
            await session.commit()
        except ProviderError:
            if cached:
                logger.warning(
                    "market_cache.refresh_failed_serving_stale_cache",
                    ticker=ticker, interval=interval)
                return _served(
                    list(window_bars)
                    or list(reversed(cached[:lookback_days])), interval, now)
            raise

        refreshed = await service.get_recent_prices(ticker, interval)
        if isinstance(written, int) and written > 0:
            return _served(list(reversed(refreshed[:written])), interval, now)
        return _served(list(_bars_in_window(refreshed, start)), interval, now)
