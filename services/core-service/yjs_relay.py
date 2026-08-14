import asyncio
import base64
import time
import uuid
from uuid import UUID
import structlog
import y_py as Y
from config import settings
from database import SessionLocal
from fastapi import WebSocket
from shared.redis import get_events_client
from domains.research.repository import ResearchNoteRepository

logger = structlog.get_logger(__name__)

_SNAPSHOT_MIN_UPDATES = 20
_SNAPSHOT_DEBOUNCE_SECONDS = 30
_REDIS_CHANNEL_PREFIX = "yjs:note:"
_REPLICA_ID = uuid.uuid4().hex


class YjsRoom:
    def __init__(self, note_id: str, initial_state: bytes | None) -> None:
        self.note_id = note_id
        self.doc = Y.YDoc()
        if initial_state:
            Y.apply_update(self.doc, initial_state)
        self.connections: set[WebSocket] = set()
        self._updates_since_snapshot = 0
        self._last_snapshot_at = time.monotonic()
        self._dirty = False
        self.lock = asyncio.Lock()

    def encode_state(self) -> bytes:
        return Y.encode_state_as_update(self.doc)

    def apply(self, update: bytes, *, counts_toward_snapshot: bool) -> None:
        Y.apply_update(self.doc, update)
        if counts_toward_snapshot:
            self._updates_since_snapshot += 1
            self._dirty = True

    def should_snapshot(self) -> bool:
        return self._dirty and (
            self._updates_since_snapshot >= _SNAPSHOT_MIN_UPDATES or (
                time.monotonic() - self._last_snapshot_at
                ) >= _SNAPSHOT_DEBOUNCE_SECONDS)

    def mark_snapshotted(self) -> None:
        self._updates_since_snapshot = 0
        self._last_snapshot_at = time.monotonic()
        self._dirty = False


async def _load_snapshot(note_id: str) -> bytes | None:
    async with SessionLocal() as session:
        repo = ResearchNoteRepository(session)
        note = await repo.get_by_id(UUID(note_id))
        return note.yjs_state if note else None


async def _publish_cross_replica(note_id: str, update: bytes) -> None:
    client = get_events_client(settings)
    encoded = base64.b64encode(update).decode("ascii")
    await client.publish(
        f"{_REDIS_CHANNEL_PREFIX}{note_id}", f"{_REPLICA_ID}:{encoded}")


async def _persist_snapshot(note_id: str, state: bytes) -> None:
    async with SessionLocal() as session:
        repo = ResearchNoteRepository(session)
        note = await repo.get_by_id(UUID(note_id))
        if note is not None:
            note.yjs_state = state
            await session.commit()


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

    async def connect(self, websocket: WebSocket, note_id: str) -> YjsRoom:
        room = await self.get_or_create_room(note_id)
        await websocket.accept()
        room.connections.add(websocket)
        await websocket.send_bytes(room.encode_state())
        return room

    def disconnect(self, websocket: WebSocket, room: YjsRoom) -> None:
        room.connections.discard(websocket)

    async def _broadcast_local(self, room: YjsRoom, update: bytes,
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

    async def handle_local_update(self, room: YjsRoom, update: bytes,
                                  sender: WebSocket) -> None:
        async with room.lock:
            room.apply(update, counts_toward_snapshot=True)
            await self._broadcast_local(room, update, exclude=sender)
            do_snapshot = room.should_snapshot()
            if do_snapshot:
                snapshot = room.encode_state()
                room.mark_snapshotted()
        await _publish_cross_replica(room.note_id, update)
        if do_snapshot:
            await _persist_snapshot(room.note_id, snapshot)

    async def apply_remote_update(self, note_id: str, update: bytes) -> None:
        room = self._rooms.get(note_id)
        if room is None:
            return
        async with room.lock:
            room.apply(update, counts_toward_snapshot=False)
            await self._broadcast_local(room, update, exclude=None)


relay = YjsRelay()


async def run_cross_replica_listener() -> None:
    client = get_events_client(settings)
    pubsub = client.pubsub()
    await pubsub.psubscribe(f"{_REDIS_CHANNEL_PREFIX}*")
    logger.info("yjs_cross_replica_listener_started")
    async for message in pubsub.listen():
        if message["type"] != "pmessage":
            continue
        note_id = message["channel"].removeprefix(_REDIS_CHANNEL_PREFIX)
        try:
            origin_id, _, encoded = message["data"].partition(":")
            if origin_id == _REPLICA_ID:
                continue
            update = base64.b64decode(encoded)
            await relay.apply_remote_update(note_id, update)
        except Exception:
            logger.exception("yjs_cross_replica_update_failed",
                             note_id=note_id)
