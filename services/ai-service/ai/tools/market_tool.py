import math
import re
from collections.abc import Sequence
from typing import Any, ClassVar
from ai.providers.base import MarketDataProvider, PriceBar, ProviderError
from ai.providers.registry import get_market_data_provider
from ai.tools.base import Tool, clamp_int
from ai.tools.market_data_cache import (get_bars_cache_first,
                                        missing_session_note)
from domains.market.schema import PriceBarRead

_TOP_VOLUME_SESSIONS = 3
_FLAT_CHANGE_PCT = 0.05


def _direction(change_pct: float) -> str:
    if change_pct > _FLAT_CHANGE_PCT:
        return "up day"
    if change_pct < -_FLAT_CHANGE_PCT:
        return "down day"
    return "flat day"


def describe_high_volume_sessions(
        bars: Sequence[PriceBar | PriceBarRead], shown: int) -> list[str]:
    start = max(len(bars) - shown, 0)
    rows: list[tuple[float, str]] = []
    for i in range(start, len(bars)):
        volume = float(bars[i].volume or 0)
        base = float(bars[i - 1].close if i > 0 else bars[i].open)
        close = float(bars[i].close)
        if volume <= 0 or base == 0 or not math.isfinite(base):
            continue
        change_pct = (close - base) / base * 100
        basis = (
            "previous session" if i > 0 else
            "no earlier session in the data; from this session's open")
        rows.append((
            volume, (
                f"- {bars[i].timestamp.date()}: volume {volume:,.0f}, "
                f"{_direction(change_pct)} ({change_pct:+.2f}%, {basis} "
                f"{base:g} -> {close:g})")))
    if not rows:
        return []
    top = sorted(rows, key=lambda r: r[0], reverse=True)[:_TOP_VOLUME_SESSIONS]
    return [
        "Highest-volume sessions shown (price direction computed in code; "
        "describe each with this direction):", *(line for _, line in top)
    ]


_DEFAULT_LOOKBACK_DAYS = 30
PRICE_INTERVALS = ("1d", "1wk", "1mo")
_MAX_LOOKBACK_DAYS = 730

CLOSE_RE = re.compile(r"close=(-?\d+(?:\.\d+)?)")


def latest_close_from_price_history(observation: str) -> float | None:
    matches = CLOSE_RE.findall(observation)
    return float(matches[-1]) if matches else None


class GetCurrentPriceTool(Tool):
    name = "get_current_price"
    description = (
        'Get the current price for a ticker. action_input: {"ticker": "AAPL"}')
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string"
            }
        },
        "required": ["ticker"],
        "additionalProperties": False
    }

    def __init__(
            self, provider: MarketDataProvider | None = None, *,
            analysis_ticker: str | None = None) -> None:
        self.provider = provider or get_market_data_provider()
        self.analysis_ticker = analysis_ticker

    async def run(self, ticker: str) -> str:  # type: ignore[override]
        return self.label_result(ticker, await self._quote(ticker))

    async def _quote(self, ticker: str) -> str:
        try:
            quote = await self.provider.get_current_price(ticker)
        except ProviderError as exc:
            return f"Price lookup failed for {ticker!r}: {exc}"
        return (
            f"{quote.ticker} current price: {quote.price} {quote.currency} "
            f"as of {quote.as_of.isoformat()} (source: {quote.source})")


class GetRecentPriceHistoryTool(Tool):
    name = "get_recent_price_history"
    description = (
        "Get recent daily/weekly price history for a ticker. action_input: "
        '{"ticker": "AAPL", "interval": "1d", "lookback_days": 30} '
        '("interval"/"lookback_days" optional)')
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string"
            },
            "interval": {
                "type": "string",
                "enum": list(PRICE_INTERVALS)
            },
            "lookback_days": {
                "type": "integer"
            }
        },
        "required": ["ticker"],
        "additionalProperties": False
    }

    def __init__(
            self, provider: MarketDataProvider | None = None, *,
            analysis_ticker: str | None = None) -> None:
        self.provider = provider or get_market_data_provider()
        self.analysis_ticker = analysis_ticker

    async def run(  # type: ignore[override]
        self,
        ticker: str,
        interval: str = "1d",
        lookback_days: int = _DEFAULT_LOOKBACK_DAYS
    ) -> str:
        return self.label_result(
            ticker, await self._history(ticker, interval, lookback_days))

    async def _history(
            self, ticker: str, interval: str, lookback_days: int) -> str:
        if interval not in PRICE_INTERVALS:
            return (
                f"Price history lookup failed for {ticker!r}: unsupported "
                f"interval {interval!r} (use one of "
                f"{', '.join(PRICE_INTERVALS)}).")
        lookback_days = clamp_int(
            lookback_days, default=_DEFAULT_LOOKBACK_DAYS, lo=1,
            hi=_MAX_LOOKBACK_DAYS)
        try:
            bars = await get_bars_cache_first(
                ticker, interval=interval, lookback_days=lookback_days,
                provider=self.provider)
        except ProviderError as exc:
            return f"Price history lookup failed for {ticker!r}: {exc}"

        if not bars:
            return (
                f"No price history found for {ticker!r} at "
                f"interval={interval!r}.")

        shown = bars[-20:]
        lines = [
            f"{bar.timestamp.date()}: open={bar.open} high={bar.high} "
            f"low={bar.low} close={bar.close} volume={bar.volume}"
            for bar in shown
        ]
        text = (
            f"Recent price history for {ticker!r} ({interval}, showing last "
            f"{len(shown)} of {len(bars)} bars):\n" + "\n".join(lines))
        volume_lines = describe_high_volume_sessions(bars, len(shown))
        if volume_lines:
            text = text + "\n" + "\n".join(volume_lines)
        note = missing_session_note(bars) if interval == "1d" else None
        return f"{text}\n{note}" if note else text
