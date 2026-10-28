import asyncio
import base64
import contextlib
import os
import select as select_module
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar
from uuid import UUID, uuid4
import structlog
import y_py as Y
from config import settings
from database import SessionLocal
from fastapi import WebSocket
from sqlalchemy import select, update
from websocket_manager import CLOSE_FORBIDDEN
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


NOTE_TEXT_NAME = "content"
_VALIDATION_WORKERS = 2
_VALIDATION_TIMEOUT_SECONDS = 5.0

T = TypeVar("T")

_WORKER_CODE = """
import struct, sys
import y_py as Y
inp, out = sys.stdin.buffer, sys.stdout.buffer
while True:
    header = inp.read(4)
    if len(header) < 4:
        break
    (size,) = struct.unpack('>I', header)
    data = inp.read(size)
    try:
        Y.apply_update(Y.YDoc(), data)
        status, reason = 0, b''
    except BaseException as exc:
        status = 1
        reason = (type(exc).__name__ + ': ' + str(exc))[:200].encode('utf-8', 'replace')
    out.write(struct.pack('>BI', status, len(reason)) + reason)
    out.flush()
"""  # noqa: E501


class YjsDecodeError(Exception):
    pass


def guarded(func: Callable[[], T], *, what: str) -> T:
    try:
        return func()
    except (KeyboardInterrupt, SystemExit, GeneratorExit,
            asyncio.CancelledError):
        raise
    except BaseException as exc:
        raise YjsDecodeError(
            f"{what}: {type(exc).__name__}: {str(exc)[:200]}") from exc


class _DecoderWorker:

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def _ensure(self) -> subprocess.Popen[bytes]:
        process = self._process
        if process is None or process.poll() is not None:
            env = dict(os.environ, RUST_BACKTRACE="0")
            process = subprocess.Popen([sys.executable, "-c", _WORKER_CODE],
                                       stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, env=env)
            self._process = process
        return process

    def kill(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        with contextlib.suppress(Exception):
            process.wait(timeout=2.0)
        for pipe in (process.stdin, process.stdout):
            if pipe is not None:
                with contextlib.suppress(Exception):
                    pipe.close()

    @staticmethod
    def _read_exact(fd: int, size: int, deadline: float) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            ready, _, _ = select_module.select([fd], [], [], remaining)
            if not ready:
                raise TimeoutError
            chunk = os.read(fd, size - len(chunks))
            if not chunk:
                raise EOFError
            chunks.extend(chunk)
        return bytes(chunks)

    def validate_blocking(self, update: bytes, timeout: float) -> str | None:
        with self._lock:
            process = self._ensure()
            assert process.stdin is not None and process.stdout is not None
            deadline = time.monotonic() + timeout
            try:
                process.stdin.write(struct.pack(">I", len(update)) + update)
                process.stdin.flush()
                fd = process.stdout.fileno()
                status, size = struct.unpack(
                    ">BI", self._read_exact(fd, 5, deadline))
                reason = self._read_exact(fd, size, deadline) if size else b""
            except TimeoutError:
                logger.warning("yjs_validator_timeout", size=len(update))
                self.kill()
                return "decoder timed out"
            except (EOFError, OSError, ValueError):
                logger.warning("yjs_validator_worker_died", size=len(update))
                self.kill()
                return "decoder process died"
            if status == 0:
                return None
            return reason.decode("utf-8", "replace") or "invalid update"


class UpdateValidator:

    def __init__(
            self, workers: int = _VALIDATION_WORKERS,
            timeout: float = _VALIDATION_TIMEOUT_SECONDS) -> None:
        self._workers = [_DecoderWorker() for _ in range(max(1, workers))]
        self._timeout = timeout
        self._next = 0

    async def validate(self, update: bytes) -> str | None:
        worker = self._workers[self._next % len(self._workers)]
        self._next += 1
        return await asyncio.to_thread(
            worker.validate_blocking, update, self._timeout)

    def shutdown(self) -> None:
        for worker in self._workers:
            worker.kill()

    async def aclose(self) -> None:
        await asyncio.to_thread(self.shutdown)


validator = UpdateValidator()


def extract_text(state: bytes) -> str:
    def _run() -> str:
        doc = Y.YDoc()
        Y.apply_update(doc, state)
        return str(doc.get_text(NOTE_TEXT_NAME))

    return guarded(_run, what="extract_text")


def seed_state(text: str) -> bytes:
    def _run() -> bytes:
        doc = Y.YDoc()
        ytext = doc.get_text(NOTE_TEXT_NAME)
        doc.transact(lambda txn: ytext.extend(txn, text))
        return bytes(Y.encode_state_as_update(doc))

    return guarded(_run, what="seed_state")


def _changed_span(old: str, new: str) -> tuple[int, int, int]:
    limit = min(len(old), len(new))
    prefix = 0
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while (suffix < limit - prefix
           and old[len(old) - 1 - suffix] == new[len(new) - 1 - suffix]):
        suffix += 1
    return prefix, len(old) - suffix, len(new) - suffix


def replace_text(state: bytes, text: str) -> bytes:
    def _run() -> bytes:
        doc = Y.YDoc()
        Y.apply_update(doc, state)
        ytext = doc.get_text(NOTE_TEXT_NAME)
        old = str(ytext)
        prefix, old_end, new_end = _changed_span(old, text)
        start = len(old[:prefix].encode("utf-8"))
        removed = len(old[prefix:old_end].encode("utf-8"))
        inserted = text[prefix:new_end]

        def _edit(txn: Y.YTransaction) -> None:
            if removed:
                ytext.delete_range(txn, start, removed)
            if inserted:
                ytext.insert(txn, start, inserted)

        doc.transact(_edit)
        return bytes(Y.encode_state_as_update(doc))

    return guarded(_run, what="replace_text")


def merge_states(*states: bytes) -> bytes:
    def _run() -> bytes:
        doc = Y.YDoc()
        for state in states:
            Y.apply_update(doc, state)
        return bytes(Y.encode_state_as_update(doc))

    return guarded(_run, what="merge_states")


class InvalidYjsUpdate(Exception):
    pass


class YjsRoom:

    def __init__(self, note_id: str, initial_state: bytes | None) -> None:
        self.note_id = note_id
        self.doc = Y.YDoc()
        if initial_state:
            try:
                guarded(
                    lambda: Y.apply_update(self.doc, initial_state),
                    what="load_state")
            except YjsDecodeError:
                logger.exception("yjs_stored_state_invalid", note_id=note_id)
        self.connections: set[WebSocket] = set()
        self._updates_since_snapshot = 0
        self._last_snapshot_at = time.monotonic()
        self._dirty = False
        self._version = 0
        self.lock = asyncio.Lock()

    def encode_state(self) -> bytes:
        return bytes(
            guarded(
                lambda: Y.encode_state_as_update(self.doc),
                what="encode_state"))

    def apply(self, update: bytes, *, counts_toward_snapshot: bool) -> None:
        guarded(
            lambda: Y.apply_update(self.doc, update), what="apply")
        if counts_toward_snapshot:
            self._updates_since_snapshot += 1
            self._version += 1
            self._dirty = True

    def should_snapshot(self) -> bool:
        return self._dirty and (
            self._updates_since_snapshot >= _SNAPSHOT_MIN_UPDATES or
            (time.monotonic() - self._last_snapshot_at)
            >= _SNAPSHOT_DEBOUNCE_SECONDS)

    def snapshot(self) -> tuple[bytes, int]:
        return self.encode_state(), self._version

    def mark_snapshotted(self, version: int | None = None) -> None:
        if version is not None and version != self._version:
            return
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
            snapshot = room.snapshot() if room._dirty else None
        if snapshot is not None:
            state, version = snapshot
            try:
                await _persist_snapshot(room.note_id, state)
            except Exception:
                logger.exception(
                    "yjs_snapshot_persist_failed_on_disconnect",
                    note_id=room.note_id)
                return
            async with room.lock:
                room.mark_snapshotted(version)
        await self._evict_if_empty(room)

    async def _evict_if_empty(self, room: YjsRoom) -> None:
        async with self._creation_lock:
            if not _in_use(room) and self._rooms.get(room.note_id) is room:
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
        reason = await validator.validate(update)
        if reason is not None:
            raise InvalidYjsUpdate(reason)
        async with room.lock:
            try:
                room.apply(update, counts_toward_snapshot=True)
            except YjsDecodeError as exc:
                raise InvalidYjsUpdate(str(exc)) from exc
            await self._broadcast_local(room, update, exclude=sender)
            snapshot = room.snapshot() if room.should_snapshot() else None
        await _publish_cross_replica(room.note_id, update)
        if snapshot is not None:
            state, version = snapshot
            try:
                persisted = await _persist_snapshot(room.note_id, state)
            except Exception:
                logger.exception(
                    "yjs_snapshot_persist_failed", note_id=room.note_id)
            else:
                if persisted:
                    async with room.lock:
                        room.mark_snapshotted(version)
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
                snapshot = room.snapshot() if due else None
            if snapshot is not None:
                state, version = snapshot
                try:
                    persisted = await _persist_snapshot(room.note_id, state)
                except Exception:
                    logger.exception(
                        "yjs_snapshot_flush_failed", note_id=room.note_id)
                    continue
                if persisted:
                    async with room.lock:
                        room.mark_snapshotted(version)
                    saved += 1
                elif idle:
                    async with room.lock:
                        room.mark_snapshotted(version)
            if idle and not room._dirty:
                await self._evict_if_empty(room)
        return saved

    async def persist_all_dirty(self) -> int:
        saved = 0
        for room in list(self._rooms.values()):
            async with room.lock:
                if not room._dirty:
                    continue
                state, version = room.snapshot()
            try:
                if await _persist_snapshot(room.note_id, state):
                    async with room.lock:
                        room.mark_snapshotted(version)
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
            except YjsDecodeError:
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
    encoded = base64.b64encode(update).decode("ascii")
    try:
        await get_events_client(settings).publish(
            f"{_REDIS_CHANNEL_PREFIX}{note_id}", f"{_REPLICA_ID}:{encoded}")
    except Exception:
        logger.warning(
            "yjs_cross_replica_publish_failed", note_id=note_id,
            exc_info=True)


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
        seeded = seed_state(row.content)
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
        return merge_states(bytes(stored), state)
    except YjsDecodeError:
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
            text = extract_text(merged)
        except YjsDecodeError:
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
