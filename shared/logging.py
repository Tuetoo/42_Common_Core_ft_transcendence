import logging
import sys
import structlog
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
