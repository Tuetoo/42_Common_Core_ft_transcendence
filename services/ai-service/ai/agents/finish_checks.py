import math
import re
from collections.abc import Callable, Sequence
from typing import Any
import structlog
from ai.agents.debate_common import find_ungrounded_figures
from ai.agents.loop import (MAX_FINISH_VALIDATION_RETRIES, ScratchpadEntry,
                            evidence_reference_text)
from ai.tools.news_tool import tool_labels_in_text

logger = structlog.get_logger(__name__)

FinishCheck = Callable[[str, dict[str, Any] | None, list[ScratchpadEntry]],
                       str | None]

RECORD_ID_IN_TEXT_RE = re.compile(r"\bF\d+\.[A-Z]\d+\b")


def record_ids_in_text(text: str) -> list[str]:
    return sorted(set(RECORD_ID_IN_TEXT_RE.findall(text or "")))


def internal_tokens_in_text(text: str) -> list[str]:
    return record_ids_in_text(text) + tool_labels_in_text(text or "")


INTERNAL_TOKEN_MESSAGE = (
    "Internal text check: your answer contains {tokens}, which are internal "
    "filing-record ids or search-tool labels for you, not words for the "
    "reader. Rewrite those sentences without them: name a filing by its "
    'form and filing date (e.g. "the 10-Q filed 2026-07-23") and a source '
    "by its own name, and leave out bracketed markers, catalog= fields and "
    '"link unavailable". Ids in reported_metrics fields stay as they are.')


def internal_token_message(tokens: Sequence[str]) -> str:
    return INTERNAL_TOKEN_MESSAGE.format(
        tokens=", ".join(f'"{token}"' for token in tokens))


class RejectOnceBudget:

    def __init__(self, role: str) -> None:
        self.role = role
        self.rejections = 0

    def reject(self, issue: str) -> str:
        self.rejections += 1
        return issue

    @property
    def budget_left(self) -> bool:
        return self.rejections < MAX_FINISH_VALIDATION_RETRIES

    def once_allowed(self, check: str, **fields: Any) -> bool:
        if self.budget_left:
            return True
        logger.info(
            "finish_checks.once_check_logged_only", role=self.role,
            check=check, **fields)
        return False


class InternalTokenValidator(RejectOnceBudget):

    def __init__(
        self, role: str, base: FinishCheck | None = None,
        once_checks: Sequence[tuple[str, FinishCheck]] = ()
    ) -> None:
        super().__init__(role)
        self.base = base
        self.once_checks = list(once_checks)
        self.fired: set[str] = set()

    def _token_issue(
            self, final_answer: str, reported_metrics: dict[str, Any] | None,
            scratchpad: list[ScratchpadEntry]) -> str | None:
        del reported_metrics, scratchpad
        tokens = internal_tokens_in_text(final_answer)
        if not tokens:
            return None
        logger.info(
            "finish_checks.internal_tokens_in_answer", role=self.role,
            tokens=tokens, rejected="internal_tokens" not in self.fired
            and self.budget_left)
        return internal_token_message(tokens)

    def __call__(
            self, final_answer: str, reported_metrics: dict[str, Any] | None,
            scratchpad: list[ScratchpadEntry]) -> str | None:
        if self.base is not None:
            issue = self.base(final_answer, reported_metrics, scratchpad)
            if issue is not None:
                return self.reject(issue)
        issues: list[str] = []
        checks = [("internal_tokens", self._token_issue), *self.once_checks]
        for name, check in checks:
            if name in self.fired:
                continue
            issue = check(final_answer, reported_metrics, scratchpad)
            if issue is None or not self.once_allowed(name, issue=issue[:200]):
                continue
            logger.info(
                "finish_checks.once_check_rejected", role=self.role,
                check=name, issue=issue[:200])
            self.fired.add(name)
            issues.append(issue)
        return self.reject(" ".join(issues)) if issues else None


OWN_EVIDENCE_CHECK = "own_evidence_figures"
OWN_EVIDENCE_FIGURE_MESSAGE = (
    "Figure check: your answer states {figures}, which match no number in "
    "this run's own tool results. State each figure as a tool result of "
    "this run gives it (rounding is fine), or leave it out; do not bring in "
    "figures from other agents, earlier runs or memory.")


def own_evidence_ungrounded_figures(
        final_answer: str, scratchpad: Sequence[ScratchpadEntry],
        extra_reference: str = "") -> list[str]:
    evidence = evidence_reference_text(scratchpad)
    if not evidence:
        return []
    if extra_reference:
        evidence += "\n" + extra_reference
    return sorted(find_ungrounded_figures(final_answer, evidence))


def own_evidence_figure_check(extra_reference: str = "") -> FinishCheck:

    def check(
            final_answer: str, reported_metrics: dict[str, Any] | None,
            scratchpad: list[ScratchpadEntry]) -> str | None:
        del reported_metrics
        try:
            figures = own_evidence_ungrounded_figures(
                final_answer, scratchpad, extra_reference)
        except Exception as exc:
            logger.warning(
                "finish_checks.own_evidence_check_failed", error=str(exc))
            return None
        if not figures:
            return None
        return OWN_EVIDENCE_FIGURE_MESSAGE.format(
            figures=", ".join(f'"{f}"' for f in figures))

    return check


def reported_number_issue(
        name: str, claimed: Any, allowed: Sequence[float], tolerance: float,
        mismatch: str) -> str | None:
    try:
        value = float(claimed)
    except (TypeError, ValueError):
        value = math.nan
    if not math.isfinite(value):
        return (
            f"reported_metrics.{name} must be a plain number (got "
            f"{claimed!r}).")
    if allowed and not any(abs(value - actual) <= tolerance
                           for actual in allowed):
        return mismatch.format(value=value)
    return None
