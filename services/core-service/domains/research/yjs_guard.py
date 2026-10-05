import asyncio
import contextlib
import os
import select
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import TypeVar
import structlog

logger = structlog.get_logger(__name__)

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
            ready, _, _ = select.select([fd], [], [], remaining)
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
    import y_py as Y

    def _run() -> str:
        doc = Y.YDoc()
        Y.apply_update(doc, state)
        return str(doc.get_text(NOTE_TEXT_NAME))

    return guarded(_run, what="extract_text")


def seed_state(text: str) -> bytes:
    import y_py as Y

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
    import y_py as Y

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
    import y_py as Y

    def _run() -> bytes:
        doc = Y.YDoc()
        for state in states:
            Y.apply_update(doc, state)
        return bytes(Y.encode_state_as_update(doc))

    return guarded(_run, what="merge_states")
