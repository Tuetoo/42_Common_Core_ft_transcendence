import contextlib
import time
from dataclasses import dataclass
from typing import TypeVar
from uuid import UUID
import clients.ai_client as ai_client
import clients.auth_client as auth_client
import structlog
import yjs_relay
from database import SessionLocal
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from websocket_manager import (CLOSE_BAD_REQUEST, CLOSE_FORBIDDEN,
                               CLOSE_INTERNAL_ERROR, CLOSE_POLICY_VIOLATION,
                               CLOSE_UNAUTHORIZED, CLOSE_UNSUPPORTED_DATA,
                               SocketIdentity, WebSocketEnvelope,
                               accept_and_close, acquire_or_close,
                               close_quietly, manager, open_identity,
                               resolve_live_identity, serve_until_expiry)
from domains.portfolio.service import PortfolioService
from domains.research.service import NoteAccess, ResearchService
from shared.security import CurrentUser

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["websocket"])
yjs_router = APIRouter(tags=["yjs"])

_CHANNEL_PREFIXES = ("user", "presence", "analysis", "document")
_READ_ONLY_LOG_INTERVAL_SECONDS = 30.0
ANALYSIS_STATUS_EVENT = "analysis.status"


@dataclass(eq=False)
class _ChannelSession:
    websocket: WebSocket
    channel: str
    identity: SocketIdentity


@dataclass(eq=False)
class _YjsSession:
    websocket: WebSocket
    note_id: UUID
    identity: SocketIdentity
    access: NoteAccess


_S = TypeVar("_S", _ChannelSession, _YjsSession)

_channel_sessions_by_user: dict[str, set[_ChannelSession]] = {}
_sessions_by_user: dict[str, set[_YjsSession]] = {}


def _register(registry: dict[str, set[_S]], session: _S) -> None:
    registry.setdefault(session.identity.user_id, set()).add(session)


def _unregister(registry: dict[str, set[_S]], session: _S) -> None:
    sessions = registry.get(session.identity.user_id)
    if sessions is None:
        return
    sessions.discard(session)
    if not sessions:
        registry.pop(session.identity.user_id, None)


async def _can_read_analysis(requester_id: str, analysis_id: UUID) -> bool:
    ref = await ai_client.get_analysis_ref(str(analysis_id))
    if ref.user_id is not None:
        return str(ref.user_id) == requester_id
    if ref.portfolio_id is None:
        return False
    async with SessionLocal() as session:
        await PortfolioService(session).get(
            ref.portfolio_id, UUID(requester_id))
    return True


async def _can_read_document(requester_id: str, document_id: UUID) -> bool:
    async with SessionLocal() as session:
        await ResearchService(session).get_document(
            document_id, UUID(requester_id))
    return True


async def send_analysis_status(websocket: WebSocket, analysis_id: str) -> None:
    try:
        ref = await ai_client.get_analysis_ref(analysis_id)
        envelope = WebSocketEnvelope(
            event=ANALYSIS_STATUS_EVENT, payload={
                "analysis_id": str(ref.id),
                "ticker": ref.ticker,
                "status": str(ref.status),
                "portfolio_id": (
                    str(ref.portfolio_id) if ref.portfolio_id else None)
            })
        await websocket.send_text(envelope.model_dump_json())
    except Exception:
        logger.info(
            "websocket_analysis_status_skipped", analysis_id=analysis_id,
            exc_info=True)


async def _channel_allowed(channel: str, requester_id: str) -> bool:
    prefix, _, target = channel.partition(":")
    if prefix == "user":
        return target == requester_id
    if prefix == "presence":
        return (target == requester_id
                or await auth_client.are_users_connected(requester_id, target))
    if prefix == "analysis":
        return await _can_read_analysis(requester_id, UUID(target))
    return await _can_read_document(requester_id, UUID(target))


async def authorize_channel(channel: str,
                            requester_id: str) -> tuple[bool, int]:
    prefix, sep, target = channel.partition(":")
    if not sep or prefix not in _CHANNEL_PREFIXES or not target:
        return False, CLOSE_BAD_REQUEST
    try:
        allowed = await _channel_allowed(channel, requester_id)
    except Exception:
        logger.info(
            "websocket_channel_auth_denied", channel=channel, exc_info=True)
        allowed = False
    return allowed, CLOSE_FORBIDDEN


async def _note_access(note_id: UUID, identity: SocketIdentity) -> NoteAccess:
    async with SessionLocal() as session:
        service = ResearchService(session)
        note = await service.notes.get_by_id(note_id)
        if note is None:
            return NoteAccess(can_read=False, can_write=False)
        user = CurrentUser(user_id=identity.user_id, role=identity.role)
        return await service.note_access(note, user)


async def _close_on_access_change(
        websocket: WebSocket, code: int, message: str, *, event: str,
        user_id: str, **fields: str) -> None:
    await close_quietly(websocket, code, message)
    logger.info(event, user_id=user_id, code=code, **fields)


async def _recheck_channel_sessions(
        user_id: str, *, reason: str, deactivated: bool) -> int:
    changed = 0
    for session in list(_channel_sessions_by_user.get(user_id, ())):
        identity = (
            None if deactivated else await resolve_live_identity(
                session.identity))
        if identity is None:
            code, message = CLOSE_UNAUTHORIZED, "account deactivated"
        else:
            try:
                allowed = await _channel_allowed(session.channel, user_id)
            except Exception:
                logger.warning(
                    "websocket_session_recheck_failed",
                    channel=session.channel, user_id=user_id, reason=reason,
                    exc_info=True)
                continue
            if allowed:
                continue
            code, message = (
                CLOSE_FORBIDDEN, "access to this channel was revoked")
        _unregister(_channel_sessions_by_user, session)
        await _close_on_access_change(
            session.websocket, code, message,
            event="websocket_session_closed_on_access_change",
            user_id=user_id, channel=session.channel, reason=reason)
        changed += 1
    return changed


async def recheck_user_sessions(
        user_id: str, *, reason: str, deactivated: bool = False) -> int:
    user_id = str(user_id)
    changed = await _recheck_channel_sessions(
        user_id, reason=reason, deactivated=deactivated)
    for session in list(_sessions_by_user.get(user_id, ())):
        identity = (
            None if deactivated else await resolve_live_identity(
                session.identity))
        if identity is None:
            _unregister(_sessions_by_user, session)
            await _close_on_access_change(
                session.websocket, CLOSE_UNAUTHORIZED, "account deactivated",
                event="yjs_session_closed_on_access_change", user_id=user_id,
                note_id=str(session.note_id), reason=reason)
            changed += 1
            continue
        try:
            access = await _note_access(session.note_id, identity)
        except Exception:
            logger.warning(
                "yjs_session_recheck_failed", note_id=str(session.note_id),
                user_id=user_id, reason=reason, exc_info=True)
            continue
        if not access.can_read:
            message = "access to this note was revoked"
        elif session.access.can_write and not access.can_write:
            message = "write access to this note was revoked"
        else:
            session.access = access
            continue
        _unregister(_sessions_by_user, session)
        await _close_on_access_change(
            session.websocket, CLOSE_FORBIDDEN, message,
            event="yjs_session_closed_on_access_change", user_id=user_id,
            note_id=str(session.note_id), reason=reason)
        changed += 1
    return changed


@router.websocket("/ws/{channel}")
async def websocket_endpoint(
        websocket: WebSocket, channel: str,
        token: str | None = Query(default=None)) -> None:
    identity = await open_identity(websocket, token)
    if identity is None:
        return
    requester_id = identity.user_id

    allowed, code = await authorize_channel(channel, requester_id)
    if not allowed:
        await accept_and_close(
            websocket, code, "not authorized for this channel")
        return

    if not await acquire_or_close(websocket, requester_id):
        return

    session = _ChannelSession(
        websocket=websocket, channel=channel, identity=identity)
    traced = False
    try:
        await websocket.accept()
        await manager.connect(websocket, channel)
        _register(_channel_sessions_by_user, session)
        prefix, _, target = channel.partition(":")
        traced = prefix in ("analysis", "document")
        if traced:
            logger.info(
                "websocket_opened", channel=channel, user_id=requester_id)
        if prefix == "analysis":
            await send_analysis_status(websocket, target)

        async def _receive_loop() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("text") is None:
                    await close_quietly(
                        websocket, CLOSE_UNSUPPORTED_DATA, "text frames only")
                    return

        await serve_until_expiry(websocket, identity, _receive_loop)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("websocket_connection_failed", channel=channel)
        await close_quietly(websocket, CLOSE_INTERNAL_ERROR)
    finally:
        _unregister(_channel_sessions_by_user, session)
        manager.disconnect(websocket, channel)
        manager.release_user(requester_id)
        if traced:
            logger.info(
                "websocket_closed", channel=channel, user_id=requester_id)


@yjs_router.websocket("/ws/yjs/{note_id}")
async def yjs_websocket_endpoint(
        websocket: WebSocket, note_id: str,
        token: str | None = Query(default=None)) -> None:
    note_uuid: UUID | None = None
    with contextlib.suppress(ValueError):
        note_uuid = UUID(note_id)
    if note_uuid is None:
        await accept_and_close(
            websocket, CLOSE_UNAUTHORIZED, "invalid token or note id")
        return
    identity = await open_identity(
        websocket, token, invalid_reason="invalid token or note id")
    if identity is None:
        return

    try:
        access = await _note_access(note_uuid, identity)
    except Exception:
        logger.info("yjs_note_auth_failed", note_id=note_id, exc_info=True)
        access = NoteAccess(can_read=False, can_write=False)
    if not access.can_read:
        await accept_and_close(
            websocket, CLOSE_FORBIDDEN, "not authorized for this note")
        return

    if not await acquire_or_close(websocket, identity.user_id):
        return

    room_key = str(note_uuid)
    room: yjs_relay.YjsRoom | None = None
    session = _YjsSession(
        websocket=websocket, note_id=note_uuid, identity=identity,
        access=access)
    try:
        await websocket.accept()
        room = await yjs_relay.relay.connect(websocket, room_key)
        _register(_sessions_by_user, session)

        async def _receive_loop() -> None:
            assert room is not None
            last_read_only_log = 0.0
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                update = message.get("bytes")
                if update is None:
                    await close_quietly(
                        websocket, CLOSE_UNSUPPORTED_DATA,
                        "binary frames only")
                    return
                if not session.access.can_write:
                    now = time.monotonic()
                    if (now - last_read_only_log
                            >= _READ_ONLY_LOG_INTERVAL_SECONDS):
                        last_read_only_log = now
                        logger.info(
                            "yjs_read_only_update_dropped", note_id=room_key,
                            user_id=identity.user_id)
                    continue
                try:
                    await yjs_relay.relay.handle_local_update(
                        room, update, sender=websocket)
                except yjs_relay.InvalidYjsUpdate as exc:
                    logger.warning(
                        "yjs_invalid_update", note_id=room_key,
                        user_id=identity.user_id, size=len(update),
                        error=str(exc)[:200])
                    await close_quietly(
                        websocket, CLOSE_POLICY_VIOLATION,
                        "invalid Yjs update")
                    return

        await serve_until_expiry(websocket, identity, _receive_loop)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("yjs_connection_failed", note_id=room_key)
        await close_quietly(websocket, CLOSE_INTERNAL_ERROR)
    finally:
        _unregister(_sessions_by_user, session)
        try:
            if room is not None:
                await yjs_relay.relay.disconnect(websocket, room)
        finally:
            manager.release_user(identity.user_id)
