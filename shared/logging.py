import logging
import re
import sys
from contextvars import ContextVar
from typing import Any, TypeVar
from uuid import uuid4
import structlog
from prometheus_client import REGISTRY, Counter, Gauge, Histogram
from shared.config import BaseServiceSettings

URL_LOGGERS = ("uvicorn.access", "uvicorn.error")
REDACTED_QUERY = "?[redacted]"


def strip_query(value: object) -> object:
    if isinstance(value, str) and value.startswith("/") and "?" in value:
        return value.split("?", 1)[0] + REDACTED_QUERY
    return value


class QueryStringFilter(logging.Filter):

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(strip_query(arg) for arg in record.args)
        return True


def install_query_string_filter() -> None:
    for name in URL_LOGGERS:
        target = logging.getLogger(name)
        if not any(isinstance(f, QueryStringFilter) for f in target.filters):
            target.addFilter(QueryStringFilter())


def configure_logging(settings: BaseServiceSettings) -> None:
    logging.basicConfig(
        format="%(message)s", stream=sys.stdout, level=settings.LOG_LEVEL)
    for noisy in ("httpx", "httpcore", "botocore", "urllib3", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    install_query_string_filter()

    def add_service_name(
            _logger: structlog.types.WrappedLogger, _method_name: str,
            event_dict: structlog.types.EventDict
    ) -> structlog.types.EventDict:
        event_dict["service"] = settings.SERVICE_NAME
        return event_dict

    shared_processors: list[structlog.types.Processor] = [
        add_service_name, structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level, structlog.stdlib.add_log_level_number,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info
    ]

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if settings.LOG_JSON else structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            settings.LOG_LEVEL), context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True)


REQUEST_ID_HEADER = "x-request-id"
_VALID = re.compile(r"[A-Za-z0-9._-]{1,64}")

Scope = dict[str, Any]
Message = dict[str, Any]

_current: ContextVar[str | None] = ContextVar("request_id", default=None)


def current_request_id() -> str | None:
    return _current.get()


def request_id_from(headers: list[tuple[bytes, bytes]]) -> str:
    for name, value in headers:
        if name.lower() == REQUEST_ID_HEADER.encode():
            candidate = value.decode("latin-1").strip()
            if _VALID.fullmatch(candidate):
                return candidate
    return uuid4().hex


class RequestIdMiddleware:

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = request_id_from(scope.get("headers") or [])
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append(
                    (REQUEST_ID_HEADER.encode(), request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        token = _current.set(request_id)
        try:
            with structlog.contextvars.bound_contextvars(
                    request_id=request_id):
                await self.app(scope, receive, send_with_id)
        finally:
            _current.reset(token)


_C = TypeVar("_C", Counter, Gauge, Histogram)


def get_or_create(cls: type[_C], name: str, doc: str,
                  labels: tuple[str, ...] = ()) -> _C:
    existing = REGISTRY._names_to_collectors.get(name)
    if isinstance(existing, cls):
        return existing
    return cls(name, doc, labels)
