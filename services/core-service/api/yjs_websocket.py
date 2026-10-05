import contextlib
import time
from dataclasses import dataclass
from uuid import UUID
import structlog
import yjs_relay
from api.ws_common import (CLOSE_FORBIDDEN, CLOSE_INTERNAL_ERROR,
                           CLOSE_POLICY_VIOLATION, CLOSE_TOO_MANY_CONNECTIONS,
                           CLOSE_UNAUTHORIZED, CLOSE_UNSUPPORTED_DATA,
                           SocketIdentity, accept_and_close, authenticate,
                           close_quietly, resolve_live_identity,
                           serve_until_expiry)
from database import SessionLocal
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from websocket_manager import ConnectionLimitExceeded, manager
from domains.research.service import NoteAccess, ResearchService
from shared.security import CurrentUser

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["yjs"])

_READ_ONLY_LOG_INTERVAL_SECONDS = 30.0


@dataclass(eq=False)
class _YjsSession:
    websocket: WebSocket
    note_id: UUID
    identity: SocketIdentity
    access: NoteAccess


_sessions_by_user: dict[str, set[_YjsSession]] = {}


def _register_session(session: _YjsSession) -> None:
    _sessions_by_user.setdefault(session.identity.user_id, set()).add(session)


def _unregister_session(session: _YjsSession) -> None:
    sessions = _sessions_by_user.get(session.identity.user_id)
    if sessions is None:
        return
    sessions.discard(session)
    if not sessions:
        _sessions_by_user.pop(session.identity.user_id, None)


async def recheck_user_sessions(
        user_id: str, *, reason: str, deactivated: bool = False) -> int:
    changed = 0
    for session in list(_sessions_by_user.get(str(user_id), ())):
        identity = (
            None if deactivated else await resolve_live_identity(
                session.identity))
        if identity is None:
            await close_quietly(
                session.websocket, CLOSE_UNAUTHORIZED, "account deactivated")
            _unregister_session(session)
            logger.info(
                "yjs_session_closed_on_access_change",
                note_id=str(session.note_id), user_id=str(user_id),
                reason=reason, code=CLOSE_UNAUTHORIZED)
            changed += 1
            continue
        try:
            access = await _note_access(session.note_id, identity)
        except Exception:
            logger.warning(
                "yjs_session_recheck_failed", note_id=str(session.note_id),
                user_id=str(user_id), reason=reason, exc_info=True)
            continue
        if not access.can_read:
            await close_quietly(
                session.websocket, CLOSE_FORBIDDEN,
                "access to this note was revoked")
            _unregister_session(session)
            logger.info(
                "yjs_session_closed_on_access_change",
                note_id=str(session.note_id), user_id=str(user_id),
                reason=reason, code=CLOSE_FORBIDDEN)
            changed += 1
            continue
        if session.access.can_write and not access.can_write:
            logger.info(
                "yjs_session_downgraded_to_read_only",
                note_id=str(session.note_id), user_id=str(user_id),
                reason=reason)
            changed += 1
        session.access = access
    return changed


async def _note_access(note_id: UUID, identity: SocketIdentity) -> NoteAccess:
    async with SessionLocal() as session:
        service = ResearchService(session)
        note = await service.notes.get_by_id(note_id)
        if note is None:
            return NoteAccess(can_read=False, can_write=False)
        user = CurrentUser(user_id=identity.user_id, role=identity.role)
        return await service.note_access(note, user)


@router.websocket("/ws/yjs/{note_id}")
async def yjs_websocket_endpoint(
        websocket: WebSocket, note_id: str,
        token: str | None = Query(default=None)) -> None:
    token_identity = authenticate(token)
    note_uuid: UUID | None = None
    with contextlib.suppress(ValueError):
        note_uuid = UUID(note_id)
    if token_identity is None or note_uuid is None:
        await accept_and_close(
            websocket, CLOSE_UNAUTHORIZED, "invalid token or note id")
        return
    identity = await resolve_live_identity(token_identity)
    if identity is None:
        await accept_and_close(
            websocket, CLOSE_UNAUTHORIZED, "account deactivated")
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

    try:
        manager.acquire_user(identity.user_id)
    except ConnectionLimitExceeded:
        await accept_and_close(
            websocket, CLOSE_TOO_MANY_CONNECTIONS, "too many connections")
        return

    room_key = str(note_uuid)
    room: yjs_relay.YjsRoom | None = None
    session = _YjsSession(
        websocket=websocket, note_id=note_uuid, identity=token_identity,
        access=access)
    try:
        await websocket.accept()
        room = await yjs_relay.relay.connect(websocket, room_key)
        _register_session(session)

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
        _unregister_session(session)
        try:
            if room is not None:
                await yjs_relay.relay.disconnect(websocket, room)
        finally:
            manager.release_user(identity.user_id)
