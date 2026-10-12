import contextlib
from typing import Any, Protocol
from prometheus_client import Counter, Gauge
from shared.enums import AnalysisFailureReason
from shared.logging import get_or_create

KNOWN_FAILURE_REASONS = frozenset(
    reason.value for reason in AnalysisFailureReason)

ANALYSES_TOTAL: Counter = get_or_create(
    Counter, "alphamind_analyses",
    "Analyses that reached a terminal state, by status and failure reason.",
    ("status", "failure_reason"))
ANALYSIS_QUEUE_PENDING: Gauge = get_or_create(
    Gauge, "alphamind_analysis_queue_pending",
    "Analyses waiting in the in-process serial queue.")
ANALYSIS_QUEUE_RUNNING: Gauge = get_or_create(
    Gauge, "alphamind_analysis_queue_running",
    "Analyses currently being run by the serial queue worker (0 or 1).")
LLM_ERRORS_TOTAL: Counter = get_or_create(
    Counter, "alphamind_llm_errors",
    "Failed LLM calls seen by the router, by provider and error kind.",
    ("provider", "kind"))


def record_analysis_completed() -> None:
    ANALYSES_TOTAL.labels(status="completed", failure_reason="none").inc()


def record_analysis_failed(reason: str | None) -> None:
    label = reason if reason in KNOWN_FAILURE_REASONS else "other"
    ANALYSES_TOTAL.labels(status="failed", failure_reason=label).inc()


def llm_error_kind(exc: BaseException) -> str:
    from ai.llm.base import (LLMProviderTimeoutError, LLMRateLimitError,
                             LLMRequestTimeoutError, LLMServerOverloadedError)

    if isinstance(exc, (LLMRequestTimeoutError, LLMProviderTimeoutError)):
        return "timeout"
    if isinstance(exc, LLMServerOverloadedError):
        return "server_overload"
    if isinstance(exc, LLMRateLimitError):
        return "rate_limit"
    return "other"


def record_llm_error(provider: str, exc: BaseException) -> None:
    with contextlib.suppress(Exception):
        LLM_ERRORS_TOTAL.labels(provider=provider,
                                kind=llm_error_kind(exc)).inc()


class _QueueLike(Protocol):

    @property
    def current(self) -> Any:
        ...

    def pending_count(self) -> int:
        ...


def bind_analysis_queue(queue: _QueueLike) -> None:
    ANALYSIS_QUEUE_PENDING.set_function(lambda: float(queue.pending_count()))
    ANALYSIS_QUEUE_RUNNING.set_function(
        lambda: 1.0 if queue.current is not None else 0.0)
