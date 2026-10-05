import math
from typing import Any, ClassVar
from ai.providers.base import MarketDataProvider, ProviderError
from ai.providers.registry import get_market_data_provider
from ai.tools.base import Tool, clamp_int
from ai.tools.indicators import (atr, bollinger_bands, macd,
                                 price_position_vs_level, rsi, rsi_band_words,
                                 sma)
from ai.tools.market_data_cache import (get_bars_cache_first,
                                        missing_session_note)

_DEFAULT_LOOKBACK_DAYS = 90
_MAX_LOOKBACK_DAYS = 730


class GetTechnicalIndicatorsTool(Tool):
    name = "get_technical_indicators"
    description = (
        "Compute RSI, MACD, SMA(20/50), Bollinger Bands, and ATR for a ticker "
        'from recent daily price history. action_input: {"ticker": "AAPL"} '
        '(optional: {"lookback_days": 90})')
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string"
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
        self, ticker: str, lookback_days: int = _DEFAULT_LOOKBACK_DAYS
    ) -> str:
        return self.label_result(
            ticker, await self._indicators(ticker, lookback_days))

    async def _indicators(self, ticker: str, lookback_days: int) -> str:
        lookback_days = clamp_int(
            lookback_days, default=_DEFAULT_LOOKBACK_DAYS, lo=1,
            hi=_MAX_LOOKBACK_DAYS)
        try:
            bars = await get_bars_cache_first(
                ticker, interval="1d", lookback_days=lookback_days,
                provider=self.provider)
        except ProviderError as exc:
            return (
                f"Technical indicator computation failed for {ticker!r}: {exc}"
            )

        if len(bars) < 20:
            return (
                f"Not enough price history for {ticker!r} to compute "
                f"technical "
                f"indicators reliably ({len(bars)} bars, need at least 20 -- "
                "try a longer lookback_days).")

        good_bars = [
            b for b in bars if math.isfinite(b.close)
            and math.isfinite(b.high) and math.isfinite(b.low)
        ]
        dropped = len(bars) - len(good_bars)
        if len(good_bars) < 20:
            return (
                f"Not enough valid price history for {ticker!r} to compute "
                f"technical indicators reliably ({len(good_bars)} usable "
                f"bars after filtering non-finite prices, need at least 20 "
                "-- try a longer lookback_days).")
        bars = good_bars

        closes = [float(b.close) for b in bars]
        highs = [float(b.high) for b in bars]
        lows = [float(b.low) for b in bars]

        lines = [
            f"Technical indicators for {ticker!r} ({len(bars)} daily bars):"
        ]
        if dropped:
            lines.append(
                f"(Note: {dropped} real bar(s) had a non-finite open/high/"
                f"low/close and were excluded from this real computation.)")

        latest_close = closes[-1]
        lines.append(
            f"Latest close: {latest_close:.2f} "
            f"(bar date {bars[-1].timestamp.date()})")

        sma20 = sma(closes, 20)
        sma50 = sma(closes, 50)
        for period, average in ((20, sma20), (50, sma50)):
            if average is None:
                continue
            lines.append(f"SMA({period}): {average:.2f}")
        for period, average in ((20, sma20), (50, sma50)):
            if average is None or average == 0:
                continue
            position = price_position_vs_level(latest_close, average)
            pct = (latest_close - average) / average * 100
            lines.append(f"Price vs SMA({period}): {position} ({pct:+.1f}%)")

        rsi14 = rsi(closes, period=14)
        if rsi14 is not None:
            lines.append(
                f"RSI(14): {rsi14:.1f} (band: {rsi_band_words(rsi14)}; "
                f"standard "
                "bands: <=30 oversold, >=70 overbought, otherwise neutral)")

        macd_result = macd(closes)
        if macd_result is not None:
            macd_line, signal_line, histogram = macd_result
            lines.append(
                f"MACD: line={macd_line:.3f} signal={signal_line:.3f} "
                f"histogram={histogram:.3f}")
        else:
            lines.append("MACD: not enough history (need 35+ bars)")

        bb = bollinger_bands(closes, period=20)
        if bb is not None:
            upper, middle, lower = bb
            lines.append(
                f"Bollinger Bands(20): upper={upper:.2f} mid={middle:.2f} "
                f"lower={lower:.2f}")

        atr14 = atr(highs, lows, closes, period=14)
        if atr14 is not None:
            lines.append(f"ATR(14): {atr14:.2f}")

        note = missing_session_note(bars)
        if note:
            lines.append(note)
        return "\n".join(lines)
