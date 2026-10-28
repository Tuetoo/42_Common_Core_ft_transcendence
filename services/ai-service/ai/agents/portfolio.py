from collections.abc import Callable, Sequence
from typing import Any
import structlog
from ai.agents.evidence_grounding import (
    extract_reported_percentages, final_answer_has_unverifiable_percentages,
    wrap_verification_warning)
from ai.agents.finish_checks import (OWN_EVIDENCE_CHECK,
                                     InternalTokenValidator,
                                     own_evidence_figure_check)
from ai.agents.loop import (SCORE_UNRESOLVED, BaseAgent, ReActAgentLoop,
                            ScratchpadEntry, combine_context,
                            is_successful_tool_entry,
                            prefetch_tool_call, prefetch_tool_calls)
from ai.graph.state import AgentOutput, AnalysisState
from ai.tools.portfolio_tool import (GetPortfolioCompositionTool,
                                     RecomputeScenarioTool)
from shared.enums import AgentRole

logger = structlog.get_logger(__name__)

MAX_ITERATIONS = 7

_SYSTEM_PROMPT = (
    "You are the Portfolio Agent in AlphaMind's multi-agent investment "
    "research system. Your job is specifically to evaluate what happens to "
    "the WHOLE portfolio if the user acts on this analysis -- concentration "
    "and correlation risk -- NOT this ticker's own standalone risk (that is "
    "the separate Risk Agent's job). "
    "get_portfolio_composition takes no real arguments and its real result "
    "cannot change mid-run -- call it ONCE at the start of your real "
    "reasoning, then move straight to recompute_scenario. Calling "
    "get_portfolio_composition a second time produces the exact same real "
    "observation as the first (there is no different real argument that "
    "could change the outcome), and only a few total real turns are "
    "available -- a real "
    "wasted turn here is a real turn no longer available to actually reach "
    "a real conclusion. "
    "Calling recompute_scenario repeatedly with near-identical parameters "
    "(the same position size, asked again), never something genuinely "
    "different, reaches the iteration cap without ever finishing despite "
    "already having a real, complete answer. "
    "Two recompute_scenario results for the ticker under analysis are "
    "normally already in your scratchpad, computed in code before your "
    "first turn (share counts worth about 5% and 10% of the portfolio's "
    "total net value). Base your answer on those two results and quote "
    "their figures verbatim. Call recompute_scenario yourself only when "
    "they are missing or failed, or for a genuinely different "
    "`additional_shares` value -- never a share count already computed. If "
    "the two results are materially similar (concentration risk doesn't "
    "meaningfully change either way), that similarity IS your answer -- "
    "finish with it; don't keep trying more variants hoping for a "
    "different outcome. "
    "get_portfolio_composition also reports available cash -- factor real "
    "capital constraints into your judgment, not just existing holdings' "
    "structure (e.g. a position size that would require more cash than is "
    "actually available is a real constraint worth naming). "
    "Any word describing a multiple or fraction of the SHARE COUNT for one "
    "tool call, sitting close to a real PERCENTAGE from another tool's "
    "output, is a real, identified confusion risk: the percentage can be "
    "misstated using the same multiplier. When your final answer states "
    "any percentage a tool already "
    "reported (current weight, post-scenario weight, anything with a % "
    "sign), copy those digits verbatim from the tool's own observation "
    "text -- never retype, round, or apply any arithmetic to it, including "
    "arithmetic you were asked to apply to a DIFFERENT number (like a "
    "share count) elsewhere in the same turn. "
    "Knowledge-graph context for your held "
    "tickers is provided alongside this prompt (you do not call a tool for "
    "it) and can include more than one dated reading of the same changing "
    "metric (e.g. a volatility or price-level figure), an old, stale reading "
    "right next to a fresher one. When you see more than one reading of "
    "the same metric "
    "in that context, treat the one tagged [MOST RECENT reading] (or "
    "otherwise the latest date shown) as current -- never cite one marked "
    "[SUPERSEDED ...], or an older date, as "
    "today's value. This is separate from the verbatim-copy rule above for "
    "percentages a TOOL reports in this run -- this rule is specifically "
    "about picking which KG-provided reading is current in the first "
    "place. "
    "A percentage for a hypothetical scenario that no tool call returned is "
    "a freehand estimate, and because post-purchase concentration is NOT "
    "linear in share count, such an estimate is measurably wrong and drifts "
    "to a different wrong value on every retry. Retrying does NOT "
    "fix this on its own -- a retry has no memory of a prior attempt's own "
    "reasoning, only a fresh warning that some number was unverified. "
    "EVERY hypothetical scenario mentioned anywhere in "
    "your final answer -- meaning every distinct additional_shares value "
    "you discuss -- must have its own real recompute_scenario call backing "
    "whatever percentage you state for it. If you want to compare two "
    "scenarios, call recompute_scenario twice, once per scenario, and quote "
    "each one's own real result verbatim (see the verbatim-copy rule "
    "above). Never state a percentage for a scenario you did not call "
    "recompute_scenario for -- if you are out of turns to call it again, "
    'describe that scenario only in qualitative terms (e.g. "a larger '
    'position would further concentrate risk") rather than putting an '
    "unverified number on it. "
    "Every successful recompute_scenario result ends with a line "
    '"cost=..., available_cash=..., shortfall=..." computed in code -- '
    "when shortfall is above 0 that scenario cannot be funded from current "
    "cash; say so using those figures. A buy scenario also has a line "
    '"committed_to_other_open_buy_tiers=..., buy_budget=..., '
    "budget_shortfall=...\": cash already reserved by other tickers' open "
    "BUY tiers is not free for a new buy, so describe what can be funded "
    "with buy_budget (not available_cash), and when budget_shortfall is "
    "above 0 say the scenario exceeds the remaining BUY budget. buy_budget "
    "is the remaining, uncommitted amount; only "
    "committed_to_other_open_buy_tiers is committed -- never call the "
    "remaining buy_budget committed. If a tool "
    "reports that a live price is unavailable, the portfolio total cannot "
    "be computed this run: do not estimate any weight yourself. Your "
    "finish_answer is checked mechanically: it is rejected until you have a "
    "successful "
    "get_portfolio_composition result and at least one successful "
    "recompute_scenario result for the ticker under analysis, and while "
    "it contains any percentage no tool result or knowledge-graph context "
    "reported.")


def _trusted_percentages(
        scratchpad: Sequence[ScratchpadEntry],
        portfolio_kg_context: str | None) -> set[str]:
    return extract_reported_percentages(
        *(
            str(entry.observation) for entry in scratchpad
            if is_successful_tool_entry(entry)), portfolio_kg_context)


_PORTFOLIO_VALIDATION_SUFFIX = (
    "To keep a number for a scenario that is not in that list, call "
    "recompute_scenario now with that additional_shares value and quote "
    "its result verbatim; otherwise remove that scenario's number "
    "(describe it in words), then set finish_answer again.")


def _computed_scenarios(scratchpad: list[ScratchpadEntry],
                        ticker: str) -> list[str]:
    lines: list[str] = []
    for entry in scratchpad:
        if not is_successful_tool_entry(entry, "recompute_scenario"):
            continue
        action_input = entry.action_input or {}
        if str(action_input.get("ticker", "")).strip().upper() != ticker:
            continue
        observation_lines = str(entry.observation).strip().splitlines()
        if not observation_lines:
            continue
        first_line = observation_lines[0][:200]
        lines.append(
            f"additional_shares={action_input.get('additional_shares')} "
            f"-> {first_line}")
    return lines


def _portfolio_finish_validator(
    ticker: str, portfolio_kg_context: str | None
) -> Callable[[str, dict[str, Any] | None, list[ScratchpadEntry]], str | None]:
    wanted = ticker.strip().upper()

    def _validate(
            final_answer: str, reported_metrics: dict[str, Any] | None,
            scratchpad: list[ScratchpadEntry]) -> str | None:
        del reported_metrics
        if not any(
                is_successful_tool_entry(e, "get_portfolio_composition")
                for e in scratchpad):
            return (
                "you have no successful get_portfolio_composition result this "
                "run -- call it (it takes no arguments) before concluding.")
        if not any(
                is_successful_tool_entry(e, "recompute_scenario")
                and str((e.action_input or {}).get("ticker", "")).strip(
                ).upper() == wanted for e in scratchpad):
            return (
                f"you have no successful recompute_scenario result for "
                f"{wanted} "
                "this run -- call recompute_scenario with ticker "
                f"{wanted!r} and a share count before concluding (a failed "
                "scenario, or one for another ticker, does not count).")
        mismatches = final_answer_has_unverifiable_percentages(
            final_answer,
            _trusted_percentages(scratchpad, portfolio_kg_context))
        if mismatches:
            distinct = ", ".join(dict.fromkeys(mismatches))
            computed = "; ".join(_computed_scenarios(scratchpad, wanted))
            return (
                f"your answer states {distinct}, which no successful tool "
                "result or portfolio knowledge-graph context above reported. "
                f"The scenarios you have actually computed for {wanted} this "
                f"run are exactly these: {computed}. A percentage for any "
                "other share count did not come from a tool -- that scenario "
                "has not been computed yet, whatever an earlier thought of "
                "yours says.")
        return None

    return _validate


async def _prefetch_scenarios(tool: RecomputeScenarioTool,
                              ticker: str) -> list[ScratchpadEntry]:
    try:
        counts = await tool.suggested_share_counts(ticker)
    except Exception as exc:
        logger.warning(
            "portfolio.scenario_prefetch_skipped", ticker=ticker,
            error=f"{type(exc).__name__}: {exc}")
        return []
    if counts is None:
        logger.info("portfolio.scenario_prefetch_skipped", ticker=ticker)
        return []
    return [
        await prefetch_tool_call(
            tool, {
                "ticker": ticker,
                "additional_shares": shares
            }) for shares in counts
    ]


class PortfolioAgent(BaseAgent):

    def __init__(self, role: AgentRole, portfolio_id: str | None) -> None:
        super().__init__(role)
        self.portfolio_id = portfolio_id

    async def run(self, state: AnalysisState) -> AgentOutput:
        feedback = state.reflection_feedback.get(self.role)

        prior_fabrication_note = None
        if feedback is not None:
            prior_output = state.agent_outputs.get(self.role)
            prior_warnings = ((prior_output.raw_output
                               or {}).get("verification_warnings")
                              if prior_output is not None else None)
            if prior_warnings:
                prior_fabrication_note = (
                    "Your own PRIOR attempt this analysis was flagged for "
                    "stating a figure no real tool call or KG context "
                    "actually reported: " + " ".join(prior_warnings) +
                    " Do not repeat that same figure for that same "
                    "scenario -- either call recompute_scenario again for "
                    "it and quote the real result verbatim, or describe "
                    "that scenario only in qualitative terms if you are "
                    "out of turns to call it again.")

        composition_tool = GetPortfolioCompositionTool(self.portfolio_id)
        scenario_tool = RecomputeScenarioTool(self.portfolio_id)
        prefetched = await prefetch_tool_calls([(composition_tool, {})])
        if prefetched:
            prefetched += await _prefetch_scenarios(
                scenario_tool, state.ticker)
        loop = ReActAgentLoop(
            role=self.role, tools=[composition_tool, scenario_tool],
            system_prompt=_SYSTEM_PROMPT, max_iterations=MAX_ITERATIONS,
            finish_answer_validator=InternalTokenValidator(
                self.role.value,
                _portfolio_finish_validator(
                    state.ticker, state.portfolio_kg_context), once_checks=[(
                        OWN_EVIDENCE_CHECK, own_evidence_figure_check())]),
            finish_validation_suffix=_PORTFOLIO_VALIDATION_SUFFIX)
        result = await loop.run(
            f"Assess how acting on {state.ticker} (e.g. buying more) would "
            "affect this portfolio's concentration/correlation risk.",
            extra_context=combine_context(
                feedback, state.procedural_hints.get(self.role),
                state.portfolio_kg_context, prior_fabrication_note),
            prefetched_evidence=prefetched)

        trusted_percentages = _trusted_percentages(
            result.scratchpad, state.portfolio_kg_context)
        mismatches = final_answer_has_unverifiable_percentages(
            result.final_answer, trusted_percentages)
        summary = result.final_answer
        verification_warnings: list[str] = []
        if mismatches:
            logger.warning(
                "portfolio.unverified_percentage_in_final_answer",
                mismatched_values=mismatches,
                trusted_values=sorted(trusted_percentages))
            distinct_mismatches = list(dict.fromkeys(mismatches))
            reason = (
                f"this analysis states {', '.join(distinct_mismatches)}, "
                f"{'a figure' if len(distinct_mismatches) == 1 else 'figures'}"
                " "
                f"none of the real tool calls or portfolio KG context above "
                f"actually reported (trusted values: "
                f"{', '.join(sorted(trusted_percentages)) or 'none'}) -- "
                f"treat any percentage in the text below as unverified "
                f"until manually re-checked against the real evidence.")
            summary = wrap_verification_warning(reason, result.final_answer)
            verification_warnings.append(reason)

        output = self.completed_output(
            result, feedback=feedback, summary=summary,
            verification_warnings=verification_warnings, raw_output={
                "final_answer": result.final_answer,
                "injected_context": state.portfolio_kg_context
            })
        if mismatches:
            output = output.model_copy(update={
                "score": min(output.score, SCORE_UNRESOLVED),
                "is_well_supported": False
            })
        return output
