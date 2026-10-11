import asyncio
import difflib
import itertools
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
import structlog
from ai.graph.state import AgentOutput, AnalysisState
from ai.llm.base import (AGENT_LLM_CALL_RETRIES, LLMMessage,
                         LLMNonEnglishOutputError, LLMResponse, LLMTier)
from ai.llm.json_fence import parse_json_dict
from ai.llm.router import LLMRouter
from ai.tools.base import NON_EVIDENTIAL_TOOLS, Tool, is_evidence_record
from config import settings
from shared.enums import AgentRole

logger = structlog.get_logger(__name__)

_REACT_LOOP_MAX_TOKENS = 4096


def _build_react_step_schema(
    tools: Sequence[Tool],
    reported_metrics_schema: dict[str, object] | None = None
) -> dict[str, object]:
    action_input_arms: list[dict[str, object]] = [
        dict(getattr(tool, "parameters", {"type": "object"})) for tool in tools
    ]
    action_input_arms.append({"type": "null"})
    reported_metrics_arm: dict[str, object] = (
        dict(reported_metrics_schema)
        if reported_metrics_schema is not None else {
            "type": "object"
        })
    return {
        "type": "object",
        "properties": {
            "thought": {
                "type": "string"
            },
            "action": {
                "anyOf": [{
                    "type": "string"
                }, {
                    "type": "null"
                }]
            },
            "action_input": {
                "anyOf": action_input_arms
            },
            "finish_answer": {
                "anyOf": [{
                    "type": "string"
                }, {
                    "type": "null"
                }]
            },
            "reported_metrics": {
                "anyOf": [reported_metrics_arm, {
                    "type": "null"
                }]
            }
        },
        "required": ["thought"]
    }


_MAX_CORRECTION_RETRIES = 2

_MAX_ZERO_EVIDENCE_RETRIES = 2

MAX_FINISH_VALIDATION_RETRIES = 2

_DEFAULT_FINISH_VALIDATION_SUFFIX = (
    "Recompute using the actual figures already shown above in this "
    "scratchpad, then set finish_answer again with the corrected value.")

_MAX_DUPLICATE_BLOCK_RETRIES = 2

_NEAR_DUPLICATE_OBSERVATION_THRESHOLD = 0.85
_NEAR_DUPLICATE_COMPARISON_CHARS = 2000


def _is_near_duplicate_observation(
        new_observation: str, scratchpad: "list[ScratchpadEntry]") -> bool:
    new_sig = new_observation[:_NEAR_DUPLICATE_COMPARISON_CHARS]
    recent_observations = [
        e.observation for e in scratchpad[-3:] if e.observation
    ]
    return any(
        difflib.SequenceMatcher(
            None, new_sig, obs[:_NEAR_DUPLICATE_COMPARISON_CHARS]).ratio() >=
        _NEAR_DUPLICATE_OBSERVATION_THRESHOLD for obs in recent_observations)


def _same_action_signature(
        action: str | None, action_input: dict[str, Any] | None,
        signature: tuple[str | None, str] | None) -> bool:
    if signature is None:
        return False
    sig_action, sig_input_json = signature
    return (
        action == sig_action
        and json.dumps(action_input or {}, sort_keys=True) == sig_input_json)


@dataclass
class ScratchpadEntry:
    thought: str
    action: str | None
    action_input: dict[str, Any] | None
    observation: str | None
    is_tool_result: bool = True
    tool_error: bool = False
    prefetched: bool = False
    peer_ticker: str | None = None


PREFETCH_THOUGHT_PREFIX = "(fetched in code before the first turn"


async def prefetch_tool_call(
        tool: Tool, action_input: dict[str, Any]) -> ScratchpadEntry:
    try:
        observation = await tool.run(**action_input)
    except Exception as exc:
        observation = (
            f"Prefetched tool call failed: {tool.name}: {type(exc).__name__}: "
            f"{exc}")
    return ScratchpadEntry(
        thought=f"{PREFETCH_THOUGHT_PREFIX}: {tool.name})", action=tool.name,
        action_input=action_input, observation=observation, prefetched=True)


async def prefetch_tool_calls(
        calls: Sequence[tuple[Tool, dict[str, Any]]]) -> list[ScratchpadEntry]:
    if not settings.AGENT_PREFETCH_ENABLED:
        return []
    return list(
        await asyncio.gather(
            *(
                prefetch_tool_call(tool, action_input)
                for tool, action_input in calls)))


def _coerce_finish_answer(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def tool_reported_error(observation: str) -> bool:
    first_line = observation.lstrip().split("\n", 1)[0]
    return any(pattern.match(first_line) for pattern in _TOOL_ERROR_PATTERNS)


_TOOL_ERROR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[A-Z][A-Za-z/ ]{0,60} failed(?: for |:)"),
    re.compile(r"Not enough (?:valid )?price history for "),
    re.compile(r"Unknown tool "),
    re.compile(r"Tool .{1,80} was called with invalid arguments"),
    re.compile(r"Tool .{1,80} raised an error: "))


def is_successful_tool_entry(
        entry: ScratchpadEntry, action: str | None = None) -> bool:
    return bool(
        entry.is_tool_result and not entry.tool_error and entry.observation
        and not entry.peer_ticker
        and (action is None or entry.action == action))


def _is_evidence_entry(entry: ScratchpadEntry) -> bool:
    return is_evidence_record(vars(entry))


def _is_reference_entry(entry: ScratchpadEntry) -> bool:
    return bool(
        entry.is_tool_result and not entry.tool_error and entry.observation
        and (entry.action or "") not in NON_EVIDENTIAL_TOOLS)


def has_successful_tool_evidence(
        scratchpad: Sequence[ScratchpadEntry]) -> bool:
    return any(_is_evidence_entry(e) for e in scratchpad)


NUMBER_MATCH_REL_TOLERANCE = 0.01
_ANCHOR_WORD_RUN = 3
_ANCHOR_MIN_WORD_LETTERS = 3
_ANCHOR_DATE_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)?\b")
_ANCHOR_HEADER_COUNT_RE = re.compile(
    r"\b\d[\d,]*(?:\s+of\s+\d[\d,]*)?\s+"
    r"(?:(?:usable|daily|real|valid|trading)\s+)?"
    r"(?:bars|passages|articles|results|items|entries|chunks|rows)\b",
    re.IGNORECASE)
_ANCHOR_NUMBER_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
_ANCHOR_WORD_RE = re.compile(r"\w+")

_NEAR_DUPLICATE_NUDGE = (
    "\n\n[Note: this result is nearly identical to one you "
    "already received. Searching again with similar terms "
    "is unlikely to surface new information. If you don't "
    "have enough evidence to answer well, it's better to "
    "finish now with an honest summary of what's actually "
    "available than to keep repeating this search.]")


def _anchor_numbers(text: str) -> set[float]:
    values: set[float] = set()
    text = _ANCHOR_HEADER_COUNT_RE.sub(" ", _ANCHOR_DATE_RE.sub(" ", text))
    for raw in _ANCHOR_NUMBER_RE.findall(text):
        cleaned = raw.replace(",", "").rstrip(".")
        try:
            value = float(cleaned)
        except ValueError:
            continue
        is_integer = "." not in cleaned
        if is_integer and (value < 10 or 1900 <= value <= 2100):
            continue
        values.add(value)
    return values


def _anchor_word_runs(text: str) -> set[tuple[str, ...]]:
    words = [w.lower() for w in _ANCHOR_WORD_RE.findall(text)]
    runs: set[tuple[str, ...]] = set()
    for i in range(len(words) - _ANCHOR_WORD_RUN + 1):
        window = words[i:i + _ANCHOR_WORD_RUN]
        if all(w.isalpha() and len(w) >= _ANCHOR_MIN_WORD_LETTERS
               for w in window):
            runs.add(tuple(window))
    return runs


def evidence_reference_text(scratchpad: Sequence[ScratchpadEntry]) -> str:
    return "\n".join((e.observation or "").replace(_NEAR_DUPLICATE_NUDGE, "")
                     for e in scratchpad if _is_reference_entry(e))


_FALLBACK_ANCHOR_MIN_PAIRS = 2
_FALLBACK_ANCHOR_PAIR_LETTERS = 4
_FALLBACK_ANCHOR_WORD_LETTERS = 5
_FALLBACK_ANCHOR_SUFFIXES = (
    "ings", "ing", "ions", "ion", "ed", "es", "s", "ly")


def _shares_a_number(answer: str, reference: str) -> bool:
    answer_numbers = _anchor_numbers(answer)
    if not answer_numbers:
        return False
    reference_numbers = _anchor_numbers(reference)
    for claimed in answer_numbers:
        for actual in reference_numbers:
            if claimed == actual or (actual != 0 and abs(claimed - actual)
                                     <= NUMBER_MATCH_REL_TOLERANCE *
                                     abs(actual)):
                return True
    return False


def _word_pairs(text: str) -> set[tuple[str, str]]:
    words = [w.lower() for w in _ANCHOR_WORD_RE.findall(text)]
    return {(a, b)
            for a, b in itertools.pairwise(words) if a.isalpha()
            and b.isalpha() and len(a) >= _FALLBACK_ANCHOR_PAIR_LETTERS
            and len(b) >= _FALLBACK_ANCHOR_PAIR_LETTERS}


def _stem(word: str) -> str:
    for suffix in _FALLBACK_ANCHOR_SUFFIXES:
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[:-len(suffix)]
    return word


def _content_words(text: str) -> set[str]:
    return {
        _stem(w.lower())
        for w in _ANCHOR_WORD_RE.findall(text)
        if w.isalpha() and len(w) >= _FALLBACK_ANCHOR_WORD_LETTERS
    }


def word_share(answer: str, reference: str) -> float:
    words = _content_words(answer)
    if not words:
        return 0.0
    return len(words & _content_words(reference)) / len(words)


def loosely_anchored(answer: str, reference: str) -> bool:
    return (
        _shares_a_number(answer, reference)
        or bool(_anchor_word_runs(answer) & _anchor_word_runs(reference))
        or len(_word_pairs(answer) & _word_pairs(reference))
        >= _FALLBACK_ANCHOR_MIN_PAIRS or word_share(
            answer, reference) >= settings.FALLBACK_ANCHOR_MIN_WORD_SHARE)


FALLBACK_ANCHOR_ISSUE = (
    "your finish_answer shares nothing with the other agent's summary you "
    "were given to build on (and you have no tool result of your own) -- it "
    "must be a reading of that summary for this ticker, not text from "
    "anywhere else.")


def evidence_anchor_issue(
        final_answer: str, scratchpad: Sequence[ScratchpadEntry],
        fallback_reference: str | None = None) -> str | None:
    reference = evidence_reference_text(scratchpad)
    if not reference:
        if fallback_reference and not loosely_anchored(final_answer,
                                                       fallback_reference):
            return FALLBACK_ANCHOR_ISSUE
        return None
    if _shares_a_number(final_answer, reference):
        return None
    if _anchor_word_runs(final_answer) & _anchor_word_runs(reference):
        return None
    return (
        "your finish_answer does not contain a single figure or phrase from "
        "this run's own tool results above -- it must be a summary of what "
        "your tools actually returned for this ticker, not text from "
        "anywhere else.")


@dataclass
class AgentLoopResult:
    final_answer: str
    scratchpad: list[ScratchpadEntry] = field(default_factory=list)
    iterations_used: int = 0
    hit_iteration_cap: bool = False
    unresolved_finish_validation_issue: str | None = None
    finish_meta: dict[str, Any] | None = None
    role: str | None = None


_SCORE_FIXED_ON_RETRY = 0.65
SCORE_UNRESOLVED = 0.35
_SCORE_THIN_EVIDENCE = 0.6
THIN_EVIDENCE_MAX_ITEMS = 2
_SCORE_ONE_EVIDENCE_TOOL = 0.84
_SCORE_NO_OWN_EVIDENCE = 0.76
_SCORE_PER_EXTRA_TOOL = 0.04
_SCORE_MAX_EXTRA_TOOLS = 3
_PENALTY_TOOL_ERROR = 0.05
_PENALTY_TOOL_ERROR_CAP = 0.15
_PENALTY_CORRECTION = 0.04
_PENALTY_CORRECTION_CAP = 0.12
_PENALTY_NO_ACCEPTED_TURN = 0.10
_PENALTY_TRUNCATED = 0.05
_PENALTY_FALLBACK_SERVED = 0.03
_PENALTY_WARNING = 0.08
_PENALTY_WARNING_CAP = 0.16
_PENALTY_RECYCLED = 0.10
_SCORE_WELL_SUPPORTED_FLOOR = 0.40
_TRUNCATED_FINISH_RE = re.compile(r"length|max_tokens", re.IGNORECASE)


def _evidence_quality_score(
        result: AgentLoopResult, *,
        verification_warnings: Sequence[str] | None,
        recycled: bool) -> tuple[float, dict[str, Any]]:
    evidence_tools = {
        entry.action
        for entry in result.scratchpad
        if _is_evidence_entry(entry) and entry.action
    }
    tool_errors = sum(
        1 for entry in result.scratchpad
        if entry.is_tool_result and entry.tool_error)
    corrections = sum(
        1 for entry in result.scratchpad if not entry.is_tool_result)
    warnings = len([w for w in (verification_warnings or []) if w])
    meta = result.finish_meta
    truncated = bool(
        meta
        and _TRUNCATED_FINISH_RE.search(str(meta.get("finish_reason") or "")))
    fallback_served = bool(meta and meta.get("rate_limited"))

    if evidence_tools:
        extra = min(len(evidence_tools) - 1, _SCORE_MAX_EXTRA_TOOLS)
        score = _SCORE_ONE_EVIDENCE_TOOL + extra * _SCORE_PER_EXTRA_TOOL
    else:
        score = _SCORE_NO_OWN_EVIDENCE
    score -= min(tool_errors * _PENALTY_TOOL_ERROR, _PENALTY_TOOL_ERROR_CAP)
    score -= min(corrections * _PENALTY_CORRECTION, _PENALTY_CORRECTION_CAP)
    score -= min(warnings * _PENALTY_WARNING, _PENALTY_WARNING_CAP)
    if meta is None:
        score -= _PENALTY_NO_ACCEPTED_TURN
    if truncated:
        score -= _PENALTY_TRUNCATED
    if fallback_served:
        score -= _PENALTY_FALLBACK_SERVED
    if recycled:
        score -= _PENALTY_RECYCLED
    signals = {
        "evidence_tools": len(evidence_tools),
        "tool_errors": tool_errors,
        "corrections": corrections,
        "warnings": warnings,
        "no_accepted_turn": meta is None,
        "truncated": truncated,
        "fallback_served": fallback_served,
        "recycled": recycled
    }
    return max(score, _SCORE_WELL_SUPPORTED_FLOOR), signals


def derive_agent_quality(
        result: AgentLoopResult, *, is_retry: bool, no_evidence: bool = False,
        thin_evidence: bool = False,
        verification_warnings: Sequence[str] | None = None,
        recycled: bool = False) -> tuple[float, bool]:
    has_unresolved_issue = (
        result.unresolved_finish_validation_issue is not None)
    if result.hit_iteration_cap or has_unresolved_issue or no_evidence:
        return SCORE_UNRESOLVED, False
    score, signals = _evidence_quality_score(
        result, verification_warnings=verification_warnings, recycled=recycled)
    if is_retry:
        score = min(score, _SCORE_FIXED_ON_RETRY)
    if thin_evidence:
        score = min(score, _SCORE_THIN_EVIDENCE)
    score = round(score, 2)
    logger.info(
        "agent_loop.quality_score_derived", role=result.role, score=score,
        is_retry=is_retry, thin_evidence=thin_evidence, **signals)
    return score, True


def is_thin_evidence(evidence_yield: int | None) -> bool:
    return (
        evidence_yield is not None
        and 0 < evidence_yield <= THIN_EVIDENCE_MAX_ITEMS)


def _finish_meta(response: LLMResponse, thought: str) -> dict[str, Any]:
    return {
        "thought": thought,
        "provider": response.provider,
        "model": response.model,
        "finish_reason": response.finish_reason,
        "prompt_tokens": response.usage.prompt_tokens,
        "completion_tokens": response.usage.completion_tokens,
        "thinking_tokens": response.usage.thinking_tokens,
        "rate_limited": response.rate_limited
    }


@dataclass
class _ParsedStep:
    thought: str
    action: str | None
    action_input: dict[str, Any] | None
    finish_answer: str | None
    reported_metrics: dict[str, Any] | None


class ReActAgentLoop:

    def __init__(
            self, *, role: AgentRole, tools: list[Tool], system_prompt: str,
            max_iterations: int, llm_router: LLMRouter | None = None,
            llm_tier: LLMTier = LLMTier.CHEAP, finish_answer_validator: (
                Callable[[str, dict[str, Any] | None, list[ScratchpadEntry]],
                         str | None]
                | None) = None,
            reported_metrics_schema: dict[str, Any] | None = None,
            require_tool_evidence: bool = True, evidence_yield: (
                Callable[[list[ScratchpadEntry]], int | None] | None) = None,
            anchor_fallback_reference: str | None = None,
            finish_validation_suffix: str | None = None,
            thinking_budget: int | None = None) -> None:
        self.role = role
        self.thinking_budget = thinking_budget
        self.finish_validation_suffix = (
            finish_validation_suffix if finish_validation_suffix is not None
            else _DEFAULT_FINISH_VALIDATION_SUFFIX)
        self.require_tool_evidence = require_tool_evidence
        self.evidence_yield = evidence_yield
        self.anchor_fallback_reference = anchor_fallback_reference
        self.tools: dict[str, Tool] = {tool.name: tool for tool in tools}
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations
        self.llm_router = llm_router or LLMRouter()
        self.llm_tier = llm_tier
        self.finish_answer_validator = finish_answer_validator
        self._response_schema = _build_react_step_schema(
            list(self.tools.values()), reported_metrics_schema)

    async def run(
        self, task: str, *, extra_context: str | None = None,
        prefetched_evidence: Sequence[ScratchpadEntry] | None = None
    ) -> AgentLoopResult:
        scratchpad: list[ScratchpadEntry] = [
            replace(
                entry, is_tool_result=True, tool_error=entry.tool_error
                or tool_reported_error(entry.observation or ""),
                prefetched=True) for entry in prefetched_evidence or ()
        ]
        productive_iterations = 0
        correction_retries = 0
        duplicate_block_retries = 0
        finish_validation_retries = 0
        zero_evidence_retries = 0
        anchor_retries = 0
        non_english_rejections = 0
        flagged_duplicate_signature: tuple[str | None, str] | None = None

        _LIVE_DATA_TOOLS = frozenset({"get_current_price"})
        executed_signatures: set[tuple[str, str]] = {
            (
                entry.action,
                json.dumps(entry.action_input or {}, sort_keys=True))
            for entry in scratchpad
            if entry.action is not None and not entry.tool_error
        }
        errored_signature_counts: dict[tuple[str, str], int] = {}
        for entry in scratchpad:
            if entry.tool_error and entry.action is not None:
                key = (
                    entry.action,
                    json.dumps(entry.action_input or {}, sort_keys=True))
                errored_signature_counts[key] = (
                    errored_signature_counts.get(key, 0) + 1)

        while (productive_iterations < self.max_iterations
               and correction_retries <= _MAX_CORRECTION_RETRIES
               and duplicate_block_retries <= _MAX_DUPLICATE_BLOCK_RETRIES):
            is_final_turn = productive_iterations == self.max_iterations - 1
            messages = self._build_messages(
                task, extra_context, scratchpad, is_final_turn=is_final_turn)
            try:
                response = await self.llm_router.complete(
                    self.llm_tier, messages, retries=AGENT_LLM_CALL_RETRIES,
                    max_tokens=_REACT_LOOP_MAX_TOKENS,
                    response_format="json_object",
                    response_schema=self._response_schema,
                    thinking_budget=self.thinking_budget)
            except LLMNonEnglishOutputError as exc:
                correction_retries += 1
                non_english_rejections += 1
                logger.warning(
                    "react_loop.non_english_response_rejected",
                    role=self.role.value, reason=exc.reason, model=exc.model,
                    attempt=correction_retries)
                scratchpad.append(
                    ScratchpadEntry(
                        thought="(non-English model response rejected)",
                        action=None, action_input=None, observation=(
                            "Your previous response was not in English and "
                            "was discarded. Respond again in English only, "
                            'with exactly one JSON object: {"thought": str, '
                            '"action": str|null, "action_input": '
                            'object|null, "finish_answer": str|null}.'),
                        is_tool_result=False))
                continue
            parsed = self._parse_response(response.content)

            if parsed is None:
                correction_retries += 1

                if (response.finish_reason
                        and "MAX_TOKENS" in response.finish_reason):
                    observation = (
                        "Your previous response was cut off before it "
                        "finished "
                        "(it ran out of its token budget partway through "
                        '"action_input"). Keep `thought` to a single short '
                        "sentence this time -- don't restate evidence or "
                        "reasoning at length -- and respond again with "
                        "exactly "
                        'one complete JSON object: {"thought": str, "action": '
                        'str|null, "action_input": object|null, '
                        '"finish_answer": str|null}.')
                else:
                    observation = (
                        "Your previous response was not valid JSON in the "
                        "required format. Respond with exactly one JSON "
                        "object: "
                        '{"thought": str, "action": str|null, "action_input": '
                        'object|null, "finish_answer": str|null}.')
                scratchpad.append(
                    ScratchpadEntry(
                        thought="(unparseable model response)", action=None,
                        action_input=None, observation=observation,
                        is_tool_result=False))
                continue

            if parsed.action is None and parsed.finish_answer is None:
                logger.warning(
                    "react_loop.neither_action_nor_finish_set",
                    raw=response.content[:500])
                correction_retries += 1
                scratchpad.append(
                    ScratchpadEntry(
                        thought=parsed.thought, action=None, action_input=None,
                        observation="Your response set neither `action` nor "
                        "`finish_answer` -- exactly one must be non-null. If "
                        "you "
                        "need more evidence, set `action` to a tool name; if "
                        "you're done, set `finish_answer` to your answer.",
                        is_tool_result=False))
                continue

            if parsed.finish_answer is not None:
                generic_issue: str | None = None
                zero_yield = (
                    self.evidence_yield is not None
                    and self.evidence_yield(scratchpad) == 0)
                if (self.require_tool_evidence
                        and not has_successful_tool_evidence(scratchpad)):
                    generic_issue = (
                        "you have no successful tool result yet this run "
                        "(every call so far either failed or none was made).")
                    if zero_evidence_retries < _MAX_ZERO_EVIDENCE_RETRIES:
                        zero_evidence_retries += 1
                        self._append_gate_rejection(
                            scratchpad, parsed.thought, "no_tool_evidence",
                            zero_evidence_retries,
                            f"[Your finish_answer was not accepted: "
                            f"{generic_issue} "
                            "Call one of your tools now to gather real "
                            "evidence "
                            "for this ticker, then finish from what it "
                            "returns.]")
                        continue
                elif not zero_yield:
                    generic_issue = evidence_anchor_issue(
                        parsed.finish_answer, scratchpad,
                        self.anchor_fallback_reference)
                    if generic_issue is not None and anchor_retries < 1:
                        anchor_retries += 1
                        rewrite_from = (
                            "the other agent's summary in the context above"
                            if generic_issue == FALLBACK_ANCHOR_ISSUE else
                            "the specific figures and facts your tools "
                            "returned above")
                        self._append_gate_rejection(
                            scratchpad, parsed.thought, "evidence_anchor",
                            anchor_retries,
                            f"[Your finish_answer was not accepted: "
                            f"{generic_issue} "
                            f"Rewrite it in English from {rewrite_from}.]")
                        continue
                validation_issue = (
                    self.finish_answer_validator(
                        parsed.finish_answer, parsed.reported_metrics,
                        scratchpad)
                    if self.finish_answer_validator is not None else None)
                if (validation_issue is not None and finish_validation_retries
                        < MAX_FINISH_VALIDATION_RETRIES):
                    finish_validation_retries += 1
                    logger.info(
                        "react_loop.finish_answer_validation_failed",
                        role=self.role.value, issue=validation_issue[:300],
                        attempt=finish_validation_retries)
                    scratchpad.append(
                        ScratchpadEntry(
                            thought=parsed.thought, action=None,
                            action_input=None, observation=(
                                "[Your finish_answer was not accepted: "
                                f"{validation_issue} "
                                f"{self.finish_validation_suffix}]"),
                            is_tool_result=False))
                    continue
                if validation_issue is not None:
                    logger.warning(
                        "react_loop.finish_answer_validation_unresolved",
                        role=self.role.value, issue=validation_issue[:300])
                if generic_issue is not None:
                    logger.warning(
                        "react_loop.finish_gate_unresolved",
                        role=self.role.value, issue=generic_issue[:300])
                unresolved = (
                    "; ".join(
                        issue for issue in (generic_issue, validation_issue)
                        if issue) or None)
                return AgentLoopResult(
                    final_answer=parsed.finish_answer, scratchpad=scratchpad,
                    iterations_used=productive_iterations,
                    hit_iteration_cap=False,
                    unresolved_finish_validation_issue=unresolved,
                    finish_meta=_finish_meta(response, parsed.thought),
                    role=self.role.value)

            if is_final_turn:
                logger.info(
                    "react_loop.final_turn_action_overridden",
                    role=self.role.value, attempted_action=parsed.action)
                productive_iterations += 1
                fallback_answer = (
                    parsed.thought.strip()
                    if parsed.thought and parsed.thought.strip() else (
                        "Iteration cap reached; the agent's final-turn "
                        "response set no usable reasoning to fall back on."))
                return AgentLoopResult(
                    final_answer=fallback_answer, scratchpad=scratchpad,
                    iterations_used=productive_iterations,
                    hit_iteration_cap=True,
                    finish_meta=_finish_meta(response, parsed.thought),
                    role=self.role.value)

            current_signature = (
                parsed.action,
                json.dumps(parsed.action_input or {}, sort_keys=True))
            errored_key = (parsed.action or "", current_signature[1])
            is_repeated_call = parsed.action not in _LIVE_DATA_TOOLS and (
                current_signature in executed_signatures
                or errored_signature_counts.get(errored_key, 0) >= 2)

            if is_repeated_call or _same_action_signature(
                    parsed.action, parsed.action_input,
                    flagged_duplicate_signature):
                duplicate_block_retries += 1
                if not is_repeated_call:
                    block_reason = "flagged_near_duplicate_signature"
                    why = (
                        "this exact tool call just returned a result "
                        "nearly identical to one you already had")
                elif current_signature in executed_signatures:
                    block_reason = "identical_signature_recalled"
                    why = (
                        "this exact tool call already succeeded earlier this "
                        "run; its result is in the scratchpad above")
                else:
                    block_reason = "repeated_failure"
                    why = (
                        "this exact tool call has already failed twice this "
                        "run")
                logger.info(
                    "react_loop.duplicate_action_blocked",
                    role=self.role.value, action=parsed.action,
                    reason=block_reason)
                observation = (
                    f"[Blocked before running: {why}, and repeating it is "
                    "not allowed. Try a genuinely different query/tool, or "
                    "finish now with an honest summary of what's actually "
                    "available.]")
                scratchpad.append(
                    ScratchpadEntry(
                        thought=parsed.thought, action=parsed.action,
                        action_input=parsed.action_input,
                        observation=observation, is_tool_result=False))
                continue

            productive_iterations += 1
            observation = await self._execute_action(
                parsed.action, parsed.action_input)
            peer_ticker = self._peer_ticker(parsed.action, parsed.action_input)
            tool_error = tool_reported_error(observation)
            if tool_error:
                errored_signature_counts[errored_key] = (
                    errored_signature_counts.get(errored_key, 0) + 1)
                logger.info(
                    "react_loop.tool_reported_error", role=self.role.value,
                    action=parsed.action,
                    attempt=errored_signature_counts[errored_key])
            elif parsed.action is not None:
                executed_signatures.add((parsed.action, current_signature[1]))
            if not tool_error and _is_near_duplicate_observation(observation,
                                                                 scratchpad):
                flagged_duplicate_signature = (
                    parsed.action,
                    json.dumps(parsed.action_input or {}, sort_keys=True))
                observation += _NEAR_DUPLICATE_NUDGE
            else:
                flagged_duplicate_signature = None
            scratchpad.append(
                ScratchpadEntry(
                    thought=parsed.thought, action=parsed.action,
                    action_input=parsed.action_input, observation=observation,
                    tool_error=tool_error, peer_ticker=peer_ticker))

        return self._best_effort_result(
            scratchpad, productive_iterations, duplicate_block_retries,
            non_english=non_english_rejections > 0)

    def _append_gate_rejection(
            self, scratchpad: list[ScratchpadEntry], thought: str, gate: str,
            attempt: int, observation: str) -> None:
        logger.info(
            "react_loop.finish_gate_rejected", role=self.role.value, gate=gate,
            attempt=attempt)
        scratchpad.append(
            ScratchpadEntry(
                thought=thought, action=None, action_input=None,
                observation=observation, is_tool_result=False))

    def _peer_ticker(
            self, action: str | None,
            action_input: dict[str, Any] | None) -> str | None:
        tool = self.tools.get(action or "")
        return None if tool is None else tool.peer_ticker(action_input)

    async def _execute_action(
            self, action: str | None,
            action_input: dict[str, Any] | None) -> str:
        tool = self.tools.get(action or "")
        if tool is None:
            return (
                f"Unknown tool {action!r}. Available tools: "
                f"{', '.join(sorted(self.tools)) or '(none)'}.")
        try:
            return await tool.run(**(action_input or {}))
        except TypeError as exc:
            return f"Tool {action!r} was called with invalid arguments: {exc}"
        except Exception as exc:
            logger.warning(
                "react_loop.tool_raised", tool=action,
                error_type=type(exc).__name__, error=str(exc)[:500])
            return (
                f"Tool {action!r} raised an error: {type(exc).__name__}: {exc}"
            )

    def _best_effort_result(
            self, scratchpad: list[ScratchpadEntry],
            productive_iterations: int, duplicate_block_retries: int = 0, *,
            non_english: bool = False) -> AgentLoopResult:
        if productive_iterations >= self.max_iterations:
            cap_description = f"Iteration cap ({self.max_iterations})"
        elif duplicate_block_retries > _MAX_DUPLICATE_BLOCK_RETRIES:
            cap_description = (
                f"Repeated duplicate/blocked tool calls (after "
                f"{productive_iterations} genuine research step(s))")
        else:
            kind = "invalid or non-English" if non_english else "invalid"
            cap_description = (
                f"Repeated {kind} responses (after {productive_iterations} "
                "genuine research step(s))")
        last_real_observation = next((
            entry.observation for entry in reversed(scratchpad)
            if is_successful_tool_entry(entry)), None)
        if last_real_observation is not None:
            summary = (
                f"{cap_description} reached before the agent volunteered a "
                f"final answer. Best-effort summary from the last observation "
                f"gathered: {last_real_observation}")
        else:
            summary = (
                f"{cap_description} reached with no usable evidence gathered.")
        return AgentLoopResult(
            final_answer=summary, scratchpad=scratchpad,
            iterations_used=productive_iterations, hit_iteration_cap=True,
            role=self.role.value)

    def _build_messages(
            self, task: str, extra_context: str | None,
            scratchpad: list[ScratchpadEntry], *,
            is_final_turn: bool = False) -> list[LLMMessage]:
        tool_list = "\n".join(
            f"- {t.name}: {t.description}" for t in self.tools.values())
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        system = (
            f"Today's real date is {today}. This is ground truth -- it "
            "overrides any date-related assumption you would otherwise make "
            "from your own training. A web_search query that names a past "
            'year ("... recent September 2025") returns results from that '
            "year, which are stale and contradict other agents' real-time "
            "price data. When you search or read a result, check its actual "
            "date before treating it as current; don't assume a source is "
            "recent just because it discusses the ticker.\n\n"
            f"{self.system_prompt}\n\n"
            "You operate a Thought/Action/Observation loop. On every turn, "
            "respond "
            "with exactly one JSON object and nothing else (no markdown, no "
            "prose "
            "outside the JSON):\n"
            '{"thought": "your reasoning", "action": "tool_name or null", '
            '"action_input": {"arg": "value"} or null, '
            '"finish_answer": "your final answer or null", '
            '"reported_metrics": {"metric_name": number, ...} or null}\n\n'
            "`thought` is a brief internal note to yourself, not a report -- "
            "one "
            "or two sentences on what you know so far and what you're about "
            "to do "
            "next. Do not use it to write out search results, quote sources, "
            "or "
            "restate evidence at length; that belongs in `finish_answer` once "
            "you're done, not in every turn's `thought`. A long `thought` "
            "risks "
            "getting cut off before you reach `action`/`finish_answer`, which "
            "wastes the whole turn -- keeping it short is not just a style "
            "preference, it protects the turn from failing.\n\n"
            "`finish_answer`, when non-null, must be a plain prose string (a "
            "few sentences) -- never a nested JSON object or structured data, "
            "even if the task is naturally about structured metrics; describe "
            "them in a sentence instead. It needs to read naturally wherever "
            "it ends up (a debate argument, a report), not look like an API "
            "response.\n\n"
            "`reported_metrics`: leave this null unless your own instructions "
            "above explicitly define a fixed set of metric names for you to "
            "report this way -- most agents never need to set it. If your "
            "instructions do define such a set, and you are setting "
            "finish_answer, restate each of those metrics' numeric values "
            "here too, using exactly the metric names given to you (e.g. "
            '{"net_margin_pct": 12.5}) and the exact number your own tool '
            "reported for it this run -- this lets your numeric claims be "
            "checked as real numbers against their real source, instead of "
            "guessed at from your prose. Never invent a metric name you "
            "weren't given, and never state a number here that isn't also "
            "reflected in your finish_answer prose.\n\n"
            "Exactly one of `action` or `finish_answer` must be non-null. Use "
            "`action` to gather more evidence via a tool; use `finish_answer` "
            "only "
            "once you judge the evidence gathered so far is sufficient to "
            "answer "
            "the task.\n\n"
            "Re-searching with reworded queries after two searches that both "
            "returned only generic, non-specific results (not literal "
            "duplicates "
            "-- different articles, equally unhelpful) runs out of budget "
            "before "
            "finishing. A genuine "
            "absence of specific findings IS itself a complete, valid answer "
            "-- "
            '"I found no [ticker]-specific developments on X" is a real, '
            "honest "
            "conclusion, not a failure to report. If a second search on the "
            "same "
            "underlying question comes back just as generic as the first, "
            "that's "
            "your answer -- finish with it rather than trying a third "
            "rewording.\n\n"
            f"Available tools:\n{tool_list}")
        if is_final_turn:
            system += (
                "\n\nThis is your LAST turn -- no further tool calls are "
                "possible after this one regardless of what you set `action` "
                "to. You MUST set `finish_answer` now, using whatever "
                "evidence you've already gathered above. Setting `action` "
                "instead would waste this turn entirely.")

        parts = [f"Task: {task}"]
        if extra_context:
            parts.append(f"Additional context: {extra_context}")
        prefetched = [e for e in scratchpad if e.prefetched]
        if prefetched:
            calls = "; ".join(
                f"{e.action}("
                f"{json.dumps(e.action_input or {}, sort_keys=True)})"
                for e in prefetched)
            parts.append(
                "Already done for you in code before your first turn: "
                f"{calls}. Their results are the first entries of your "
                "scratchpad below and count as your own tool calls. Any "
                "instruction to make one of these calls first is already "
                "satisfied -- do not repeat them with the same input; go on "
                "with the next step your instructions describe, or finish "
                "if the evidence is already sufficient.")
        if scratchpad:
            history = "\n".join(
                f"Thought: {e.thought}\nAction: {e.action}\n"
                f"Action Input: {e.action_input}\nObservation: {e.observation}"
                for e in scratchpad)
            parts.append(f"Scratchpad so far:\n{history}")

        return [
            LLMMessage(role="system", content=system),
            LLMMessage(role="user", content="\n\n".join(parts))
        ]

    @staticmethod
    def _parse_response(raw: str) -> _ParsedStep | None:
        data = parse_json_dict(
            raw, log_event="react_loop.unparseable_response")
        if data is None:
            return None

        raw_reported_metrics = data.get("reported_metrics")
        raw_thought = data.get("thought")
        return _ParsedStep(
            thought="" if raw_thought is None else str(raw_thought),
            action=data.get("action") or None,
            action_input=data.get("action_input") or None,
            finish_answer=_coerce_finish_answer(data.get("finish_answer")),
            reported_metrics=(
                raw_reported_metrics if isinstance(raw_reported_metrics, dict)
                and raw_reported_metrics else None))


class BaseAgent(ABC):
    role: AgentRole

    def __init__(self, role: AgentRole) -> None:
        self.role = role

    @abstractmethod
    async def run(self, state: AnalysisState) -> AgentOutput:
        raise NotImplementedError
