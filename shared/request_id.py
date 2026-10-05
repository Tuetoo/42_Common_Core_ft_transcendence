import re
from contextvars import ContextVar
from typing import Any
from uuid import uuid4
import structlog

HEADER = "x-request-id"
_VALID = re.compile(r"[A-Za-z0-9._-]{1,64}")

Scope = dict[str, Any]
Message = dict[str, Any]

_current: ContextVar[str | None] = ContextVar("request_id", default=None)


def current_request_id() -> str | None:
    return _current.get()


def request_id_from(headers: list[tuple[bytes, bytes]]) -> str:
    for name, value in headers:
        if name.lower() == HEADER.encode():
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
                headers.append((HEADER.encode(), request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        token = _current.set(request_id)
        try:
            with structlog.contextvars.bound_contextvars(
                    request_id=request_id):
                await self.app(scope, receive, send_with_id)
        finally:
            _current.reset(token)
