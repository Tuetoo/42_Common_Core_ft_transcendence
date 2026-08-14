import logging
import sys
import structlog
from shared.config import BaseServiceSettings


def configure_logging(settings: BaseServiceSettings) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout,
                        level=settings.LOG_LEVEL)

    def add_service_name(
            _logger: structlog.types.WrappedLogger, _method_name: str,
            event_dict: structlog.types.EventDict
            ) -> structlog.types.EventDict:
        event_dict["service"] = settings.SERVICE_NAME
        return event_dict

    shared_processors: list[structlog.types.Processor] = [
        add_service_name,
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_log_level_number,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if settings.LOG_JSON else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            settings.LOG_LEVEL),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
