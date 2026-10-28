import re
from typing import Any
import structlog
from ai.agents.finish_checks import InternalTokenValidator
from ai.agents.loop import (BaseAgent, ReActAgentLoop, ScratchpadEntry,
                            is_thin_evidence)
from ai.graph.state import AgentOutput, AnalysisState
from ai.tools.news_tool import (SearchNewsTool, live_search_yield,
                                ran_live_search)
from config import settings
from shared.enums import AgentRole, AgentStatus

logger = structlog.get_logger(__name__)

MAX_ITERATIONS = 4


def _news_ran_fresh_search(news_output: AgentOutput | None) -> bool | None:
    if news_output is None:
        return None
    if not isinstance(news_output.raw_output, dict):
        return None
    scratchpad = news_output.raw_output.get("scratchpad")
    if not isinstance(scratchpad, list):
        return None
    return ran_live_search(e for e in scratchpad if isinstance(e, dict))


def _news_live_yield(news_output: AgentOutput | None) -> int | None:
    if news_output is None or news_output.status != AgentStatus.COMPLETED:
        return None
    if not isinstance(news_output.raw_output, dict):
        return None
    scratchpad = news_output.raw_output.get("scratchpad")
    if not isinstance(scratchpad, list):
        return None
    return live_search_yield(scratchpad)


_SENTIMENT_LABELS = (
    "very negative", "negative", "neutral", "mixed", "positive",
    "very positive")

_SYSTEM_PROMPT = (
    "You are the Sentiment Agent in AlphaMind's multi-agent investment "
    "research "
    "system. Your job is to characterize current market sentiment for a "
    "ticker "
    "based on recent news and discussion, in QUALITATIVE terms only. There is "
    "no independent sentiment-measurement tool anywhere in this system -- a "
    "specific-looking numeric score (e.g. '0.35') would be an unsupported "
    "invention, not a real measurement, so do not state one anywhere in your "
    "answer. State your assessment explicitly in your final answer, in "
    "exactly this format so it can be extracted automatically: "
    f"'Sentiment: <label>', where <label> is exactly one of: "
    f"{', '.join(_SENTIMENT_LABELS)} (e.g. 'Sentiment: mixed'). Describe "
    "intensity and reasoning in words, not numbers (e.g. 'cautiously "
    "optimistic, tempered by unresolved litigation risk'). "
    "Only when neither the News Agent's live search nor your own search "
    "returned any relevant article: do not write a 'Sentiment:' line at "
    "all; say that no sentiment reading is possible this run. "
    "Bracketed markers and catalog= fields in search results are for you, "
    "not the reader: name the outlet itself. "
    "Whether you need to search "
    "yourself is NOT your own subjective call -- the context below will "
    "always explicitly state whether the News Agent actually ran a live "
    "search (search_news/web_search) this run, or only checked its own "
    "archived prior notes without searching anything fresh. If the context "
    "says News did NOT run a live search this run, you MUST independently "
    "call search_news at least once yourself before finishing -- do not "
    "simply restate News's summary in that case, since it may itself be "
    "resting on stale, previously-archived coverage rather than anything "
    "confirmed fresh today. Only skip searching yourself when the context "
    "confirms News already ran a real, fresh search this run AND that "
    "signal looks clear (not mixed or thin). If no News Agent context is "
    "provided at all, always search yourself using your own tools.")

_SENTIMENT_LABEL_PATTERN = re.compile(
    "sentiment:?\\s*(" + "|".join(
        re.escape(label)
        for label in sorted(_SENTIMENT_LABELS, key=len, reverse=True)) + ")",
    re.IGNORECASE)


def _extract_sentiment_label(final_answer: str) -> str | None:
    match = _SENTIMENT_LABEL_PATTERN.search(final_answer)
    if match is None:
        return None
    return match.group(1).lower()


NO_EVIDENCE_LABEL_MESSAGE = (
    "No-evidence check: neither the News Agent's live search nor your own "
    "search returned any relevant article this run, so there is no "
    "sentiment reading to label. Remove the 'Sentiment:' line and say "
    "plainly that no sentiment reading is possible this run.")
_SENTIMENT_VALIDATION_SUFFIX = (
    "Fix only what the check above names, keep every other correct fact, "
    "then set finish_answer again.")


def _news_fallback_unverified(news_output: AgentOutput | None) -> bool:
    raw = news_output.raw_output if news_output is not None else None
    return isinstance(raw, dict) and bool(
        raw.get("fallback_sources_unverified"))


def _build_extra_context(
        state: AnalysisState, feedback: str | None,
        procedural_hints: str | None) -> str | None:
    parts = []
    if feedback:
        parts.append(f"Reflection feedback: {feedback}")
    news_output = state.agent_outputs.get(AgentRole.NEWS)
    if news_output is not None:
        parts.append(f"News Agent's summary: {news_output.summary}")
        ran_fresh_search = _news_ran_fresh_search(news_output)
        if news_output.status == AgentStatus.FAILED:
            parts.append(
                "Freshness signal: the News Agent FAILED this run and "
                "searched nothing -- there is no News evidence to build on. "
                "You must call search_news yourself before finishing.")
        elif ran_fresh_search is True and _news_live_yield(news_output) == 0:
            parts.append(
                "Freshness signal: News ran a live search this run but it "
                "returned 0 relevant articles -- there is no fresh News "
                "evidence to build on. You must call search_news yourself "
                "before finishing.")
        elif ran_fresh_search is True:
            parts.append(
                "Freshness signal: News DID run a live search "
                "(search_news/web_search) this run -- its summary above "
                "reflects a real, current search, not only its own prior "
                "archived notes.")
        elif ran_fresh_search is False:
            parts.append(
                "Freshness signal: News did NOT run any live search this "
                "run (it only checked its own archived prior notes, if "
                "that) -- its summary above may be resting on stale, "
                "previously-covered material. You must independently "
                "call search_news at least once yourself before "
                "finishing; do not simply restate News's summary as-is.")
        if isinstance(
                news_output.raw_output,
                dict) and news_output.raw_output.get("verification_warnings"):
            parts.append(
                "Caveat: the News Agent's summary above contains at least "
                "one unverified, web-search-derived claim (flagged with "
                "its own [VERIFICATION WARNING: ...] banner) -- do not "
                "treat anything from that summary as settled fact when "
                "characterizing sentiment; describe it as unconfirmed if "
                "you rely on it at all.")
    if procedural_hints:
        parts.append(procedural_hints)
    return "\n\n".join(parts) if parts else None


class SentimentAgent(BaseAgent):

    async def run(self, state: AnalysisState) -> AgentOutput:
        feedback = state.reflection_feedback.get(self.role)
        news_yield = _news_live_yield(state.agent_outputs.get(AgentRole.NEWS))
        news_yielded = news_yield is not None and news_yield > 0

        def _evidence_yield(scratchpad: list[ScratchpadEntry]) -> int | None:
            return (
                news_yield if news_yielded else live_search_yield(scratchpad))

        news_output = state.agent_outputs.get(AgentRole.NEWS)

        def _label_without_evidence(
                final_answer: str, reported_metrics: dict[str, Any] | None,
                scratchpad: list[ScratchpadEntry]) -> str | None:
            del reported_metrics
            if news_yielded or live_search_yield(scratchpad):
                return None
            if _extract_sentiment_label(final_answer) is None:
                return None
            return NO_EVIDENCE_LABEL_MESSAGE

        loop = ReActAgentLoop(
            role=self.role, tools=[SearchNewsTool(ticker=state.ticker)],
            system_prompt=_SYSTEM_PROMPT, max_iterations=MAX_ITERATIONS,
            require_tool_evidence=not news_yielded,
            evidence_yield=_evidence_yield, anchor_fallback_reference=(
                news_output.summary if news_yielded and news_output is not None
                else None), finish_answer_validator=InternalTokenValidator(
                    self.role.value, once_checks=[
                        ("label_without_evidence", _label_without_evidence)
                    ]), finish_validation_suffix=_SENTIMENT_VALIDATION_SUFFIX,
            thinking_budget=settings.SENTIMENT_THINKING_BUDGET)
        result = await loop.run(
            f"Characterize current market sentiment for {state.ticker} "
            "qualitatively.", extra_context=_build_extra_context(
                state, feedback, state.procedural_hints.get(self.role)))
        own_yield = live_search_yield(result.scratchpad)
        no_evidence = not news_yielded and not own_yield
        summary = result.final_answer
        if no_evidence:
            logger.warning("sentiment.no_evidence", ticker=state.ticker)
            written = _extract_sentiment_label(result.final_answer)
            if written is not None:
                logger.warning(
                    "sentiment.label_written_without_evidence",
                    ticker=state.ticker, label=written)
            summary = (
                "[NO EVIDENCE: neither the News Agent's live search nor this "
                "agent's own search returned any relevant article this run; "
                "no sentiment reading is possible.]\n\n" + summary)
        evidence_yield = news_yield if news_yielded else own_yield
        return self.completed_output(
            result, feedback=feedback, summary=summary,
            verification_warnings=[], no_evidence=no_evidence,
            thin_evidence=(
                is_thin_evidence(evidence_yield)
                or (news_yielded and _news_fallback_unverified(news_output))),
            raw_output={
                "no_evidence": no_evidence,
                "evidence_yield": evidence_yield
            })
