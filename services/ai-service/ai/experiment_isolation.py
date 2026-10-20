"""The ANALYSIS_EXPERIMENT_ISOLATION switch: one check, one log event.

Every read or write of earlier analyses' context that the switch turns off
calls `isolation_skips`, so the event name and its fields stay the same
everywhere (handbook chapter 16).
"""
from typing import Any
import structlog
from config import settings

logger = structlog.get_logger(__name__)


def isolation_skips(path: str, **fields: Any) -> bool:
    """Return True, and log the skipped path, when the switch is on."""
    if not settings.ANALYSIS_EXPERIMENT_ISOLATION:
        return False
    logger.info("experiment_isolation.skipped", path=path, **fields)
    return True
