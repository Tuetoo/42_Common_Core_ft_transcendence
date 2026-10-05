import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
import structlog
from config import settings
from fastapi import WebSocket
from starlette.websockets import WebSocketState
from shared.security import decode_token, is_uuid_claim
from shared.user_state import get_user_state

logger = structlog.get_logger(__name__)

CLOSE_UNSUPPORTED_DATA = 1003
CLOSE_POLICY_VIOLATION = 1008
CLOSE_INTERNAL_ERROR = 1011
CLOSE_BAD_REQUEST = 4400
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_TOO_MANY_CONNECTIONS = 4429


@dataclass(frozen=True)
class SocketIdentity:
    user_id: str
    expires_at: float
    role: str = "user"


def authenticate(token: str | None) -> SocketIdentity | None:
    try:
        claims: dict[str, Any] = decode_token(token or "", settings)
    except Exception:
        return None
    subject = claims.get("sub")
    exp = claims.get("exp")
    if claims.get("type") != "access" or not is_uuid_claim(subject):
        return None
    if not isinstance(exp, int | float):
        return None
    return SocketIdentity(
        user_id=subject, expires_at=float(exp),
        role=str(claims.get("role") or "user"))


async def resolve_live_identity(
        identity: SocketIdentity) -> SocketIdentity | None:
    if not settings.USER_STATE_CHECK_ENABLED:
        return identity
    state = await get_user_state(settings, identity.user_id)
    if state is None:
        return identity
    if not state.is_active:
        return None
    return SocketIdentity(
        user_id=identity.user_id, expires_at=identity.expires_at,
        role=state.role)


async def accept_and_close(
        websocket: WebSocket, code: int, reason: str) -> None:
    try:
        if websocket.application_state == WebSocketState.CONNECTING:
            await websocket.accept()
        await close_quietly(websocket, code, reason)
    except Exception:
        logger.debug("websocket_accept_close_failed", code=code, exc_info=True)


async def close_quietly(
        websocket: WebSocket, code: int, reason: str = "") -> None:
    try:
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.close(code=code, reason=reason)
    except Exception:
        logger.debug("websocket_close_failed", code=code, exc_info=True)


async def serve_until_expiry(
        websocket: WebSocket, identity: SocketIdentity,
        receive_loop: Callable[[], Awaitable[None]]) -> None:
    remaining = identity.expires_at - time.time()
    if remaining <= 0:
        await close_quietly(websocket, CLOSE_UNAUTHORIZED, "token expired")
        return
    timeout_cm = asyncio.timeout(remaining)
    try:
        async with timeout_cm:
            await receive_loop()
    except TimeoutError:
        if not timeout_cm.expired():
            raise
        await close_quietly(websocket, CLOSE_UNAUTHORIZED, "token expired")
