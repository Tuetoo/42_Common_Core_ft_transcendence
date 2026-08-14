import uuid
from typing import Any
import structlog
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

logger = structlog.get_logger(__name__)


class AppError(Exception):
    code: str = "app_error"

    def __init__(self, message: str | None = None):
        self.message = message or self.__class__.__doc__ or self.code
        super().__init__(self.message)


class NotFoundError(AppError):
    """The requested resource does not exist."""
    code = "not found"


class ConflictError(AppError):
    """The request conflicts with the current state."""
    code = "conflict"


class ValidationAppError(AppError):
    """Business-rule validation failed."""
    code = "validation_error"


class PermissionDeniedError(AppError):
    """The authenticated user is not allowed to perform this action."""
    code = "permission_denied"


class InvalidCredentialsError(AppError):
    """Email/password combination is incorrect."""
    code = "invalid_credentials"


class TokenExpiredError(AppError):
    """JWT access/refresh token is invalid or expired."""
    code = "token_expired"


class RateLimitExceededError(AppError):
    """Public API rate limit exceeded for this API key."""
    code = "rate_limit_exceeded"


class UpstreamServiceError(AppError):
    """A call from this service to another service failed or timed out."""
    code = "upstream_service_error"


EXCEPTION_STATUS_MAP: dict[type[AppError], int] = {
    NotFoundError: status.HTTP_404_NOT_FOUND,
    ConflictError: status.HTTP_409_CONFLICT,
    ValidationAppError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    PermissionDeniedError: status.HTTP_403_FORBIDDEN,
    InvalidCredentialsError: status.HTTP_401_UNAUTHORIZED,
    TokenExpiredError: status.HTTP_401_UNAUTHORIZED,
    RateLimitExceededError: status.HTTP_429_TOO_MANY_REQUESTS,
    UpstreamServiceError: status.HTTP_502_BAD_GATEWAY,
}


def _error_payload(request: Request, code: str, message: str
                   ) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": getattr(request.state, "request_id",
                                  str(uuid.uuid4())),
        }
    }


def _status_for(exc: AppError) -> int:
    for cls in type(exc).__mro__:
        if cls in EXCEPTION_STATUS_MAP:
            return EXCEPTION_STATUS_MAP[cls]
    return status.HTTP_400_BAD_REQUEST


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError
                               ) -> JSONResponse:
        status_code = _status_for(exc)
        logger.warning("app_error", code=exc.code, message=exc.message,
                       path=request.url.path)
        return JSONResponse(status_code=status_code, content=_error_payload(
            request, exc.code, exc.message))

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception
                                      ) -> JSONResponse:
        logger.exception("unhandled_exception", path=request.url.path)
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            content=_error_payload(
                                request, "internal_server_error",
                                "An unexpected error occurred."))
