import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar
from uuid import uuid4
import clients.auth_client as auth_client
import structlog
from config import settings
from fastapi import WebSocket
from pydantic import Field
from starlette.websockets import WebSocketState
from shared.events import run_pubsub_listener
from shared.redis import get_events_client
from shared.schemas import BaseSchema
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


async def open_identity(
        websocket: WebSocket, token: str | None, *,
        invalid_reason: str = "invalid or missing token"
) -> SocketIdentity | None:
    token_identity = authenticate(token)
    if token_identity is None:
        await accept_and_close(websocket, CLOSE_UNAUTHORIZED, invalid_reason)
        return None
    identity = await resolve_live_identity(token_identity)
    if identity is None:
        await accept_and_close(
            websocket, CLOSE_UNAUTHORIZED, "account deactivated")
    return identity


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


MAX_CONNECTIONS_PER_USER = 20
PRESENCE_HEARTBEAT_SECONDS = 60.0
FANOUT_CHANNEL = "ws:fanout"
_REPLICA_ID = uuid4().hex

T = TypeVar("T")


class WebSocketEnvelope(BaseSchema, Generic[T]):
    event: str
    payload: T
    emitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ConnectionLimitExceeded(Exception):
    pass


class WebSocketManager:

    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}
        self._user_counts: dict[str, int] = {}
        self._presence_tasks: dict[str, asyncio.Task[None]] = {}
        self._presence_wakeups: dict[str, asyncio.Event] = {}

    def acquire_user(self, user_id: str) -> None:
        count = self._user_counts.get(user_id, 0)
        if count >= MAX_CONNECTIONS_PER_USER:
            raise ConnectionLimitExceeded(user_id)
        self._user_counts[user_id] = count + 1
        if user_id not in self._presence_tasks:
            self._presence_wakeups[user_id] = asyncio.Event()
            self._presence_tasks[user_id] = asyncio.create_task(
                self._presence_loop(user_id))

    def release_user(self, user_id: str) -> None:
        count = self._user_counts.get(user_id, 0) - 1
        if count > 0:
            self._user_counts[user_id] = count
            return
        self._user_counts.pop(user_id, None)
        wakeup = self._presence_wakeups.get(user_id)
        if wakeup is not None:
            wakeup.set()

    async def _presence_loop(self, user_id: str) -> None:
        try:
            await auth_client.set_user_presence(user_id, online=True)
            while True:
                wakeup = self._presence_wakeups[user_id]
                try:
                    await asyncio.wait_for(
                        wakeup.wait(), timeout=PRESENCE_HEARTBEAT_SECONDS)
                    timed_out = False
                except TimeoutError:
                    timed_out = True
                wakeup.clear()
                if self._user_counts.get(user_id, 0) > 0:
                    if timed_out:
                        await auth_client.set_user_presence(
                            user_id, online=True)
                    continue
                await auth_client.set_user_presence(user_id, online=False)
                if self._user_counts.get(user_id, 0) > 0:
                    await auth_client.set_user_presence(user_id, online=True)
                    continue
                break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("presence_loop_failed", user_id=user_id)
        finally:
            self._presence_tasks.pop(user_id, None)
            self._presence_wakeups.pop(user_id, None)

    async def connect(self, websocket: WebSocket, channel: str) -> None:
        self._connections.setdefault(channel, set()).add(websocket)

    def disconnect(self, websocket: WebSocket, channel: str) -> None:
        sockets = self._connections.get(channel)
        if sockets is None:
            return
        sockets.discard(websocket)
        if not sockets:
            self._connections.pop(channel, None)

    def sockets_for(self,
                    channels: Iterable[str]) -> list[tuple[WebSocket, str]]:
        seen: set[int] = set()
        targets: list[tuple[WebSocket, str]] = []
        for channel in dict.fromkeys(channels):
            for connection in list(self._connections.get(channel, set())):
                if id(connection) in seen:
                    continue
                seen.add(id(connection))
                targets.append((connection, channel))
        return targets

    async def _deliver_local(self, channels: Iterable[str], text: str) -> int:
        dead: list[tuple[WebSocket, str, Exception]] = []
        sent = 0
        for connection, channel in self.sockets_for(channels):
            try:
                await connection.send_text(text)
                sent += 1
            except Exception as exc:
                dead.append((connection, channel, exc))
        for connection, channel, error in dead:
            logger.warning(
                "websocket_send_failed", channel=channel,
                error_type=type(error).__name__, error=str(error)[:200])
            self.disconnect(connection, channel)
            with contextlib.suppress(Exception):
                await connection.close(code=1011)
        return sent

    async def broadcast(
            self, channel: str, envelope: WebSocketEnvelope[Any], *,
            cross_replica: bool = False) -> int:
        text = envelope.model_dump_json()
        sent = await self._deliver_local([channel], text)
        if cross_replica:
            await _publish_fanout([channel], text)
        return sent

    async def deliver_remote(self, raw: str) -> int:
        try:
            data = json.loads(raw)
            if data.get("origin") == _REPLICA_ID:
                return 0
            channels = [str(c) for c in data["channels"]]
            text = str(data["text"])
        except (ValueError, KeyError, TypeError):
            logger.warning("websocket_fanout_message_invalid")
            return 0
        return await self._deliver_local(channels, text)


manager = WebSocketManager()


async def acquire_or_close(websocket: WebSocket, user_id: str) -> bool:
    try:
        manager.acquire_user(user_id)
    except ConnectionLimitExceeded:
        await accept_and_close(
            websocket, CLOSE_TOO_MANY_CONNECTIONS, "too many connections")
        return False
    return True


async def _publish_fanout(channels: list[str], text: str) -> None:
    try:
        await get_events_client(settings).publish(
            FANOUT_CHANNEL,
            json.dumps({
                "origin": _REPLICA_ID,
                "channels": channels,
                "text": text
            }))
    except Exception:
        logger.warning("websocket_fanout_publish_failed", exc_info=True)


async def _deliver_fanout_message(message: dict[str, Any]) -> None:
    if message.get("type") != "message":
        return
    try:
        await manager.deliver_remote(message["data"])
    except Exception:
        logger.exception("websocket_fanout_delivery_failed")


async def _subscribe_fanout(pubsub: Any) -> None:
    await pubsub.subscribe(FANOUT_CHANNEL)


async def run_cross_replica_listener() -> None:
    await run_pubsub_listener(
        settings, name="websocket_fanout_listener",
        subscribe=_subscribe_fanout, handle=_deliver_fanout_message)
