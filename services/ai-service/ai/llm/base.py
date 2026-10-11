import asyncio
import contextlib
import random
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from contextvars import ContextVar, Token
from enum import StrEnum
from typing import Any, Literal, NamedTuple
from pydantic import BaseModel


class LLMTier(StrEnum):
    CHEAP = "cheap"
    HIGHEST_ACCURACY = "highest_accuracy"


class LLMMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class LLMUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    thinking_tokens: int | None = None


class LLMResponse(BaseModel):
    content: str
    provider: str
    model: str
    usage: LLMUsage
    finish_reason: str | None = None
    rate_limited: bool = False


class LLMError(Exception):
    pass


class LLMConfigError(LLMError):
    pass


class LLMRequestError(LLMError):
    pass


class LLMInvalidRequestError(LLMRequestError):
    pass


class LLMRateLimitError(LLMRequestError):

    def __init__(
            self, message: str = "", *,
            requests_sent: int | None = None) -> None:
        super().__init__(message)
        self.requests_sent = requests_sent


class LLMServerOverloadedError(LLMRateLimitError):
    pass


class LLMProviderTimeoutError(LLMRequestError):
    pass


class LLMRequestTimeoutError(LLMServerOverloadedError):
    pass


class LLMNonEnglishOutputError(LLMError):

    def __init__(
            self, message: str, *, reason: str | None = None,
            model: str | None = None,
            response: LLMResponse | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.model = model
        self.response = response


RATE_LIMIT_BACKOFF_SCHEDULE: list[float] = [1.0, 2.0, 4.0, 8.0, 60.0]

_RATE_LIMIT_BACKOFF_JITTER_FRACTION = 0.20


def rate_limit_backoff_seconds(attempt: int) -> float:
    index = min(attempt, len(RATE_LIMIT_BACKOFF_SCHEDULE) - 1)
    base = RATE_LIMIT_BACKOFF_SCHEDULE[index]
    jitter_factor = random.uniform(
        1.0 - _RATE_LIMIT_BACKOFF_JITTER_FRACTION,
        1.0 + _RATE_LIMIT_BACKOFF_JITTER_FRACTION)
    return base * jitter_factor


AGENT_LLM_CALL_RETRIES = 4


class GatedReservation(NamedTuple):
    key: tuple[str, ...]
    wait: float
    gate_wait: float
    reserved: bool


class RequestPacer:

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._next_allowed_at: dict[tuple[str, ...], float] = {}
        self._counters: dict[tuple[str, ...], int] = {}
        self._marks: dict[tuple[str, ...], int] = {}

    async def reserve_slot(
            self, key: tuple[str, ...], min_interval_seconds: float) -> float:
        if min_interval_seconds <= 0:
            return 0.0
        async with self._lock:
            now = time.monotonic()
            next_allowed = self._next_allowed_at.get(key, 0.0)
            wait = max(0.0, next_allowed - now)
            self._next_allowed_at[key] = (
                max(now, next_allowed) + min_interval_seconds)
        return wait

    async def reserve_best_slot_gated(
            self, candidates: Sequence[tuple[str, ...]],
            min_interval_seconds: float, *,
            max_wait_seconds: float | None = None,
            gate_key: tuple[str, ...] | None = None,
            gate_interval_seconds: float = 0.0,
            gate_max_wait_seconds: float | None = None) -> GatedReservation:
        if not candidates:
            raise ValueError(
                "reserve_best_slot_gated requires at least one candidate")
        async with self._lock:
            now = time.monotonic()
            if min_interval_seconds > 0:
                chosen = min(
                    candidates,
                    key=lambda k: self._next_allowed_at.get(k, 0.0))
                next_allowed = self._next_allowed_at.get(chosen, 0.0)
                wait = max(0.0, next_allowed - now)
            else:
                chosen, next_allowed, wait = candidates[0], 0.0, 0.0
            use_gate = gate_key is not None and gate_interval_seconds > 0
            gate_next = 0.0
            gate_wait = 0.0
            if use_gate:
                assert gate_key is not None
                gate_next = self._next_allowed_at.get(gate_key, 0.0)
                gate_wait = max(0.0, gate_next - now)
            if (max_wait_seconds is not None and wait > max_wait_seconds) or (
                    gate_max_wait_seconds is not None
                    and gate_wait > gate_max_wait_seconds):
                return GatedReservation(chosen, wait, gate_wait, False)
            if min_interval_seconds > 0:
                self._next_allowed_at[chosen] = (
                    max(now, next_allowed) + min_interval_seconds)
            if use_gate:
                assert gate_key is not None
                self._next_allowed_at[gate_key] = (
                    max(now, gate_next) + gate_interval_seconds)
        return GatedReservation(chosen, wait, gate_wait, True)

    async def peek_earliest_next_allowed_at(
            self, candidates: Sequence[tuple[str, ...]]) -> float:
        if not candidates:
            return 0.0
        return min(self._next_allowed_at.get(c, 0.0) for c in candidates)

    async def peek_exhausted_fraction(
            self, candidates: Sequence[tuple[str, ...]],
            exhausted_wait_seconds: float) -> float:
        if not candidates:
            return 0.0
        now = time.monotonic()
        exhausted = sum(
            1 for c in candidates if self._next_allowed_at.get(c, 0.0) -
            now > exhausted_wait_seconds)
        return exhausted / len(candidates)

    async def mark_unavailable_until(
            self, key: tuple[str, ...], until_monotonic: float) -> None:
        async with self._lock:
            current = self._next_allowed_at.get(key, 0.0)
            self._next_allowed_at[key] = max(current, until_monotonic)
            self._marks[key] = self._marks.get(key, 0) + 1

    def mark_count(self, key: tuple[str, ...]) -> int:
        return self._marks.get(key, 0)

    async def clear(self, key: tuple[str, ...]) -> bool:
        async with self._lock:
            return self._next_allowed_at.pop(key, None) is not None

    def reset(self) -> None:
        self._next_allowed_at.clear()
        self._counters.clear()
        self._marks.clear()

    def increment_counter(self, key: tuple[str, ...]) -> int:
        value = self._counters.get(key, 0) + 1
        self._counters[key] = value
        return value

    def reset_counter(self, key: tuple[str, ...]) -> int:
        return self._counters.pop(key, 0)


request_pacer = RequestPacer()


class LLMProviderAdapter(ABC):
    name: str

    @abstractmethod
    async def complete(
            self, messages: list[LLMMessage], *, model: str,
            temperature: float = 0.2, max_tokens: int = 1024,
            response_format: str | None = None,
            timeout_seconds: float | None = None,
            response_schema: dict[str, Any] | None = None) -> LLMResponse:
        ...

    @abstractmethod
    async def stream(
            self, messages: list[LLMMessage], *, model: str,
            temperature: float = 0.2,
            max_tokens: int = 1024) -> AsyncIterator[str]:
        raise NotImplementedError
        yield ""  # pragma: no cover


_current_gate: "ContextVar[ConcurrencyGate | None]" = ContextVar(
    "llm_concurrency_gate", default=None)


class ConcurrencyGate:

    def __init__(self, semaphore: asyncio.Semaphore | None) -> None:
        self._semaphore = semaphore
        self._held = False
        self._token: Token[ConcurrencyGate | None] | None = None

    async def __aenter__(self) -> "ConcurrencyGate":
        if self._semaphore is not None:
            await self._semaphore.acquire()
            self._held = True
        self._token = _current_gate.set(self)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._token is not None:
            _current_gate.reset(self._token)
            self._token = None
        if self._semaphore is not None and self._held:
            self._held = False
            self._semaphore.release()

    @contextlib.asynccontextmanager
    async def released(self) -> AsyncIterator[None]:
        if self._semaphore is None or not self._held:
            yield
            return
        self._held = False
        self._semaphore.release()
        try:
            yield
        finally:
            await self._semaphore.acquire()
            self._held = True


@contextlib.asynccontextmanager
async def released_while_waiting() -> AsyncIterator[None]:
    gate = _current_gate.get()
    if gate is None:
        yield
        return
    async with gate.released():
        yield
