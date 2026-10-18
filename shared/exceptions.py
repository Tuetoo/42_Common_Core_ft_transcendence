from uuid import uuid4
import structlog
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

logger = structlog.get_logger(__name__)


class AppError(Exception):
    code: str = "app_error"
    default_message: str = ""

    def __init__(self, message: str | None = None):
        self.message = message or self.default_message or self.code
        super().__init__(self.message)


class NotFoundError(AppError):
    default_message = "The requested resource does not exist."
    code = "not_found"


class ConflictError(AppError):
    default_message = "The request conflicts with the current state."
    code = "conflict"


class ValidationAppError(AppError):
    default_message = "Business-rule validation failed."
    code = "validation_error"


class PermissionDeniedError(AppError):
    default_message = (
        "The authenticated user is not allowed to perform this action.")
    code = "permission_denied"


class InvalidCredentialsError(AppError):
    default_message = "Email/password combination is incorrect."
    code = "invalid_credentials"


class RateLimitExceededError(AppError):
    default_message = "Rate limit exceeded."
    code = "rate_limit_exceeded"


class UpstreamServiceError(AppError):
    default_message = (
        "A call from this service to another service failed or timed out.")
    code = "upstream_service_error"


class ServiceUnavailableError(AppError):
    default_message = "The service is temporarily unavailable."
    code = "service_unavailable"
    retry_after_seconds: int | None = None


EXCEPTION_STATUS_MAP: dict[type[AppError], int] = {
    NotFoundError: status.HTTP_404_NOT_FOUND,
    ConflictError: status.HTTP_409_CONFLICT,
    ValidationAppError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    PermissionDeniedError: status.HTTP_403_FORBIDDEN,
    InvalidCredentialsError: status.HTTP_401_UNAUTHORIZED,
    RateLimitExceededError: status.HTTP_429_TOO_MANY_REQUESTS,
    UpstreamServiceError: status.HTTP_502_BAD_GATEWAY,
    ServiceUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE
}


def error_response(
        request: Request, status_code: int, code: str, message: str,
        headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={
            "error": {
                "code": code,
                "message": message,
                "request_id": getattr(
                    request.state, "request_id", str(uuid4()))
            }
        }, headers=headers)


def _status_for(exc: AppError) -> int:
    for cls in type(exc).__mro__:
        if cls in EXCEPTION_STATUS_MAP:
            return EXCEPTION_STATUS_MAP[cls]
    return status.HTTP_400_BAD_REQUEST


def register_exception_handlers(app: FastAPI) -> None:

    @app.exception_handler(AppError)
    async def handle_app_error(
            request: Request, exc: AppError) -> JSONResponse:
        status_code = _status_for(exc)
        logger.warning(
            "app_error", code=exc.code, message=exc.message,
            path=request.url.path)
        retry_after = getattr(exc, "retry_after_seconds", None)
        headers = ({
            "Retry-After": str(retry_after)
        } if retry_after is not None else None)
        return error_response(
            request, status_code, exc.code, exc.message, headers)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(
            request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid4()))
        logger.exception(
            "unhandled_exception", path=request.url.path,
            request_id=request_id)
        request.state.request_id = request_id
        return error_response(
            request, status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_server_error", "An unexpected error occurred.",
            headers={"X-Request-ID": request_id})
