import re
from typing import Any
import structlog
from ai.agents.finish_checks import (OWN_EVIDENCE_CHECK,
                                     InternalTokenValidator,
                                     own_evidence_figure_check,
                                     reported_number_issue)
from ai.agents.loop import (BaseAgent, ReActAgentLoop, ScratchpadEntry,
                            combine_context, flag_unresolved_issue,
                            is_successful_tool_entry, prefetch_tool_calls)
from ai.graph.state import AgentOutput, AnalysisState
from ai.tools.indicators import (PRICE_VS_LEVEL_BAND_PCT,
                                 price_position_vs_level, rsi_band)
from ai.tools.market_tool import GetRecentPriceHistoryTool
from ai.tools.market_tool import \
    latest_close_from_price_history as _latest_close_from_price_history
from ai.tools.technical_tool import GetTechnicalIndicatorsTool

logger = structlog.get_logger(__name__)

MAX_ITERATIONS = 4

_TECHNICAL_FIELD_PATTERNS: dict[str, re.Pattern[str]] = {
    "sma20": re.compile(r"SMA\(20\): (-?\d+(?:\.\d+)?)"),
    "sma50": re.compile(r"SMA\(50\): (-?\d+(?:\.\d+)?)"),
    "rsi14": re.compile(r"RSI\(14\): (-?\d+(?:\.\d+)?)"),
    "macd_line": re.compile(r"MACD: line=(-?\d+(?:\.\d+)?)"),
    "macd_signal": re.compile(r"MACD:.*signal=(-?\d+(?:\.\d+)?)"),
    "macd_histogram": re.compile(r"MACD:.*histogram=(-?\d+(?:\.\d+)?)"),
    "bb_upper": re.compile(r"Bollinger Bands\(20\): upper=(-?\d+(?:\.\d+)?)"),
    "bb_mid": re.compile(r"Bollinger Bands\(20\):.*mid=(-?\d+(?:\.\d+)?)"),
    "bb_lower": re.compile(r"Bollinger Bands\(20\):.*lower=(-?\d+(?:\.\d+)?)"),
    "atr14": re.compile(r"ATR\(14\): (-?\d+(?:\.\d+)?)")
}

_PRICE_VS_SMA20_VALUES = ("above", "below", "at")
_MACD_TREND_VALUES = ("bullish", "bearish", "neutral")
_PRICE_VS_SMA_CLAIMS = (("price_vs_sma20", "sma20"),
                        ("price_vs_sma50", "sma50"))
_RSI_BAND_VALUES = ("oversold", "neutral", "overbought")
_CATEGORICAL_CLAIMS = (
    "price_vs_sma20", "price_vs_sma50", "macd_trend", "rsi_band")

_TECHNICAL_REPORTED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **{
            key: {
                "type": "number"
            }
            for key in _TECHNICAL_FIELD_PATTERNS
        }, "price_vs_sma20": {
            "type": "string",
            "enum": list(_PRICE_VS_SMA20_VALUES)
        },
        "price_vs_sma50": {
            "type": "string",
            "enum": list(_PRICE_VS_SMA20_VALUES)
        },
        "macd_trend": {
            "type": "string",
            "enum": list(_MACD_TREND_VALUES)
        },
        "rsi_band": {
            "type": "string",
            "enum": list(_RSI_BAND_VALUES)
        }
    },
    "required": list(_CATEGORICAL_CLAIMS),
    "additionalProperties": False
}

_TECHNICAL_TOLERANCE = 0.05

_INDICATOR_CLOSE_RE = re.compile(r"Latest close: (-?\d+(?:\.\d+)?)")
_PRINTED_POSITION_RE = {
    "price_vs_sma20": re.compile(r"Price vs SMA\(20\): (above|below|at)"),
    "price_vs_sma50": re.compile(r"Price vs SMA\(50\): (above|below|at)")
}

_PRICE_VS_SMA20_BAND_PCT = PRICE_VS_LEVEL_BAND_PCT

_MACD_TREND_EPSILON = 0.01
_MACD_NEUTRAL_ATR_FRACTION = 0.05

_RSI_BAND_RE = re.compile(r"RSI\(14\): -?\d+(?:\.\d+)? \(band: ([a-z]+)")


def _rsi_band_name(band: str) -> str:
    return band.split("-")[0].split(" ")[0]


_SYSTEM_PROMPT = (
    "You are the Technical Agent in AlphaMind's multi-agent investment "
    "research system. Your job is to read RSI, MACD, moving averages, "
    "Bollinger Bands, and ATR and form a technical view. Check more than one "
    "indicator before concluding -- a single indicator alone is not "
    "sufficient evidence (e.g. RSI overbought alone doesn't mean sell if MACD "
    "and moving averages still show a strong uptrend). "
    "Whenever you set finish_answer, also fill in reported_metrics with "
    "this run's own get_technical_indicators values for whichever of these "
    "10 exact field names you cite in your answer: sma20, sma50, rsi14, "
    "macd_line, macd_signal, macd_histogram, bb_upper, bb_mid, bb_lower, "
    'atr14 -- read the plain number shown, e.g. {"sma20": 187.42} for a '
    'line reading "SMA(20): 187.42". Always also set price_vs_sma20 and '
    "price_vs_sma50 to exactly the word (above, below or at) shown on "
    'get_technical_indicators\' "Price vs SMA(20)" / "Price vs SMA(50)" '
    "lines -- those positions are computed from the latest close for you; "
    "do not infer them from whether SMA(20) is above SMA(50). Always also "
    "set macd_trend to exactly one of: bullish, bearish, neutral, and "
    "rsi_band to the first word of the band printed next to RSI(14) "
    "(oversold, neutral or overbought). These are "
    "checked mechanically against get_technical_indicators' (and get_"
    "recent_price_history's) own output for this run, so they must be "
    "this run's real numbers, never a figure you recall from a different "
    "ticker or a prior analysis. Describe RSI(14) in plain words that agree "
    "with the band printed next to it (oversold only at or below 30, "
    "overbought only at or above 70; anything in between is neutral, e.g. "
    '"neutral, in the upper half of the range"); do not coin compound '
    'labels such as "neutral-firm" for RSI or for the overall view.')


def _parse_fresh_technical(observation: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for key, pattern in _TECHNICAL_FIELD_PATTERNS.items():
        match = pattern.search(observation)
        if match:
            values[key] = float(match.group(1))
    return values


def _actual_price_vs_sma20(latest_close: float, sma20: float) -> str:
    return price_position_vs_level(
        latest_close, sma20, _PRICE_VS_SMA20_BAND_PCT)


def _actual_macd_trend(histogram: float) -> str:
    if histogram > _MACD_TREND_EPSILON:
        return "bullish"
    if histogram < -_MACD_TREND_EPSILON:
        return "bearish"
    return "neutral"


def _accepted_macd_trends(histogram: float,
                          atr14: float | None) -> tuple[str, ...]:
    if atr14 is None:
        return (_actual_macd_trend(histogram), )
    band = max(_MACD_TREND_EPSILON, _MACD_NEUTRAL_ATR_FRACTION * abs(atr14))
    direction = (
        "bullish" if histogram > 0 else "bearish" if histogram < 0 else None)
    if abs(histogram) < band:
        return ("neutral", direction) if direction else ("neutral", )
    return (direction, ) if direction else ("neutral", )


def _indicator_latest_close(scratchpad: list[ScratchpadEntry]) -> float | None:
    indicator_close: float | None = None
    history_close: float | None = None
    for entry in scratchpad:
        if not is_successful_tool_entry(entry):
            continue
        observation = entry.observation or ""
        if entry.action == "get_technical_indicators":
            match = _INDICATOR_CLOSE_RE.search(observation)
            if match:
                indicator_close = float(match.group(1))
        elif entry.action == "get_recent_price_history":
            close = _latest_close_from_price_history(observation)
            if close is not None:
                history_close = close
    return indicator_close if indicator_close is not None else history_close


def _technical_claims_disagree_with_indicators(
        final_answer: str, reported_metrics: dict[str, Any] | None,
        scratchpad: list[ScratchpadEntry]) -> str | None:
    fresh: dict[str, float] | None = None
    printed_rsi_band: str | None = None
    printed_claims: set[str] = set()
    printed_positions: dict[str, str] = {}
    for entry in scratchpad:
        if is_successful_tool_entry(entry, "get_technical_indicators"):
            observation = entry.observation or ""
            fresh = _parse_fresh_technical(observation)
            printed_positions = {}
            for key, pattern in _PRINTED_POSITION_RE.items():
                match = pattern.search(observation)
                if match:
                    printed_positions[key] = match.group(1)
            printed_claims = set(printed_positions)
            band_match = _RSI_BAND_RE.search(observation)
            printed_rsi_band = (
                _rsi_band_name(band_match.group(1)) if band_match else None)
            if "macd_histogram" in fresh:
                printed_claims.add("macd_trend")
            if printed_rsi_band is not None:
                printed_claims.add("rsi_band")
    latest_close = _indicator_latest_close(scratchpad)
    if not fresh:
        return None
    if not reported_metrics:
        return (
            "finish_answer must be accompanied by a reported_metrics "
            "object stating this run's own sma20/sma50/rsi14/macd_line/"
            "macd_signal/macd_histogram/bb_upper/bb_mid/bb_lower/atr14 "
            "values (whichever you cite in your answer), read directly "
            "from get_technical_indicators' output above, plus "
            "price_vs_sma20/price_vs_sma50/macd_trend/rsi_band -- it "
            "was missing entirely from your last attempt.")
    for key, claimed in reported_metrics.items():
        if key in _CATEGORICAL_CLAIMS:
            continue
        if key not in fresh:
            continue
        issue = reported_number_issue(
            key, claimed, [fresh[key]], _TECHNICAL_TOLERANCE,
            f"reported_metrics.{key}={{value}} does not match "
            f"this run's own get_technical_indicators value of "
            f"{fresh[key]} for the same field -- use get_technical_"
            "indicators' real number for this run, not one recalled "
            "from a different ticker or run.")
        if issue is not None:
            return issue
    missing_claims = sorted(
        key for key in printed_claims if reported_metrics.get(key) is None)
    if missing_claims:
        return (
            f"reported_metrics must include {', '.join(missing_claims)} -- "
            "get_technical_indicators printed the facts they are checked "
            "against this run (the 'Price vs SMA(N)' lines, the MACD "
            "histogram and the RSI(14) band), so state them exactly as those "
            "lines show.")
    for claim_key, sma_key in _PRICE_VS_SMA_CLAIMS:
        position_claim = reported_metrics.get(claim_key)
        if position_claim is None:
            continue
        printed = printed_positions.get(claim_key)
        if printed is not None:
            actual = printed
        elif latest_close is None or sma_key not in fresh:
            continue
        else:
            actual = _actual_price_vs_sma20(latest_close, fresh[sma_key])
        if position_claim != actual:
            period = sma_key.removeprefix("sma")
            return (
                f"reported_metrics.{claim_key}={position_claim!r} "
                "disagrees with this run's own numbers -- the latest "
                f"close ({latest_close}) is actually {actual} SMA({period}) "
                f"({fresh.get(sma_key)}), as the 'Price vs SMA({period})' "
                f"line "
                f"shows, not {position_claim!r}.")
    macd_trend_claim = reported_metrics.get("macd_trend")
    if macd_trend_claim is not None and "macd_histogram" in fresh:
        accepted = _accepted_macd_trends(
            fresh["macd_histogram"], fresh.get("atr14"))
        if macd_trend_claim not in accepted:
            return (
                f"reported_metrics.macd_trend={macd_trend_claim!r} "
                "disagrees with this run's own MACD histogram of "
                f"{fresh['macd_histogram']} from get_technical_indicators, "
                f"which is {' or '.join(accepted)}, not {macd_trend_claim!r}.")
    if printed_rsi_band is None and "rsi14" in fresh:
        printed_rsi_band = _rsi_band_name(rsi_band(fresh["rsi14"]))
    rsi_band_claim = reported_metrics.get("rsi_band")
    if (rsi_band_claim is not None and printed_rsi_band is not None
            and str(rsi_band_claim).strip().lower() != printed_rsi_band):
        logger.info(
            "technical.rsi_band_claim_rejected", band=printed_rsi_band,
            claimed=str(rsi_band_claim)[:20], rsi14=fresh.get("rsi14"))
        return (
            f"reported_metrics.rsi_band={rsi_band_claim!r} disagrees with "
            f"this "
            f"run's RSI(14) of {fresh.get('rsi14')}, whose band printed by "
            f"get_technical_indicators is {printed_rsi_band!r} (standard "
            f"bands: "
            "<=30 oversold, >=70 overbought, otherwise neutral) -- set it to "
            "that band and describe RSI in plain words consistent with it.")
    return None


class TechnicalAgent(BaseAgent):

    async def run(self, state: AnalysisState) -> AgentOutput:
        feedback = state.reflection_feedback.get(self.role)
        indicators_tool = GetTechnicalIndicatorsTool(
            analysis_ticker=state.ticker)
        prefetched = await prefetch_tool_calls([
            (indicators_tool, {
                "ticker": state.ticker,
                "lookback_days": 90
            })
        ])
        loop = ReActAgentLoop(
            role=self.role, tools=[
                indicators_tool,
                GetRecentPriceHistoryTool(analysis_ticker=state.ticker)
            ], system_prompt=_SYSTEM_PROMPT, max_iterations=MAX_ITERATIONS,
            finish_answer_validator=InternalTokenValidator(
                self.role.value, _technical_claims_disagree_with_indicators,
                once_checks=[
                    (OWN_EVIDENCE_CHECK, own_evidence_figure_check())
                ]), reported_metrics_schema=_TECHNICAL_REPORTED_SCHEMA)
        result = await loop.run(
            f"Form a technical view on {state.ticker} using at least two "
            "distinct indicators.", extra_context=combine_context(
                feedback, state.procedural_hints.get(self.role)),
            prefetched_evidence=prefetched)
        verification_warnings: list[str] = []
        summary = flag_unresolved_issue(
            result, result.final_answer, verification_warnings,
            event="technical.unresolved_indicator_mismatch_after_retries",
            ticker=state.ticker, consequence=(
                ", and this was not corrected even after being flagged for "
                "recomputation -- treat any indicator value or trend "
                "characterization below as unverified until manually "
                "re-checked."))
        latest_close = _indicator_latest_close(result.scratchpad)
        return self.completed_output(
            result, feedback=feedback, summary=summary,
            verification_warnings=verification_warnings, raw_output={
                "key_facts": ({
                    "reference_price": latest_close
                } if latest_close is not None else {})
            })
