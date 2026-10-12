import asyncio
import base64
import contextlib
import time
from typing import Any
from uuid import UUID, uuid4
import structlog
import y_py as Y
from config import settings
from database import SessionLocal
from fastapi import WebSocket
from sqlalchemy import select, update
from websocket_manager import CLOSE_FORBIDDEN
from domains.research import yjs_guard
from domains.research.sentiment import schedule_note_sentiment
from shared.events import run_pubsub_listener
from shared.redis import get_events_client

logger = structlog.get_logger(__name__)

_SNAPSHOT_MIN_UPDATES = 20
_SNAPSHOT_DEBOUNCE_SECONDS = 30
_REDIS_CHANNEL_PREFIX = "yjs:note:"
_REPLICA_ID = uuid4().hex

_ACTIVE_KEY_PREFIX = "yjs:active:"
_ACTIVE_TTL_SECONDS = 90
ACTIVE_HEARTBEAT_SECONDS = 30.0


class InvalidYjsUpdate(Exception):
    pass


class YjsRoom:

    def __init__(self, note_id: str, initial_state: bytes | None) -> None:
        self.note_id = note_id
        self.doc = Y.YDoc()
        if initial_state:
            try:
                yjs_guard.guarded(
                    lambda: Y.apply_update(self.doc, initial_state),
                    what="load_state")
            except yjs_guard.YjsDecodeError:
                logger.exception("yjs_stored_state_invalid", note_id=note_id)
        self.connections: set[WebSocket] = set()
        self._updates_since_snapshot = 0
        self._last_snapshot_at = time.monotonic()
        self._dirty = False
        self.lock = asyncio.Lock()

    def encode_state(self) -> bytes:
        return bytes(
            yjs_guard.guarded(
                lambda: Y.encode_state_as_update(self.doc),
                what="encode_state"))

    def apply(self, update: bytes, *, counts_toward_snapshot: bool) -> None:
        yjs_guard.guarded(
            lambda: Y.apply_update(self.doc, update), what="apply")
        if counts_toward_snapshot:
            self._updates_since_snapshot += 1
            self._dirty = True

    def should_snapshot(self) -> bool:
        return self._dirty and (
            self._updates_since_snapshot >= _SNAPSHOT_MIN_UPDATES or
            (time.monotonic() - self._last_snapshot_at)
            >= _SNAPSHOT_DEBOUNCE_SECONDS)

    def mark_snapshotted(self) -> None:
        self._updates_since_snapshot = 0
        self._last_snapshot_at = time.monotonic()
        self._dirty = False


def _in_use(room: YjsRoom) -> bool:
    return bool(room.connections) or room._dirty


class YjsRelay:

    def __init__(self) -> None:
        self._rooms: dict[str, YjsRoom] = {}
        self._creation_lock = asyncio.Lock()

    async def get_or_create_room(self, note_id: str) -> YjsRoom:
        room = self._rooms.get(note_id)
        if room is not None:
            return room
        async with self._creation_lock:
            room = self._rooms.get(note_id)
            if room is not None:
                return room
            initial_state = await _load_snapshot(note_id)
            room = YjsRoom(note_id, initial_state)
            self._rooms[note_id] = room
            return room

    def has_local_room(self, note_id: str) -> bool:
        room = self._rooms.get(note_id)
        return room is not None and _in_use(room)

    def local_active_note_ids(self) -> list[str]:
        return [nid for nid, room in self._rooms.items() if _in_use(room)]

    async def connect(self, websocket: WebSocket, note_id: str) -> YjsRoom:
        await _mark_room_active(note_id)
        try:
            room = await self.get_or_create_room(note_id)
        except BaseException:
            if note_id not in self._rooms:
                await _mark_room_inactive(note_id)
            raise
        room.connections.add(websocket)
        try:
            await websocket.send_bytes(room.encode_state())
        except BaseException:
            await self.disconnect(websocket, room)
            raise
        return room

    async def disconnect(self, websocket: WebSocket, room: YjsRoom) -> None:
        room.connections.discard(websocket)
        if room.connections:
            return
        async with room.lock:
            if room.connections:
                return
            should_persist = room._dirty
            snapshot = room.encode_state() if should_persist else None
        if should_persist:
            assert snapshot is not None
            try:
                if await _persist_snapshot(room.note_id, snapshot):
                    room.mark_snapshotted()
            except Exception:
                logger.exception(
                    "yjs_snapshot_persist_failed_on_disconnect",
                    note_id=room.note_id)
                return
        await self._evict_if_empty(room)

    async def _evict_if_empty(self, room: YjsRoom) -> None:
        async with self._creation_lock:
            if not room.connections and self._rooms.get(room.note_id) is room:
                self._rooms.pop(room.note_id, None)
                evicted = True
            else:
                evicted = False
        if evicted:
            await _mark_room_inactive(room.note_id)

    async def close_note_sessions(
            self, note_id: str, *, reason: str, discard_edits: bool) -> int:
        room = self._rooms.get(str(note_id))
        if room is None:
            return 0
        if discard_edits:
            async with room.lock:
                room.mark_snapshotted()
        sockets = list(room.connections)
        for websocket in sockets:
            try:
                await websocket.close(code=CLOSE_FORBIDDEN, reason=reason)
            except Exception:
                room.connections.discard(websocket)
        if discard_edits and not room.connections:
            await self._evict_if_empty(room)
        logger.info(
            "yjs_note_sessions_closed", note_id=str(note_id),
            sessions=len(sockets), reason=reason)
        return len(sockets)

    async def handle_local_update(
            self, room: YjsRoom, update: bytes, sender: WebSocket) -> None:
        limit = settings.YJS_MAX_UPDATE_BYTES
        if len(update) > limit:
            raise InvalidYjsUpdate(
                f"update of {len(update)} bytes exceeds {limit}")
        reason = await yjs_guard.validator.validate(update)
        if reason is not None:
            raise InvalidYjsUpdate(reason)
        async with room.lock:
            try:
                room.apply(update, counts_toward_snapshot=True)
            except yjs_guard.YjsDecodeError as exc:
                raise InvalidYjsUpdate(str(exc)) from exc
            await self._broadcast_local(room, update, exclude=sender)
            do_snapshot = room.should_snapshot()
            snapshot = room.encode_state() if do_snapshot else None
        await _publish_cross_replica(room.note_id, update)
        if do_snapshot:
            assert snapshot is not None
            try:
                persisted = await _persist_snapshot(room.note_id, snapshot)
            except Exception:
                logger.exception(
                    "yjs_snapshot_persist_failed", note_id=room.note_id)
            else:
                if persisted:
                    async with room.lock:
                        room.mark_snapshotted()
                else:
                    logger.warning(
                        "yjs_persist_snapshot_note_missing",
                        note_id=room.note_id)

    async def flush_due_snapshots(self) -> int:
        saved = 0
        for room in list(self._rooms.values()):
            async with room.lock:
                idle = not room.connections
                due = room.should_snapshot() or (idle and room._dirty)
                snapshot = room.encode_state() if due else None
            if snapshot is not None:
                try:
                    persisted = await _persist_snapshot(room.note_id, snapshot)
                except Exception:
                    logger.exception(
                        "yjs_snapshot_flush_failed", note_id=room.note_id)
                    continue
                if persisted:
                    async with room.lock:
                        room.mark_snapshotted()
                    saved += 1
                elif idle:
                    async with room.lock:
                        room.mark_snapshotted()
            if idle and not room._dirty:
                await self._evict_if_empty(room)
        return saved

    async def persist_all_dirty(self) -> int:
        saved = 0
        for room in list(self._rooms.values()):
            async with room.lock:
                if not room._dirty:
                    continue
                snapshot = room.encode_state()
            try:
                if await _persist_snapshot(room.note_id, snapshot):
                    async with room.lock:
                        room.mark_snapshotted()
                    saved += 1
            except Exception:
                logger.exception(
                    "yjs_snapshot_shutdown_save_failed", note_id=room.note_id)
        if saved:
            logger.info("yjs_rooms_saved_on_shutdown", rooms=saved)
        return saved

    async def apply_remote_update(self, note_id: str, update: bytes) -> None:
        room = self._rooms.get(note_id)
        if room is None:
            return
        async with room.lock:
            try:
                room.apply(update, counts_toward_snapshot=False)
            except yjs_guard.YjsDecodeError:
                logger.warning(
                    "yjs_remote_update_rejected", note_id=note_id,
                    exc_info=True)
                return
            await self._broadcast_local(room, update, exclude=None)

    async def _broadcast_local(
            self, room: YjsRoom, update: bytes,
            exclude: WebSocket | None) -> None:
        dead: list[WebSocket] = []
        for ws in room.connections:
            if ws is exclude:
                continue
            try:
                await ws.send_bytes(update)
            except Exception:
                dead.append(ws)
        for ws in dead:
            room.connections.discard(ws)
            with contextlib.suppress(Exception):
                await ws.close()


relay = YjsRelay()


async def _publish_cross_replica(note_id: str, update: bytes) -> None:
    client = get_events_client(settings)
    encoded = base64.b64encode(update).decode("ascii")
    await client.publish(
        f"{_REDIS_CHANNEL_PREFIX}{note_id}", f"{_REPLICA_ID}:{encoded}")


async def _subscribe_note_channels(pubsub: Any) -> None:
    await pubsub.psubscribe(f"{_REDIS_CHANNEL_PREFIX}*")


async def run_cross_replica_listener() -> None:
    await run_pubsub_listener(
        settings, name="yjs_cross_replica_listener",
        subscribe=_subscribe_note_channels,
        handle=_handle_cross_replica_message)


async def _handle_cross_replica_message(message: dict[str, Any]) -> None:
    if message["type"] != "pmessage":
        return
    note_id = message["channel"].removeprefix(_REDIS_CHANNEL_PREFIX)
    try:
        origin_id, _, encoded = message["data"].partition(":")
        if origin_id == _REPLICA_ID:
            return
        update = base64.b64decode(encoded)
        await relay.apply_remote_update(note_id, update)
    except Exception:
        logger.exception("yjs_cross_replica_update_failed", note_id=note_id)


async def _load_snapshot(note_id: str) -> bytes | None:
    from domains.research.model import ResearchNote

    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(ResearchNote.yjs_state, ResearchNote.content).where(
                    ResearchNote.id == UUID(note_id)).with_for_update()
            )).one_or_none()
        if row is None:
            return None
        if row.yjs_state:
            return bytes(row.yjs_state)
        if not row.content:
            return None
        seeded = yjs_guard.seed_state(row.content)
        await session.execute(
            update(ResearchNote).where(
                ResearchNote.id == UUID(note_id)).values(
                    yjs_state=seeded, updated_at=ResearchNote.updated_at))
        await session.commit()
        return seeded


def _merge_with_stored(
        note_id: str, stored: bytes | None, state: bytes) -> bytes:
    if not stored:
        return state
    try:
        return yjs_guard.merge_states(bytes(stored), state)
    except yjs_guard.YjsDecodeError:
        logger.warning(
            "yjs_stored_state_replaced", note_id=note_id, exc_info=True)
        return state


async def _persist_snapshot(note_id: str, state: bytes) -> bool:
    from domains.research.model import ResearchNote

    note_uuid = UUID(note_id)
    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(ResearchNote.yjs_state, ResearchNote.content).where(
                    ResearchNote.id == note_uuid).with_for_update()
            )).one_or_none()
        if row is None:
            return False
        merged = _merge_with_stored(note_id, row.yjs_state, state)
        text: str | None
        try:
            text = yjs_guard.extract_text(merged)
        except yjs_guard.YjsDecodeError:
            logger.warning(
                "yjs_text_extract_failed", note_id=note_id, exc_info=True)
            text = None
        content_changed = text is not None and text != (row.content or "")
        values: dict[str, Any] = {"yjs_state": merged}
        if content_changed:
            values["content"] = text
        else:
            values["updated_at"] = ResearchNote.updated_at
        await session.execute(
            update(ResearchNote).where(ResearchNote.id == note_uuid).values(
                **values))
        await session.commit()
    if content_changed:
        schedule_note_sentiment(note_uuid)
    return True


def _active_key(note_id: str) -> str:
    return f"{_ACTIVE_KEY_PREFIX}{note_id}"


async def _mark_room_active(note_id: str) -> None:
    try:
        client = get_events_client(settings)
        key = _active_key(note_id)
        await client.zadd(
            key, {_REPLICA_ID: time.time() + _ACTIVE_TTL_SECONDS})
        await client.expire(key, _ACTIVE_TTL_SECONDS)
    except Exception:
        logger.warning(
            "yjs_active_mark_failed", note_id=note_id, exc_info=True)


async def _mark_room_inactive(note_id: str) -> None:
    try:
        await get_events_client(settings).zrem(
            _active_key(note_id), _REPLICA_ID)
    except Exception:
        logger.warning(
            "yjs_active_unmark_failed", note_id=note_id, exc_info=True)


async def is_note_room_active(note_id: UUID | str) -> bool:
    key_id = str(UUID(str(note_id)))
    if relay.has_local_room(key_id):
        return True
    try:
        client = get_events_client(settings)
        count = await client.zcount(_active_key(key_id), time.time(), "+inf")
    except Exception:
        logger.warning(
            "yjs_active_check_failed", note_id=key_id, exc_info=True)
        return False
    return int(count) > 0


async def run_active_room_heartbeat() -> None:
    while True:
        await asyncio.sleep(ACTIVE_HEARTBEAT_SECONDS)
        for note_id in relay.local_active_note_ids():
            await _mark_room_active(note_id)
        try:
            await relay.flush_due_snapshots()
        except Exception:
            logger.exception("yjs_snapshot_flush_loop_failed")
