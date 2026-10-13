import math
from typing import Any, ClassVar
from ai.providers.base import MarketDataProvider, ProviderError
from ai.providers.registry import get_market_data_provider
from ai.tools.base import Tool, clamp_int
from ai.tools.indicators import annualized_volatility, max_drawdown
from ai.tools.market_data_cache import (get_bars_cache_first,
                                        missing_session_note)

_DEFAULT_LOOKBACK_DAYS = 90
_MAX_LOOKBACK_DAYS = 730


class GetVolatilityAndDrawdownTool(Tool):
    name = "get_volatility_and_drawdown"
    description = (
        "Compute annualized volatility and max drawdown for a ticker over "
        "a given lookback, from the FULL daily price series (not just the "
        "last 20 bars get_recent_price_history renders) -- use this instead "
        'of trying to compute these by eye. action_input: {"ticker": "AAPL"} '
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
            ticker, await self._volatility(ticker, lookback_days))

    async def _volatility(self, ticker: str, lookback_days: int) -> str:
        lookback_days = clamp_int(
            lookback_days, default=_DEFAULT_LOOKBACK_DAYS, lo=1,
            hi=_MAX_LOOKBACK_DAYS)
        try:
            bars = await get_bars_cache_first(
                ticker, interval="1d", lookback_days=lookback_days,
                provider=self.provider)
        except ProviderError as exc:
            return (
                f"Volatility/drawdown computation failed for {ticker!r}: {exc}"
            )

        if len(bars) < 3:
            return (
                f"Not enough price history for {ticker!r} to compute "
                f"volatility/drawdown reliably ({len(bars)} bars, need at "
                "least 3 -- try a longer lookback_days).")

        closes = [float(b.close) for b in bars]
        dropped = sum(1 for c in closes if not math.isfinite(c))
        closes = [c for c in closes if math.isfinite(c)]

        if len(closes) < 3:
            return (
                f"Not enough valid price history for {ticker!r} to compute "
                f"volatility/drawdown reliably ({len(closes)} usable bars "
                "after filtering non-finite prices, need at least 3 -- try "
                "a longer lookback_days).")

        vol = annualized_volatility(closes)
        dd = max_drawdown(closes)

        lines = [
            f"Volatility/drawdown for {ticker!r} over {len(bars)} daily bars "
            f"({bars[0].timestamp.date()} to {bars[-1].timestamp.date()}):"
        ]
        if dropped:
            lines.append(
                f"(Note: {dropped} of {len(bars)} real bars had a "
                f"non-finite close price -- likely a real gap/bad tick in "
                f"the underlying real market-data provider -- and were "
                f"excluded from this real computation.)")
        if vol is not None:
            lines.append(f"Annualized volatility: {vol:.1%}")
        if dd is not None:
            lines.append(f"Max drawdown: {dd:.1%}")
        note = missing_session_note(bars)
        if note:
            lines.append(note)
        return "\n".join(lines)
