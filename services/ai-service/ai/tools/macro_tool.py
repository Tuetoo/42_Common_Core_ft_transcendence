from collections.abc import Callable, Sequence
from datetime import date, timedelta
from typing import Any, ClassVar
from ai.providers.base import MacroDataPoint, MacroDataProvider, ProviderError
from ai.providers.registry import get_macro_data_provider
from ai.tools.base import Tool, clamp_int

RECENT_WINDOWS_DAYS: tuple[int, ...] = (30, 90)
POLICY_RATE_SERIES_IDS = frozenset({"DFF"})
STEP_MIN_CHANGE = 0.20
STEP_HOLD_OBSERVATIONS = 5
STEP_HOLD_TOLERANCE = 0.05

Formatter = Callable[[float], str]

YOY_INDEX_SERIES_IDS = frozenset({"CPIAUCSL"})
YOY_INDEX_ALIASES = frozenset({"cpi", "cpiaucsl"})
YOY_MIN_LOOKBACK_DAYS = 430


def _direction(change: float) -> str:
    return "up" if change > 0 else "down" if change < 0 else "flat"


def point_on_or_before(
        points: Sequence[MacroDataPoint],
        cutoff: date) -> MacroDataPoint | None:
    found: MacroDataPoint | None = None
    for point in points:
        if point.observed_at > cutoff:
            break
        found = point
    return found


def recent_changes_text(
        points: Sequence[MacroDataPoint], fmt: Formatter) -> str:
    latest = points[-1]
    parts: list[str] = []
    for days in RECENT_WINDOWS_DAYS:
        ref = point_on_or_before(
            points[:-1], latest.observed_at - timedelta(days=days))
        if ref is None:
            continue
        change = latest.value - ref.value
        parts.append(
            f"{_direction(change)} {abs(change):.2f} over the last {days} "
            f"days "
            f"(from {fmt(ref.value)} on {ref.observed_at})")
    return f" Recent change: {'; '.join(parts)}." if parts else ""


def last_step_move(
    points: Sequence[MacroDataPoint]
) -> tuple[MacroDataPoint, MacroDataPoint] | None:
    for i in range(len(points) - 1, 0, -1):
        before, after = points[i - 1], points[i]
        if abs(after.value - before.value) < STEP_MIN_CHANGE:
            continue
        following = points[i + 1:i + 1 + STEP_HOLD_OBSERVATIONS]
        preceding = points[max(0, i - 1 - STEP_HOLD_OBSERVATIONS):i - 1]
        if all(abs(p.value - after.value) <= STEP_HOLD_TOLERANCE
               for p in following) and all(
                   abs(p.value - before.value) <= STEP_HOLD_TOLERANCE
                   for p in preceding):
            return before, after
    return None


def policy_move_text(points: Sequence[MacroDataPoint], fmt: Formatter) -> str:
    if points[-1].series_id not in POLICY_RATE_SERIES_IDS or len(points) < 2:
        return ""
    move = last_step_move(points)
    if move is None:
        return (
            f" Last policy-rate move: none of {STEP_MIN_CHANGE:.2f} or more "
            f"since {points[0].observed_at}.")
    before, after = move
    change = after.value - before.value
    return (
        f" Last policy-rate move: {_direction(change)} {abs(change):.2f} on "
        f"{after.observed_at} ({fmt(before.value)} -> {fmt(after.value)}).")


def year_over_year_text(points: Sequence[MacroDataPoint]) -> str:
    latest = points[-1]
    if latest.series_id not in YOY_INDEX_SERIES_IDS:
        return ""
    ref = point_on_or_before(
        points[:-1], latest.observed_at - timedelta(days=365))
    if ref is None or ref.value == 0:
        return (
            " Year-over-year change: not available from this range; the "
            "latest= value is an index level, not an inflation rate.")
    change_pct = (latest.value / ref.value - 1) * 100
    return (
        f" Year-over-year change (the inflation rate; latest= above is an "
        f"index level, not a rate): {_direction(change_pct)} "
        f"{abs(change_pct):.2f}% (from {ref.value:.2f} on "
        f"{ref.observed_at}).")


class GetMacroIndicatorsTool(Tool):
    name = "get_macro_indicators"
    description = (
        "Get a macroeconomic indicator's recent observations. action_input: "
        '{"series": "fed_funds_rate"} -- known aliases: fed_funds_rate, cpi, '
        "unemployment_rate, gdp, 10y_treasury_yield, vix. Any other string is "
        'tried as a literal FRED series ID. (optional: {"lookback_days": 365})'
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "series": {
                "type": "string"
            },
            "lookback_days": {
                "type": "integer"
            }
        },
        "required": ["series"],
        "additionalProperties": False
    }

    def __init__(self, provider: MacroDataProvider | None = None) -> None:
        self.provider = provider or get_macro_data_provider()

    async def run(  # type: ignore[override]
            self, series: str, lookback_days: int = 365) -> str:
        lookback_days = clamp_int(lookback_days, default=365, lo=1, hi=7300)
        if series.strip().lower() in YOY_INDEX_ALIASES:
            lookback_days = max(lookback_days, YOY_MIN_LOOKBACK_DAYS)
        try:
            points = await self.provider.get_indicator(
                series, lookback_days=lookback_days)
        except ProviderError as exc:
            return f"Macro indicator lookup failed for {series!r}: {exc}"

        if not points:
            return (
                f"No observations found for {series!r} in the last "
                f"{lookback_days} days.")

        latest = points[-1]

        def _format_value(value: float, unit: str | None) -> str:
            if unit == "%":
                return f"{value:.2f}%"
            return f"{value:.2f}{f' {unit}' if unit else ''}"

        trend = ""
        if len(points) >= 2:
            change = latest.value - points[0].value
            direction = (
                "up" if change > 0 else "down" if change < 0 else "flat")
            trend = (
                f" ({direction} {abs(change):.2f} over the period, "
                f"from {_format_value(points[0].value, latest.unit)})")

        def fmt(value: float) -> str:
            return _format_value(value, latest.unit)

        return (
            f"{latest.label} ({latest.series_id}): "
            f"latest={_format_value(latest.value, latest.unit)} "
            f"as of {latest.observed_at}"
            f"{trend}. {len(points)} observations in range."
            f"{recent_changes_text(points, fmt)}"
            f"{policy_move_text(points, fmt)}"
            f"{year_over_year_text(points)}")
