import re
from typing import Any
import structlog
from ai.agents.evidence_grounding import wrap_verification_warning
from ai.agents.finish_checks import (OWN_EVIDENCE_CHECK,
                                     InternalTokenValidator,
                                     own_evidence_figure_check,
                                     reported_number_issue)
from ai.agents.loop import (BaseAgent, ReActAgentLoop, ScratchpadEntry,
                            derive_agent_quality, is_successful_tool_entry,
                            prefetch_tool_calls)
from ai.graph.state import AgentOutput, AnalysisState
from ai.memory.procedural import combine_context
from ai.tools.market_tool import (CLOSE_RE, GetCurrentPriceTool,
                                  GetRecentPriceHistoryTool)
from ai.tools.market_tool import \
    latest_close_from_price_history as _latest_close_from_price_history
from shared.enums import AgentStatus

logger = structlog.get_logger(__name__)

MAX_ITERATIONS = 5

_CURRENT_PRICE_RE = re.compile(r"current price: (-?\d+(?:\.\d+)?)")
_HIGH_RE = re.compile(r"high=(-?\d+(?:\.\d+)?)")
_LOW_RE = re.compile(r"low=(-?\d+(?:\.\d+)?)")

_MARKET_METRIC_NAMES = ("latest_close", "period_high", "period_low")
_MARKET_REPORTED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        name: {
            "type": "number"
        }
        for name in _MARKET_METRIC_NAMES
    },
    "additionalProperties": False
}
_MARKET_METRIC_TOLERANCE = 0.005


def _current_price_from_quote(observation: str) -> float | None:
    match = _CURRENT_PRICE_RE.search(observation)
    return float(match.group(1)) if match else None


def _history_windows(
        scratchpad: list[ScratchpadEntry]) -> list[dict[str, float]]:
    windows: list[dict[str, float]] = []
    for entry in scratchpad:
        if not is_successful_tool_entry(entry, "get_recent_price_history"):
            continue
        observation = entry.observation or ""
        closes = [float(v) for v in CLOSE_RE.findall(observation)]
        highs = [float(v) for v in _HIGH_RE.findall(observation)]
        lows = [float(v) for v in _LOW_RE.findall(observation)]
        if closes and highs and lows:
            windows.append({
                "latest_close": closes[-1],
                "period_high": max(highs),
                "period_low": min(lows)
            })
    return windows


def _current_quotes(scratchpad: list[ScratchpadEntry]) -> list[float]:
    quotes: list[float] = []
    for entry in scratchpad:
        if is_successful_tool_entry(entry, "get_current_price"):
            price = _current_price_from_quote(entry.observation or "")
            if price is not None:
                quotes.append(price)
    return quotes


def _market_claims_disagree_with_prices(
        final_answer: str, reported_metrics: dict[str, Any] | None,
        scratchpad: list[ScratchpadEntry]) -> str | None:
    del final_answer
    windows = _history_windows(scratchpad)
    quotes = _current_quotes(scratchpad)
    if not windows and not quotes:
        return None
    required = list(_MARKET_METRIC_NAMES) if windows else ["latest_close"]
    metrics = reported_metrics or {}
    missing = [name for name in required if metrics.get(name) is None]
    if missing:
        return (
            "finish_answer must be accompanied by reported_metrics with "
            f"{', '.join(missing)} copied exactly from this run's own price "
            "tool output above (latest_close = the most recent close, "
            "period_high/period_low = the highest high / lowest low of the "
            "bars shown).")
    candidates: dict[str, list[float]] = {
        "latest_close": [w["latest_close"] for w in windows] or quotes,
        "period_high": [w["period_high"] for w in windows],
        "period_low": [w["period_low"] for w in windows]
    }
    for name in _MARKET_METRIC_NAMES:
        if name not in metrics:
            continue
        allowed = candidates[name]
        issue = reported_number_issue(
            name, metrics[name], allowed, _MARKET_METRIC_TOLERANCE,
            f"reported_metrics.{name}={{value}} does not match this run's "
            f"own price tool output ({', '.join(str(v) for v in allowed)}) "
            "-- use the exact number your tool printed this run.")
        if issue is not None:
            return issue
    return None


_SYSTEM_PROMPT = (
    "You are the Market Agent in AlphaMind's multi-agent investment research "
    "system. Your job is to establish the price trend and volume pattern for "
    "a ticker. Pull price history at varying intervals/windows until you are "
    "confident in a trend conclusion -- or confident the signal is genuinely "
    "mixed, which is also a valid conclusion. Do not invent price data you "
    "have not retrieved via a tool. "
    "Whenever you set finish_answer, also fill in reported_metrics with "
    "exactly these field names, copied from this run's own tool output: "
    "latest_close (the most recent close= value from get_recent_price_"
    "history, or the get_current_price quote if you fetched no history), "
    "period_high (the highest high= value among the bars shown) and "
    "period_low (the lowest low= value among the bars shown). They are "
    "checked mechanically against your tool output. When you describe "
    "volume, use the up/down/flat direction the tool printed for each "
    "highest-volume session; never call a down day a buying or upward "
    "session.")


class MarketAgent(BaseAgent):

    async def run(self, state: AnalysisState) -> AgentOutput:
        feedback = state.reflection_feedback.get(self.role)
        history_tool = GetRecentPriceHistoryTool(analysis_ticker=state.ticker)
        prefetched = await prefetch_tool_calls([(
            history_tool, {
                "ticker": state.ticker,
                "interval": "1d",
                "lookback_days": 30
            })])
        loop = ReActAgentLoop(
            role=self.role, tools=[
                GetCurrentPriceTool(analysis_ticker=state.ticker), history_tool
            ], system_prompt=_SYSTEM_PROMPT, max_iterations=MAX_ITERATIONS,
            finish_answer_validator=InternalTokenValidator(
                self.role.value, _market_claims_disagree_with_prices,
                once_checks=[
                    (OWN_EVIDENCE_CHECK, own_evidence_figure_check())
                ]), reported_metrics_schema=_MARKET_REPORTED_SCHEMA)
        result = await loop.run(
            f"Establish the price trend and volume pattern for "
            f"{state.ticker}.", extra_context=combine_context(
                feedback, state.procedural_hints.get(self.role)),
            prefetched_evidence=prefetched)

        reference_price: float | None = None
        price_history_close: float | None = None
        for entry in result.scratchpad:
            if is_successful_tool_entry(entry, "get_current_price"):
                price = _current_price_from_quote(entry.observation or "")
                if price is not None:
                    reference_price = price
            if is_successful_tool_entry(entry, "get_recent_price_history"):
                close = _latest_close_from_price_history(
                    entry.observation or "")
                if close is not None:
                    price_history_close = close
        if reference_price is None:
            reference_price = price_history_close

        summary = result.final_answer
        verification_warnings: list[str] = []
        if result.unresolved_finish_validation_issue is not None:
            logger.warning(
                "market.unresolved_finish_issue_after_retries",
                ticker=state.ticker,
                issue=result.unresolved_finish_validation_issue)
            reason = (
                f"{result.unresolved_finish_validation_issue} -- this was not "
                "corrected even after being flagged -- treat the price "
                "figures below as unverified until manually re-checked "
                "against this run's tool output.")
            summary = wrap_verification_warning(reason, summary)
            verification_warnings.append(reason)

        agent_score, agent_is_well_supported = derive_agent_quality(
            result, is_retry=feedback is not None,
            verification_warnings=verification_warnings)
        return AgentOutput(
            role=self.role, status=AgentStatus.COMPLETED, summary=summary,
            score=agent_score, is_well_supported=agent_is_well_supported,
            iterations_used=result.iterations_used,
            hit_iteration_cap=result.hit_iteration_cap,
            retry_of_feedback=feedback, raw_output={
                "scratchpad": [vars(e) for e in result.scratchpad],
                "verification_warnings": verification_warnings,
                "key_facts": ({
                    "reference_price": reference_price
                } if reference_price is not None else {}),
                "finish_meta": result.finish_meta
            })
