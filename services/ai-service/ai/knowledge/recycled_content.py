import dataclasses
import difflib
import re
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any
from ai.knowledge.filing_records import (RawNumbers, StatedAmount,
                                         stated_amounts)

if TYPE_CHECKING:
    from ai.agents.loop import ScratchpadEntry

AGENT_NOTE_TAG = "[agent:"
_AGENT_NOTE_LINE_RE = re.compile(r"\([^)\n]*\) " + re.escape(AGENT_NOTE_TAG))

_MIN_PRIOR_OVERLAP = 0.5
_MIN_MARGIN_OVER_SOURCES = 0.2
_MIN_SENTENCE_WORDS = 4

_STOP_WORDS = frozenset({
    "this", "that", "with", "from", "have", "been", "were", "their", "there",
    "which", "while", "about", "into", "over", "also", "than", "them", "they",
    "will", "would", "could", "should", "these", "those", "such", "more",
    "most", "other", "some", "only", "very", "just", "what", "when", "where",
    "being", "does", "done", "after", "before", "under", "within", "across",
    "among", "amid", "remains", "remain", "ongoing", "recent", "recently",
    "current", "currently", "continue", "continues", "overall", "broader",
    "significant"
})
_LEADING_BANNER_RE = re.compile(r"^(?:\s*\[[^\]]*\]\s*)+")
_APPENDIX_RE = re.compile(
    r"\n\s*(?:Live FRED this run|Data caveat|Named risk events|\[)")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"[a-z][a-z\-]{3,}")
_NUMBER_RE = re.compile(r"\d[\d,]*\.?\d*")
_SUFFIXES = ("ings", "ing", "ions", "ion", "ed", "es", "s", "ly")


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if len(word) > 5 and word.endswith(suffix):
            return word[:-len(suffix)]
    return word


def _content_words(text: str) -> set[str]:
    return {
        _stem(w)
        for w in _WORD_RE.findall(text.lower()) if w not in _STOP_WORDS
    }


def _numbers(text: str) -> set[str]:
    found: set[str] = set()
    for match in _NUMBER_RE.findall(text):
        value = match.replace(",", "").rstrip(".")
        if len(value.replace(".", "")) >= 2:
            found.add(value)
    return found


def _sentences(answer: str) -> list[str]:
    body = _LEADING_BANNER_RE.sub("", answer)
    body = _APPENDIX_RE.split("\n" + body, maxsplit=1)[0]
    body = body.replace("U.S.", "US")
    return [
        s for s in _SENTENCE_SPLIT_RE.split(body.strip())
        if len(_content_words(s)) >= _MIN_SENTENCE_WORDS
    ]


def scratchpad_field(entry: Any, name: str, default: Any = None) -> Any:
    if isinstance(entry, dict):
        return entry.get(name, default)
    return getattr(entry, name, default)


def entry_observation(entry: Any) -> str:
    return str(scratchpad_field(entry, "observation", "") or "")


def _is_agent_note_line(passage: str) -> bool:
    return _AGENT_NOTE_LINE_RE.match(passage) is not None


def note_and_source_passages(observation: str) -> tuple[list[str], list[str]]:
    header, *items = ("\n" + observation).split("\n- ")
    notes = [p for p in items if p.strip() and _is_agent_note_line(p)]
    sources = [p for p in items if p.strip() and not _is_agent_note_line(p)]
    header = header.removeprefix("\n")
    if header.strip() and not notes:
        sources.insert(0, header)
    return notes, sources


def is_evidence_entry(entry: Any) -> bool:
    action = scratchpad_field(entry, "action")
    return (
        bool(action)
        and isinstance(scratchpad_field(entry, "observation"), str)
        and scratchpad_field(entry, "is_tool_result", True) is not False
        and not scratchpad_field(entry, "tool_error", False))


def recycled_share(answer: str, scratchpad: Iterable[Any] | None) -> float:
    agent_notes: list[set[str]] = []
    sources: list[set[str]] = []
    source_numbers: set[str] = set()
    for entry in scratchpad or ():
        if not is_evidence_entry(entry):
            continue
        note_passages, source_passages = note_and_source_passages(
            entry_observation(entry))
        agent_notes.extend(_content_words(p) for p in note_passages)
        for passage in source_passages:
            sources.append(_content_words(passage))
            source_numbers |= _numbers(passage)
    if not agent_notes:
        return 0.0

    total = 0
    recycled = 0
    for sentence in _sentences(answer):
        words = _content_words(sentence)
        prior = max(len(words & note) / len(words) for note in agent_notes)
        fresh = max((len(words & src) / len(words) for src in sources),
                    default=0.0)
        anchored = bool(_numbers(sentence) & source_numbers)
        total += len(words)
        if (prior >= _MIN_PRIOR_OVERLAP
                and prior - fresh >= _MIN_MARGIN_OVER_SOURCES
                and not anchored):
            recycled += len(words)
    return recycled / total if total else 0.0


_CODE_LINE_RE = re.compile(r"\s*Named risk events \(sourced\):.*$", re.DOTALL)
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9.%$'-]*")


def _note_body(passage: str) -> str:
    header_end = passage.find("]: ")
    body = passage[header_end + 3:] if header_end >= 0 else passage
    return _CODE_LINE_RE.sub("", body)


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def prior_note_similarity(
        answer: str, scratchpad: Iterable[Any] | None) -> float | None:
    answer_tokens = _tokens(_CODE_LINE_RE.sub("", answer))
    if not answer_tokens:
        return None
    best: float | None = None
    for entry in scratchpad or ():
        if not is_evidence_entry(entry):
            continue
        for passage in note_and_source_passages(entry_observation(entry))[0]:
            ratio = difflib.SequenceMatcher(
                None, answer_tokens, _tokens(_note_body(passage)),
                autojunk=False).ratio()
            best = ratio if best is None else max(best, ratio)
    return best


def split_agent_note_passages(observations: Iterable[str]) -> tuple[str, str]:
    notes: list[str] = []
    fresh: list[str] = []
    for observation in observations:
        observation_notes, observation_fresh = note_and_source_passages(
            observation)
        notes.extend(observation_notes)
        fresh.extend(observation_fresh)
    return "\n".join(notes), "\n".join(fresh)


_AMOUNT_WORDING_RE = re.compile(
    r"(?:\$|US\$|USD\s?)?\s?\d[\d,]*(?:\.\d+)?\s?"
    r"(?:%|percent\b|trillion\b|billion\b|million\b|thousand\b"
    r"|tn\b|bn\b|mn\b|mm\b|[TBMK]\b)?", re.IGNORECASE)


def prior_note_only_claims(final_answer: str,
                           observations: Iterable[str]) -> list[str]:
    notes, fresh = split_agent_note_passages(observations)
    if not notes:
        return []
    claims: list[str] = []
    note_numbers, fresh_numbers = RawNumbers(notes), RawNumbers(fresh)
    for match in _AMOUNT_WORDING_RE.finditer(final_answer):
        wording = match.group(0).strip()
        amounts = stated_amounts(wording)
        if len(amounts) != 1 or wording in claims:
            continue
        if note_numbers.contains(amounts[0]) and not fresh_numbers.contains(
                _within_rounding(amounts[0])):
            claims.append(wording)
    return claims


_ROUNDING_REL_TOLERANCE = 0.005


def _within_rounding(amount: StatedAmount) -> StatedAmount:
    middle = (amount.low + amount.high) / 2
    slack = abs(middle) * _ROUNDING_REL_TOLERANCE
    return dataclasses.replace(
        amount, low=min(amount.low, middle - slack),
        high=max(amount.high, middle + slack))


def prior_note_only_claims_in_run(
        final_answer: str,
        scratchpad: "Sequence[ScratchpadEntry]") -> list[str]:
    return prior_note_only_claims(
        final_answer, (
            entry_observation(entry)
            for entry in scratchpad if is_evidence_entry(entry)))
