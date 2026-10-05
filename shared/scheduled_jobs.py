from typing import Any
import structlog
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

logger = structlog.get_logger(__name__)

ALL_UNITS_FAILED_STATUS = 500


def all_units_failed(units: int, failed: int) -> bool:
    return units > 0 and failed >= units


def job_response(
        job: str, result: dict[str, Any], *,
        steps: dict[str, tuple[int, int]]) -> JSONResponse:
    failed_steps = [
        name for name, (units, failed) in steps.items()
        if all_units_failed(units, failed)
    ]
    if failed_steps:
        logger.error(
            "scheduled_job_all_units_failed", job=job,
            steps={name: steps[name]
                   for name in failed_steps})
        return JSONResponse(
            status_code=ALL_UNITS_FAILED_STATUS, content=jsonable_encoder({
                **result, "all_units_failed": failed_steps
            }))
    return JSONResponse(status_code=200, content=jsonable_encoder(result))
