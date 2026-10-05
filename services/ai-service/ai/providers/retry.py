import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar
import httpx
import structlog

logger = structlog.get_logger(__name__)

T = TypeVar("T")

DEFAULT_MAX_ATTEMPTS = 3
_BASE_DELAY_SECONDS = 1.0
_BACKOFF_MULTIPLIER = 2.0
_JITTER_FRACTION = 0.3


def _delay_for_attempt(attempt: int) -> float:
    base = _BASE_DELAY_SECONDS * (_BACKOFF_MULTIPLIER**attempt)
    jitter = base * random.uniform(-_JITTER_FRACTION, _JITTER_FRACTION)
    return max(0.1, base + jitter)


def is_retryable_http_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


def retry_everything(exc: Exception) -> bool:
    return True


async def call_with_retry(
        func: Callable[[], Awaitable[T]], *, op_name: str,
        should_retry: Callable[[Exception], bool] = is_retryable_http_error,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> T:
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await func()
        except Exception as exc:
            last_exc = exc
            is_last_attempt = attempt == max_attempts - 1
            if is_last_attempt or not should_retry(exc):
                raise
            delay = _delay_for_attempt(attempt)
            logger.warning(
                "provider_retry.retrying", op=op_name, attempt=attempt + 1,
                max_attempts=max_attempts, delay_seconds=round(delay, 2),
                error=str(exc))
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc
