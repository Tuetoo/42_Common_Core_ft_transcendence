from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_tracked: ContextVar[list[str] | None] = ContextVar(
    "ingestion_source_failures", default=None)


def tracking_source_failures() -> bool:
    return _tracked.get() is not None


def report_source_failure(source: str) -> None:
    failures = _tracked.get()
    if failures is not None:
        failures.append(source)


@contextmanager
def track_source_failures() -> Iterator[list[str]]:
    failures: list[str] = []
    token = _tracked.set(failures)
    try:
        yield failures
    finally:
        _tracked.reset(token)
