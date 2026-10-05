import asyncio
import contextlib
import functools
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
import pytest
import yjs_relay
from database import SessionLocal
from event_listener import (_broadcast_document_status_changed,
                            _handle_recommendation_ready,
                            _persist_document_status)
from sqlalchemy import select
from starlette.websockets import WebSocketState
from domains.notification.model import Notification
from domains.research.model import Document
from domains.research.repository import DocumentRepository
from tests.auth_tokens import access_token as _token
from tests.auth_tokens import auth_headers as _headers

ABORTING_UPDATE = bytes.fromhex("91d8cdc310411e7ec2")

SEGFAULTING_UPDATE = bytes.fromhex(
    "09621c4eb16bc03f3a84956f91430201ce26fac779b5c3ecf026ffedfae3e259")

PANICKING_UPDATE = bytes.fromhex("3f441ccc9f2f584a112a284187f32ba845a5")


class _TextSocket:

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    async def send_text(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("closed")
        self.sent.append(text)


def _envelope(event="notification", payload=None):
    from websocket_manager import WebSocketEnvelope

    return WebSocketEnvelope(event=event, payload=payload or {"n": 1})


@pytest.fixture
def presence_calls(monkeypatch):
    import clients.auth_client as auth_client

    calls: list[tuple[str, bool]] = []

    async def _set(user_id, *, online):
        calls.append((user_id, online))

    monkeypatch.setattr(auth_client, "set_user_presence", _set)
    return calls


def _analysis_ref(**kw):
    from shared.schemas import AnalysisRef

    return AnalysisRef(
        id=uuid4(), ticker="AMD", status="completed",
        created_at=datetime.now(UTC), **kw)


def _running_analysis_ref(
        analysis_id: str, owner: str, status: str = "running"):
    from shared.enums import WorkflowStatus
    from shared.schemas import AnalysisRef

    return AnalysisRef(
        id=UUID(analysis_id), ticker="AMD", status=WorkflowStatus(status),
        created_at=datetime.now(UTC), user_id=UUID(owner), portfolio_id=None)


def _text_close_code(session) -> int:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc_info:
        session.receive_text()
    return exc_info.value.code


def _bytes_close_code(session) -> int:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc_info:
        session.receive_bytes()
    return exc_info.value.code


class _FakePubSub:

    def __init__(self, fail: bool, messages: list[dict[str, Any]]):
        self._fail = fail
        self._messages = messages
        self.closed = False

    async def subscribe(self, *channels):
        if self._fail:
            raise ConnectionError("redis down")

    async def psubscribe(self, *channels):
        if self._fail:
            raise ConnectionError("redis down")

    async def listen(self):
        for message in self._messages:
            yield message
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


class _FakeClient:

    def __init__(self, pubsubs):
        self._pubsubs = pubsubs

    def pubsub(self):
        return self._pubsubs.pop(0)


@pytest.fixture
def yjs_env(monkeypatch, mock_cross_service):
    import api.yjs_websocket as yjs_ws
    import clients.auth_client as auth_client
    import yjs_relay
    from domains.research.service import NoteAccess

    access: dict[str, tuple[bool, bool]] = {}

    async def _access(note_id, identity):
        return NoteAccess(*access.get(identity.user_id, (False, False)))

    async def _none(*args, **kwargs):
        return None

    async def _persist(note_id, state):
        return True

    async def _presence(user_id, *, online):
        return None

    monkeypatch.setattr(yjs_ws, "_note_access", _access)
    monkeypatch.setattr(yjs_relay, "_load_snapshot", _none)
    monkeypatch.setattr(yjs_relay, "_persist_snapshot", _persist)
    monkeypatch.setattr(yjs_relay, "_publish_cross_replica", _none)
    monkeypatch.setattr(yjs_relay, "_mark_room_active", _none)
    monkeypatch.setattr(yjs_relay, "_mark_room_inactive", _none)
    monkeypatch.setattr(auth_client, "set_user_presence", _presence)
    return access


class _Editor:

    def __init__(self) -> None:
        import y_py as Y

        self.doc = Y.YDoc()

    def type(self, text: str) -> bytes:
        import y_py as Y

        before = Y.encode_state_vector(self.doc)
        ytext = self.doc.get_text("content")
        self.doc.transact(lambda txn: ytext.extend(txn, text))
        return bytes(Y.encode_state_as_update(self.doc, before))


def _update(text: str) -> bytes:
    return _Editor().type(text)


def _room_text(client, note_id: str) -> str:
    import yjs_relay

    room = yjs_relay.relay._rooms[note_id]
    return str(client.portal.call(lambda: str(room.doc.get_text("content"))))


@contextlib.contextmanager
def _shared_client():
    import anyio.from_thread
    import main
    from fastapi.testclient import TestClient
    from domains.research import yjs_guard

    client = TestClient(main.app)
    with anyio.from_thread.start_blocking_portal() as portal:
        client.portal = portal
        try:
            yield client
        finally:
            portal.call(yjs_guard.validator.aclose)
            client.portal = None


def _wait_room_evicted(note_id: str) -> bool:
    import time
    import yjs_relay

    deadline = time.monotonic() + 3
    while note_id in yjs_relay.relay._rooms and time.monotonic() < deadline:
        time.sleep(0.01)
    return note_id not in yjs_relay.relay._rooms


class _FakeSocket:

    def __init__(self) -> None:
        self.application_state = WebSocketState.CONNECTED
        self.closed: tuple[int, str] | None = None

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)
        self.application_state = WebSocketState.DISCONNECTED


def _session(user_id: str, *, can_write: bool):
    import api.yjs_websocket as yjs_ws
    from api.ws_common import SocketIdentity
    from domains.research.service import NoteAccess

    session = yjs_ws._YjsSession(
        websocket=_FakeSocket(),
        note_id=uuid4(),  # type: ignore[arg-type]
        identity=SocketIdentity(user_id=user_id, expires_at=4e9),
        access=NoteAccess(can_read=True, can_write=can_write))
    yjs_ws._register_session(session)
    return session


@pytest.fixture
def note_access(monkeypatch):
    import api.yjs_websocket as yjs_ws

    monkeypatch.setattr(yjs_ws, "_sessions_by_user", {})
    access: dict[UUID, tuple[bool, bool]] = {}

    async def _access(note_id, identity):
        from domains.research.service import NoteAccess

        return NoteAccess(*access.get(note_id, (True, True)))

    monkeypatch.setattr(yjs_ws, "_note_access", _access)
    return access


class _RoomSocket:

    def __init__(self, room) -> None:
        self.room = room
        self.closed: tuple[int, str] | None = None

    async def close(self, code: int, reason: str = "") -> None:
        self.closed = (code, reason)


async def _room_with_sockets(note_id: str, count: int, *, dirty: bool):
    room = yjs_relay.YjsRoom(note_id, None)
    yjs_relay.relay._rooms[note_id] = room
    sockets = [_RoomSocket(room) for _ in range(count)]
    room.connections.update(sockets)  # type: ignore[arg-type]
    room._dirty = dirty
    return room, sockets


@pytest.fixture
def _clean_rooms():
    yield
    yjs_relay.relay._rooms.clear()


class _BroadcastSocket:

    def __init__(
            self, *, send_fails: bool = False,
            close_fails: bool = False) -> None:
        self.send_fails = send_fails
        self.close_fails = close_fails
        self.sent: list[bytes] = []
        self.closed = False

    async def send_bytes(self, data: bytes) -> None:
        if self.send_fails:
            raise RuntimeError("socket gone")
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        if self.close_fails:
            raise RuntimeError("already closed")


class _ClosableTextSocket:

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.closed: list[int] = []
        self.fail = fail

    async def send_text(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("socket broken")
        self.sent.append(text)

    async def close(self, code: int = 1000) -> None:
        self.closed.append(code)


def _dirty_room(note_id):
    import y_py as Y
    import yjs_relay

    room = yjs_relay.YjsRoom(note_id, None)
    source = Y.YDoc()
    with source.begin_transaction() as txn:
        source.get_text("t").extend(txn, "hello")
    room.apply(Y.encode_state_as_update(source), counts_toward_snapshot=True)
    return room


@dataclass
class _Committed:
    ids: list[UUID]


@pytest.fixture
async def committed_outbox_rows(_schema_ready):
    from database import SessionLocal
    from outbox_model import EventOutbox
    from sqlalchemy import delete

    made = _Committed([])

    async def _make(*specs):
        async with SessionLocal() as session:
            for name, payload, age_seconds, published in specs:
                row = EventOutbox(
                    id=uuid4(), event_name=name, payload=payload,
                    created_at=datetime.now(UTC) -
                    timedelta(seconds=age_seconds), published_at=(
                        datetime.now(UTC) - timedelta(days=published)
                        if published is not None else None))
                session.add(row)
                made.ids.append(row.id)
            await session.commit()
        count = len(specs)
        return list(made.ids[-count:])

    yield _make
    async with SessionLocal() as session:
        await session.execute(
            delete(EventOutbox).where(EventOutbox.id.in_(made.ids)))
        await session.commit()


async def _rows_by_id(ids):
    from database import SessionLocal
    from outbox_model import EventOutbox
    from sqlalchemy import select

    async with SessionLocal() as session:
        rows = ((
            await session.execute(
                select(EventOutbox).where(EventOutbox.id.in_(ids))
            )).scalars().all())
        return {row.id: row for row in rows}


@pytest.fixture
def notified_user_calls(monkeypatch):
    from domains.notification import actions

    calls: list[dict[str, Any]] = []

    async def _notify_user(*, user_id, type, title, content):
        if user_id:
            calls.append({
                "user_id": str(user_id),
                "type": str(type),
                "title": title,
                "content": content
            })

    monkeypatch.setattr(actions, "notify_user", _notify_user)
    return calls


@pytest.fixture
def event_broadcasts(monkeypatch):
    import event_listener

    sent: list[tuple[str, str]] = []

    async def _broadcast(channel, envelope, **kwargs):
        sent.append((channel, envelope.event))
        return 1

    monkeypatch.setattr(event_listener.manager, "broadcast", _broadcast)
    return sent


@pytest.fixture
def no_sentiment_analysis(monkeypatch):
    import clients.ai_client as ai_client

    async def _none(text, kind):
        return None

    monkeypatch.setattr(ai_client, "analyze_sentiment", _none)


def _types(calls, user_id=None):
    return [
        c["type"] for c in calls if user_id is None or c["user_id"] == user_id
    ]


def _org_payload(**kw):
    payload = {
        "organization_id": str(uuid4()),
        "organization_name": "Acme",
        "actor_id": str(uuid4()),
        "user_id": None,
        "role": None,
        "member_ids": []
    }
    payload.update(kw)
    return payload


async def _create_document() -> UUID:
    async with SessionLocal() as session:
        doc = await DocumentRepository(session).create(
            owner_id=uuid4(), organization_id=None,
            filename=f"test-{uuid4()}.pdf", document_type="pdf",
            category="filing", ticker="AMD",
            storage_url="documents/test/test.pdf")
        await session.commit()
        return doc.id


class TestConnectionManager:

    async def test_broadcast_delivers_once_per_socket_and_across_replicas(
            self, _schema_ready, monkeypatch):
        from websocket_manager import WebSocketManager

        manager = WebSocketManager()
        tab_a, tab_b, dead = (
            _TextSocket(), _TextSocket(), _TextSocket(fail=True))
        user = f"user:{uuid4()}"
        analysis = f"analysis:{uuid4()}"
        await manager.connect(tab_a, user)
        await manager.connect(tab_b, user)
        await manager.connect(tab_a, user)
        await manager.connect(tab_a, analysis)
        await manager.connect(dead, user)

        sent = await manager.broadcast_many([user, analysis, user],
                                            _envelope())
        assert sent == 2
        assert len(tab_a.sent) == 1 and len(tab_b.sent) == 1
        assert dead not in manager._connections[user]

        await manager.broadcast(user, _envelope())
        assert len(tab_a.sent) == 2 and len(tab_b.sent) == 2

        import websocket_manager
        from config import settings
        from shared.redis import get_events_client

        manager = WebSocketManager()
        local = _TextSocket()
        channel = f"user:{uuid4()}"
        await manager.connect(local, channel)

        pubsub = get_events_client(settings).pubsub()
        await pubsub.subscribe(websocket_manager.FANOUT_CHANNEL)
        try:
            await manager.broadcast(channel, _envelope(), cross_replica=True)
            message = None
            for _ in range(50):
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=0.1)
                if message is not None:
                    break
            assert message is not None
        finally:
            await pubsub.aclose()
        raw = message["data"]
        body = json.loads(raw)
        assert body["origin"] == websocket_manager._REPLICA_ID
        assert body["channels"] == [channel]
        assert len(local.sent) == 1

        assert await manager.deliver_remote(raw) == 0
        assert len(local.sent) == 1
        other = json.dumps({**body, "origin": "other-replica"})
        second_tab = _TextSocket()
        await manager.connect(second_tab, channel)
        assert await manager.deliver_remote(other) == 2
        assert len(local.sent) == 2 and len(second_tab.sent) == 1
        assert await manager.deliver_remote("not json") == 0

        def _broken(settings):
            raise ConnectionError("redis down")

        monkeypatch.setattr(websocket_manager, "get_events_client", _broken)
        assert (
            await websocket_manager._publish_fanout(["user:x"], "text")
            is None)

    async def test_manager_cleanup_caps_and_presence(
            self, presence_calls, monkeypatch):
        from websocket_manager import (MAX_CONNECTIONS_PER_USER,
                                       ConnectionLimitExceeded,
                                       WebSocketManager)

        mgr = WebSocketManager()
        sock = object()
        await mgr.connect(sock, "user:x")  # type: ignore[arg-type]
        assert len(mgr._connections) == 1
        mgr.disconnect(sock, "user:x")  # type: ignore[arg-type]
        mgr.disconnect(sock, "user:x")  # type: ignore[arg-type]
        assert len(mgr._connections) == 0

        for _ in range(MAX_CONNECTIONS_PER_USER):
            mgr.acquire_user("u")
        with pytest.raises(ConnectionLimitExceeded):
            mgr.acquire_user("u")
        for _ in range(MAX_CONNECTIONS_PER_USER):
            mgr.release_user("u")
        await asyncio.sleep(0.05)
        assert mgr._user_counts.get("u", 0) == 0

        presence_calls.clear()
        import websocket_manager

        monkeypatch.setattr(
            websocket_manager, "PRESENCE_HEARTBEAT_SECONDS", 0.05)
        mgr = WebSocketManager()
        mgr.acquire_user("u1")
        mgr.acquire_user("u1")
        await asyncio.sleep(0.01)
        assert presence_calls == [("u1", True)]

        await asyncio.sleep(0.12)
        beats = [c for c in presence_calls if c == ("u1", True)]
        assert 2 <= len(beats) <= 5

        mgr.release_user("u1")
        await asyncio.sleep(0.01)
        assert ("u1", False) not in presence_calls

        mgr.release_user("u1")
        await asyncio.sleep(0.02)
        assert presence_calls[-1] == ("u1", False)
        assert "u1" not in mgr._presence_tasks

        presence_calls.clear()
        mgr.acquire_user("u1")
        await asyncio.sleep(0.01)
        assert presence_calls == [("u1", True)]
        mgr.release_user("u1")
        await asyncio.sleep(0.02)
        assert presence_calls[-1] == ("u1", False)


class TestWebSocketEndpoints:

    async def test_channel_authorization_and_close_codes(
            self, mock_cross_service, monkeypatch, presence_calls):
        import api.websocket as ws
        from database import SessionLocal
        from domains.research.repository import DocumentRepository

        owner = uuid4()
        async with SessionLocal() as session:
            doc = await DocumentRepository(session).create(
                owner_id=owner, organization_id=None,
                filename=f"ws-{uuid4()}.txt", document_type="txt",
                category="news", ticker="AMD", storage_url="documents/x.txt")
            await session.commit()
        assert (await ws.authorize_channel(f"document:{doc.id}",
                                           str(owner)))[0]
        assert not (
            await ws.authorize_channel(f"document:{doc.id}", str(uuid4())))[0]

        import clients.ai_client as ai_client
        import clients.auth_client as auth_client
        from shared.exceptions import NotFoundError, UpstreamServiceError

        me = str(uuid4())
        friend = str(uuid4())
        stranger = str(uuid4())
        readable_portfolio = uuid4()
        refs = {
            "mine": _analysis_ref(user_id=UUID(me)),
            "theirs": _analysis_ref(user_id=UUID(stranger)),
            "system_ok": _analysis_ref(
                user_id=None, portfolio_id=readable_portfolio),
            "system_no": _analysis_ref(user_id=None, portfolio_id=uuid4()),
            "system_none": _analysis_ref(user_id=None, portfolio_id=None)
        }
        ids = {name: str(uuid4()) for name in refs}
        down = str(uuid4())

        async def _get_ref(analysis_id):
            if analysis_id == down:
                raise UpstreamServiceError("ai-service down")
            for name, aid in ids.items():
                if aid == analysis_id:
                    return refs[name]
            raise UpstreamServiceError("404")

        async def _connected(a, b):
            return {a, b} == {me, friend}

        async def _portfolio_get(self, portfolio_id, requester_id):
            if portfolio_id != readable_portfolio:
                raise NotFoundError("Portfolio not found.")
            return None

        readable_doc = uuid4()

        async def _get_document(self, document_id, requester_id):
            if document_id != readable_doc:
                raise NotFoundError("Document not found.")
            return None

        monkeypatch.setattr(ai_client, "get_analysis_ref", _get_ref)
        monkeypatch.setattr(auth_client, "are_users_connected", _connected)
        monkeypatch.setattr(ws.PortfolioService, "get", _portfolio_get)
        monkeypatch.setattr(ws.ResearchService, "get_document", _get_document)

        cases = [(f"user:{me}", True, None), (f"user:{stranger}", False, 4403),
                 (f"presence:{me}", True, None),
                 (f"presence:{friend}", True, None),
                 (f"presence:{stranger}", False, 4403),
                 (f"analysis:{ids['mine']}", True, None),
                 (f"analysis:{ids['theirs']}", False, 4403),
                 (f"analysis:{ids['system_ok']}", True, None),
                 (f"analysis:{ids['system_no']}", False, 4403),
                 (f"analysis:{ids['system_none']}", False, 4403),
                 (f"analysis:{down}", False, 4403),
                 ("analysis:not-a-uuid", False, 4403),
                 (f"document:{readable_doc}", True, None),
                 (f"document:{uuid4()}", False, 4403),
                 (f"portfolio:{uuid4()}", False, 4400), ("user", False, 4400),
                 ("user:", False, 4400)]
        for channel, expected, code in cases:
            allowed, close_code = await ws.authorize_channel(channel, me)
            assert allowed is expected, channel
            if not expected:
                assert close_code == code, channel

        import main
        from fastapi.testclient import TestClient

        client = TestClient(main.app)
        me, token = _token()
        with client.websocket_connect("/ws/user:" + me + "?token=bad") as s:
            assert _text_close_code(s) == 4401
        with client.websocket_connect(
                f"/ws/user:{uuid4()}?token={token}") as s:
            assert _text_close_code(s) == 4403
        with client.websocket_connect(f"/ws/bogus:{me}?token={token}") as s:
            assert _text_close_code(s) == 4400
        with client.websocket_connect(
                f"/ws/yjs/not-a-uuid?token={token}") as s:
            assert _text_close_code(s) == 4401

        from websocket_manager import manager

        me, token = _token()
        client = TestClient(main.app)
        with client.websocket_connect(f"/ws/user:{me}?token={token}") as s:
            assert manager._user_counts.get(me, 0) == 1
            s.send_bytes(b"\x00\x01")
            assert _text_close_code(s) == 1003
        assert manager._user_counts.get(me, 0) == 0
        assert f"user:{me}" not in manager._connections

        me, token = _token(ttl=timedelta(seconds=2))
        client = TestClient(main.app)
        with client.websocket_connect(f"/ws/user:{me}?token={token}") as s:
            assert _text_close_code(s) == 4401
        assert manager._user_counts.get(me, 0) == 0

    async def test_deactivated_user_refused_and_live_role_used(
            self, yjs_env, monkeypatch):
        import api.ws_common as ws_common
        import main
        from fastapi.testclient import TestClient
        from shared.user_state import UserState

        user, token = _token()
        yjs_env[user] = (True, True)

        async def _state(settings, user_id):
            return UserState(role="user", is_active=False)

        monkeypatch.setattr(ws_common, "get_user_state", _state)
        monkeypatch.setattr(
            ws_common.settings, "USER_STATE_CHECK_ENABLED", True)
        client = TestClient(main.app)
        with client.websocket_connect(f"/ws/yjs/{uuid4()}?token={token}") as s:
            assert _bytes_close_code(s) == 4401
        with client.websocket_connect(f"/ws/user:{user}?token={token}") as s:
            assert _bytes_close_code(s) == 4401

        async def _state(settings, user_id):
            return UserState(role="guest", is_active=True)

        monkeypatch.setattr(ws_common, "get_user_state", _state)
        monkeypatch.setattr(
            ws_common.settings, "USER_STATE_CHECK_ENABLED", True)
        identity = ws_common.SocketIdentity(
            user_id="u", expires_at=1.0, role="user")
        live = await ws_common.resolve_live_identity(identity)
        assert live is not None and live.role == "guest"

    async def test_analysis_socket_status_messages(
            self, monkeypatch, mock_cross_service):
        import clients.ai_client as ai_client
        import main
        from fastapi.testclient import TestClient

        owner, analysis_id = str(uuid4()), str(uuid4())

        async def _get_ref(requested: str):
            return _running_analysis_ref(requested, owner)

        monkeypatch.setattr(ai_client, "get_analysis_ref", _get_ref)
        client = TestClient(main.app)
        with client.websocket_connect(
                f"/ws/analysis:{analysis_id}?token={_token(owner)[1]}") as s:
            first = json.loads(s.receive_text())
        assert first["event"] == "analysis.status"
        assert first["payload"] == {
            "analysis_id": analysis_id,
            "ticker": "AMD",
            "status": "running",
            "portfolio_id": None
        }

        import api.websocket as ws

        calls = []

        async def _down(requested: str):
            calls.append(requested)
            raise RuntimeError("ai-service down")

        class _Socket:

            def __init__(self) -> None:
                self.sent: list[str] = []

            async def send_text(self, text: str) -> None:
                self.sent.append(text)

        monkeypatch.setattr(ai_client, "get_analysis_ref", _down)
        sock = _Socket()
        await ws.send_analysis_status(sock, "a1")
        assert calls == ["a1"] and sock.sent == []

        sent = []

        async def _spy(websocket, analysis_id):
            sent.append(analysis_id)

        monkeypatch.setattr(ws, "send_analysis_status", _spy)
        user = str(uuid4())
        client = TestClient(main.app)
        with client.websocket_connect(
                f"/ws/user:{user}?token={_token(user)[1]}"):
            pass
        assert sent == []

    async def test_analysis_broadcasts_are_logged_with_socket_count(
            self, monkeypatch):
        for handler_name, event, payload in (("_handle_analysis_completed",
                                              "analysis.completed",
                                              {"analysis_id": "a1",
                                               "user_id": ""}),
                                             ("_handle_recommendation_ready",
                                              "recommendation.ready",
                                              {"analysis_id": "a1",
                                               "user_id": None})):
            import event_listener
            from websocket_manager import manager

            infos: list[tuple[str, dict]] = []
            monkeypatch.setattr(
                event_listener.logger, "info", lambda name, **kw: infos.append(
                    (name, kw)))
            sock = _ClosableTextSocket()
            await manager.connect(sock, "analysis:a1")
            try:
                await getattr(event_listener, handler_name)(payload)
            finally:
                manager.disconnect(sock, "analysis:a1")
            assert len(sock.sent) == 1, handler_name
            assert (
                "websocket_analysis_broadcast", {
                    "event_name": event,
                    "analysis_id": "a1",
                    "sockets": 1
                }) in infos, handler_name


class TestNotificationService:

    async def test_mark_read_rules_and_retention(
            self, client, db_session, mock_cross_service, monkeypatch):
        from domains.notification.service import NotificationService
        from shared.enums import NotificationType

        user_id, headers = _headers()

        created = await NotificationService(db_session).create_notification(
            user_id=user_id, type=NotificationType.ACCOUNT_UPDATED,
            title="Welcome", content="Hello there.")
        await db_session.flush()

        listed = await client.get("/api/v1/notifications", headers=headers)
        assert listed.status_code == 200
        items = listed.json()["items"]
        assert len(items) == 1
        assert items[0]["title"] == "Welcome"
        assert items[0]["read_at"] is None

        mark_read = await client.post(
            f"/api/v1/notifications/{created.id}/read", headers=headers)
        assert mark_read.status_code == 200
        assert mark_read.json()["data"]["read_at"] is not None

        _, headers = _headers()
        too_many = [str(uuid4()) for _ in range(501)]
        response = await client.post(
            "/api/v1/notifications/read", json={"notification_ids": too_many},
            headers=headers)
        assert response.status_code == 422
        ok = await client.post(
            "/api/v1/notifications/read",
            json={"notification_ids": too_many[:500]}, headers=headers)
        assert ok.status_code == 200

        from domains.notification.repository import NotificationRepository

        repo = NotificationRepository(db_session)
        user = uuid4()
        a = await repo.create(
            user_id=user, type="account_updated", title="a", content=None)
        b = await repo.create(
            user_id=user, type="account_updated", title="b", content=None)
        assert await repo.mark_read_bulk([a.id], user) == 1
        await db_session.refresh(a)
        first = a.read_at
        assert await repo.mark_read_bulk([a.id, b.id], user) == 1
        await db_session.refresh(a)
        assert a.read_at == first

        from config import settings

        user_id, headers = _headers()
        now = datetime.now(UTC)

        async def _row(title, *, age_days, read):
            row = Notification(
                user_id=UUID(user_id), type="account_updated", title=title,
                created_at=now - timedelta(days=age_days),
                read_at=now if read else None)
            db_session.add(row)
            await db_session.flush()
            return row.id

        old_read = [
            await _row(f"old{i}", age_days=91, read=True) for i in "ab"
        ]
        old_unread = await _row("old-unread", age_days=400, read=False)
        young_read = await _row("young", age_days=10, read=True)
        await db_session.commit()

        async def _ids():
            rows = await db_session.execute(
                select(Notification.id).where(
                    Notification.user_id == UUID(user_id)))
            return set(rows.scalars().all())

        monkeypatch.setattr(settings, "NOTIFICATION_PURGE_BATCH_SIZE", 1)
        url = "/internal/scheduler/notification-retention"
        r = await client.post(url)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["retention_days"] == 90
        assert body["deleted"] >= 2 and body["failed_batches"] == 0
        assert body["batches"] == body["deleted"] + 1
        assert await _ids() == {old_unread, young_read}
        assert not set(old_read) & await _ids()

        monkeypatch.setattr(settings, "NOTIFICATION_READ_RETENTION_DAYS", 5)
        monkeypatch.setattr(settings, "NOTIFICATION_PURGE_BATCH_SIZE", 1000)
        r = await client.post(url)
        assert r.status_code == 200 and r.json()["deleted"] >= 1
        assert await _ids() == {old_unread}

        listed = await client.get(
            "/api/v1/notifications", headers={
                **headers, "X-Request-ID": "core-trace-1"
            })
        assert listed.headers["x-request-id"] == "core-trace-1"
        read = await client.post(
            f"/api/v1/notifications/{old_unread}/read", headers={
                **headers, "X-Request-ID": "core-trace-2"
            })
        assert read.json()["request_id"] == "core-trace-2"
        await db_session.commit()

        async def _broken(self, cutoff, limit):
            raise RuntimeError("database gone")

        monkeypatch.setattr(
            NotificationRepository, "delete_read_before", _broken)
        r = await client.post(url)
        assert r.status_code == 500
        assert r.json()["all_units_failed"] == ["batches"]

    async def test_push_delivery_in_app_switch_and_commit_order(
            self, monkeypatch, mock_cross_service):
        import clients.auth_client as auth_client
        from websocket_manager import manager
        import domains.notification.service as notification_service
        from domains.notification.schema import NotificationRead

        published: list[list[str]] = []

        async def _publish(channels, text):
            published.append(channels)

        async def _settings(user_id):
            return {"email": None, "notification_preferences": None}

        import websocket_manager

        monkeypatch.setattr(websocket_manager, "_publish_fanout", _publish)
        monkeypatch.setattr(
            auth_client, "get_notification_settings", _settings)
        user_id = uuid4()
        tabs = [_TextSocket(), _TextSocket(), _TextSocket()]
        for tab in tabs:
            await manager.connect(tab, f"user:{user_id}")
        try:
            note = NotificationRead(
                id=uuid4(), user_id=user_id, type="account_updated", title="t",
                content=None, created_at=datetime.now(UTC))
            service_class = notification_service.NotificationService
            await service_class.deliver_notification(note)
        finally:
            for tab in tabs:
                manager.disconnect(tab, f"user:{user_id}")
        assert [len(t.sent) for t in tabs] == [1, 1, 1]
        assert published == [[f"user:{user_id}"]]

        from database import SessionLocal
        from sqlalchemy import func, select
        from domains.notification import actions
        from domains.notification.model import Notification

        preferences: dict = {"in_app": False, "email": True, "websocket": True}
        pushed: list[str] = []
        emailed: list[str] = []

        async def _settings(user_id):
            return {
                "email": "a@example.com",
                "notification_preferences": preferences
            }

        async def _broadcast(channel, envelope, **kwargs):
            pushed.append(channel)

        async def _email(**kwargs):
            emailed.append(kwargs["to"])

        monkeypatch.setattr(
            auth_client, "get_notification_settings", _settings)
        monkeypatch.setattr(
            notification_service.manager, "broadcast", _broadcast)
        monkeypatch.setattr(
            notification_service, "send_notification_email", _email)

        async def _rows(user_id) -> int:
            async with SessionLocal() as session:
                return int((
                    await session.execute(
                        select(func.count()).select_from(Notification).where(
                            Notification.user_id == user_id))).scalar_one())

        user_id = uuid4()
        await actions.notify_user(
            user_id=user_id, type="account_updated", title="t", content="c")
        assert await _rows(user_id) == 0
        assert pushed == []
        assert emailed == ["a@example.com"]

        preferences["in_app"] = True
        await actions.notify_user(
            user_id=user_id, type="account_updated", title="t", content="c")
        assert await _rows(user_id) == 1
        assert pushed == [f"user:{user_id}"]
        assert emailed == ["a@example.com", "a@example.com"]

        import event_listener
        from domains.notification.service import NotificationService

        seen: list[bool] = []

        async def _deliver(notification, preferences=None):
            async with SessionLocal() as session:
                row = await session.get(Notification, notification.id)
                seen.append(row is not None)

        async def _broadcast(channel, envelope, **kwargs):
            return None

        async def _settings(user_id):
            return {"email": None, "notification_preferences": None}

        monkeypatch.setattr(
            NotificationService, "deliver_notification",
            staticmethod(_deliver))
        monkeypatch.setattr(
            auth_client, "get_notification_settings", _settings)
        monkeypatch.setattr(event_listener.manager, "broadcast", _broadcast)
        await event_listener._handle_analysis_completed({
            "analysis_id": str(uuid4()),
            "user_id": str(uuid4())
        })
        assert seen == [True]


@pytest.mark.usefixtures("no_sentiment_analysis")
class TestNotificationEvents:

    async def test_note_and_comment_actions_notify_actor_and_author(
            self, client, mock_cross_service, notified_user_calls,
            monkeypatch):
        import clients.auth_client as auth_client

        org_id = uuid4()
        author, author_h = _headers()
        member, member_h = _headers()

        async def _ids(user_id):
            return [org_id]

        async def _roles(user_id):
            return {org_id: "member"}

        monkeypatch.setattr(auth_client, "list_user_organization_ids", _ids)
        monkeypatch.setattr(
            auth_client, "list_user_organization_roles", _roles)

        note = (
            await client.post(
                "/api/v1/research/notes", headers=author_h, json={
                    "title": "Team thesis",
                    "organization_id": str(org_id)
                })).json()["data"]
        await client.patch(
            f"/api/v1/research/notes/{note['id']}", headers=member_h,
            json={"content": "member edit"})
        comments = f"/api/v1/research/notes/{note['id']}/comments"
        comment = (
            await
            client.post(comments, headers=member_h,
                        json={"content": "c"})).json()["data"]
        await client.patch(
            f"{comments}/{comment['id']}", headers=member_h,
            json={"content": "c2"})
        await client.delete(f"{comments}/{comment['id']}", headers=author_h)
        await client.delete(
            f"/api/v1/research/notes/{note['id']}", headers=author_h)

        assert _types(notified_user_calls, author) == [
            "note_created", "note_updated", "comment_created",
            "comment_deleted", "note_deleted"
        ]
        assert _types(notified_user_calls, member) == [
            "note_updated", "comment_created", "comment_updated",
            "comment_deleted"
        ]
        assert all("Team thesis" in c["content"] for c in notified_user_calls)

    async def test_document_organization_friend_and_user_events(
            self, notified_user_calls, _schema_ready, event_broadcasts):
        import event_listener
        from database import SessionLocal
        from domains.research.model import Document
        from shared.schemas import SYSTEM_DOCUMENT_OWNER_ID

        owner = uuid4()
        docs = [
            Document(
                owner_id=owner, filename="a.txt", document_type="txt",
                category="news", storage_url=f"k/{uuid4()}", title="Alpha"),
            Document(
                owner_id=owner, filename="b.txt", document_type="txt",
                category="news", storage_url=f"k/{uuid4()}"),
            Document(
                owner_id=SYSTEM_DOCUMENT_OWNER_ID, filename="s.txt",
                document_type="txt", category="news",
                storage_url=f"k/{uuid4()}")
        ]
        async with SessionLocal() as session:
            session.add_all(docs)
            await session.commit()
        try:
            for status in ("parsing", "ready", "ready"):
                await event_listener._persist_document_status({
                    "document_id": str(docs[0].id),
                    "status": status
                })
            await event_listener._persist_document_status({
                "document_id": str(docs[1].id),
                "status": "failed"
            })
            await event_listener._persist_document_status({
                "document_id": str(docs[2].id),
                "status": "ready"
            })
        finally:
            async with SessionLocal() as session:
                for doc in docs:
                    row = await session.get(Document, doc.id)
                    await session.delete(row)
                await session.commit()
        assert [(c["user_id"], c["type"]) for c in notified_user_calls
                ] == [(str(owner), "document_ready"),
                      (str(owner), "document_failed")]
        assert (
            '"Alpha"' in notified_user_calls[0]["content"]
            and '"b.txt"' in notified_user_calls[1]["content"])

        notified_user_calls.clear()

        user = str(uuid4())
        await event_listener._handle_organization_member_joined({
            "user_id": user,
            "organization_id": str(uuid4())
        })
        await event_listener._handle_organization_member_joined(
            {"organization_id": "x"})
        assert _types(notified_user_calls) == ["organization_member_joined"]

        a, b, joiner = (str(uuid4()) for _ in range(3))
        await event_listener._handle_organization_created(
            _org_payload(member_ids=[a]))
        await event_listener._handle_organization_updated(
            _org_payload(member_ids=[a, b, a]))
        await event_listener._handle_organization_member_joined(
            _org_payload(user_id=joiner, member_ids=[a, b, joiner]))
        await event_listener._handle_organization_role_changed(
            _org_payload(user_id=b, role="viewer", member_ids=[a, b]))
        await event_listener._handle_organization_member_removed(
            _org_payload(user_id=joiner, member_ids=[a, b]))
        await event_listener._handle_organization_member_left(
            _org_payload(user_id=b, member_ids=[a]))
        await event_listener._handle_organization_deleted(
            _org_payload(member_ids=[a, b]))
        await event_listener._handle_organization_invitation_created(
            _org_payload(user_id=joiner))
        await event_listener._handle_organization_invitation_created(
            _org_payload())

        assert _types(notified_user_calls, a) == [
            "organization_created", "organization_updated",
            "organization_member_joined", "organization_role_changed",
            "organization_member_removed", "organization_member_left",
            "organization_deleted"
        ]
        assert _types(notified_user_calls, joiner) == [
            "organization_member_joined", "organization_member_removed",
            "organization_invite"
        ]
        joined = next(c for c in notified_user_calls if c["user_id"] == joiner)
        assert joined["title"] == "Welcome to the organization"
        role_for_b = [
            c for c in notified_user_calls
            if c["user_id"] == b and c["type"] == "organization_role_changed"
        ]
        assert role_for_b[0]["content"] == 'Your role in "Acme" is now viewer.'

        notified_user_calls.clear()

        actor, other, user = (str(uuid4()) for _ in range(3))
        friend = {
            "actor_id": actor,
            "user_id": other,
            "actor_username": "alice"
        }
        await event_listener._handle_friend_requested(dict(friend))
        await event_listener._handle_friend_accepted(dict(friend))
        await event_listener._handle_friend_removed(dict(friend))
        base = {
            "user_id": user,
            "actor_id": actor,
            "role": "moderator",
            "is_active": True,
            "changed": ["display_name"]
        }
        await event_listener._handle_user_updated(dict(base))
        await event_listener._handle_user_role_changed({
            **base, "changed": ["role"]
        })
        await event_listener._handle_user_deactivated({
            **base, "is_active": False
        })
        await event_listener._handle_user_reactivated({
            **base, "changed": ["is_active"]
        })
        await event_listener._handle_user_deleted({
            **base, "changed": ["deleted"]
        })

        assert _types(notified_user_calls, other) == [
            "friend_request", "friend_accepted", "friend_removed"
        ]
        assert (
            notified_user_calls[0]["content"] ==
            "alice sent you a friend request.")
        assert _types(notified_user_calls, actor) == []
        assert _types(notified_user_calls, user) == [
            "account_updated", "account_role_changed", "account_updated",
            "account_updated"
        ]
        assert (
            "moderator" in next(
                c for c in notified_user_calls
                if c["type"] == "account_role_changed")["content"])

    async def test_ai_and_tier_events_notify_and_broadcast(
            self, notified_user_calls, event_broadcasts, _schema_ready):
        import event_listener

        user = str(uuid4())
        analysis = str(uuid4())
        common = {
            "analysis_id": analysis,
            "user_id": user,
            "ticker": "AMD",
            "portfolio_id": None
        }
        await event_listener._handle_analysis_started(dict(common))
        await event_listener._handle_analysis_failed({
            **common, "failure_reason": "no data"
        })
        await event_listener._handle_analysis_started({
            **common, "user_id": ""
        })
        await event_listener._handle_decision_recorded({
            "decision_id": str(uuid4()),
            "analysis_id": analysis,
            "user_id": user,
            "ticker": "AMD",
            "decision": "followed"
        })
        await event_listener._handle_backtest_completed({
            "backtest_id": "b1",
            "user_id": user,
            "kind": "portfolio",
            "status": "completed",
            "error": None
        })
        await event_listener._handle_backtest_failed({
            "backtest_id": "b2",
            "user_id": user,
            "kind": "ticker",
            "status": "failed",
            "error": "no prices"
        })

        assert _types(notified_user_calls, user) == [
            "analysis_started", "analysis_failed", "decision_recorded",
            "backtest_completed", "backtest_failed"
        ]
        assert "no data" in notified_user_calls[1]["content"]
        channel = f"analysis:{analysis}"
        assert event_broadcasts == [(channel, "analysis.started"),
                                    (channel, "analysis.failed"),
                                    (channel, "analysis.started"),
                                    (channel, "decision.recorded")]

        notified_user_calls.clear()
        event_broadcasts.clear()
        from database import SessionLocal
        from domains.portfolio.model import Portfolio

        owner = uuid4()
        portfolio = Portfolio(owner_id=owner, name="Auto")
        async with SessionLocal() as session:
            session.add(portfolio)
            await session.commit()
        analysis = str(uuid4())
        tier = {
            "tier_id": str(uuid4()),
            "analysis_id": analysis,
            "user_id": "",
            "portfolio_id": str(portfolio.id),
            "ticker": "AMD",
            "action": "buy",
            "quantity": "5",
            "price": "101.5",
            "reason": None
        }
        try:
            await event_listener._handle_tier_executed(dict(tier))
            await event_listener._handle_tier_execution_failed({
                **tier, "tier_id": str(uuid4()),
                "reason": "market closed"
            })
            explicit = str(uuid4())
            await event_listener._handle_tier_cancelled({
                **tier, "tier_id": str(uuid4()),
                "user_id": explicit,
                "reason": "portfolio deleted"
            })
            await event_listener._handle_tier_executed({
                **tier, "tier_id": str(uuid4()),
                "portfolio_id": str(uuid4())
            })
        finally:
            async with SessionLocal() as session:
                await session.delete(
                    await session.get(Portfolio, portfolio.id))
                await session.commit()

        assert [(c["user_id"], c["type"]) for c in notified_user_calls
                ] == [(str(owner), "tier_executed"),
                      (str(owner), "tier_execution_failed"),
                      (explicit, "tier_cancelled")]
        assert (
            notified_user_calls[0]["content"] ==
            "A planned tier was executed: buy 5 AMD at 101.5.")
        assert "market closed" in notified_user_calls[1]["content"]
        assert [b[1] for b in event_broadcasts] == [
            "tier.executed", "tier.execution_failed", "tier.cancelled",
            "tier.executed"
        ]

    async def test_event_handlers_exist_and_claims_dedupe_replicas(
            self, notified_user_calls, _schema_ready, monkeypatch):
        import event_listener
        from shared.events_registry import EVENT_OWNERS, EventName

        handled = set(event_listener._PUBSUB_HANDLERS)
        for event, owner in EVENT_OWNERS.items():
            if owner in ("auth-service", "ai-service"):
                assert event in handled, event
        assert set(EVENT_OWNERS.values()) == {
            "auth-service", "ai-service", "core-service"
        }
        assert all(
            event in (
                EventName.DOCUMENT_UPLOADED, EventName.DOCUMENT_DELETED,
                EventName.PORTFOLIO_DELETED)
            for event, owner in EVENT_OWNERS.items()
            if owner == "core-service")

        payload = {
            "actor_id": str(uuid4()),
            "user_id": str(uuid4()),
            "actor_username": "bob"
        }
        await event_listener._handle_friend_requested(dict(payload))
        await event_listener._handle_friend_requested(dict(payload))
        assert len(notified_user_calls) == 1

        def _broken(settings):
            raise ConnectionError("redis down")

        monkeypatch.setattr(event_listener, "get_events_client", _broken)
        assert await event_listener._claim_event("x", {"a": 1})

    async def test_decision_outcome_evaluated_notifies_the_user(
            self, notified_user_calls, monkeypatch):
        import event_listener

        async def _claim(event, payload):
            return True

        monkeypatch.setattr(event_listener, "_claim_event", _claim)
        user = str(uuid4())
        base = {
            "decision_id": str(uuid4()),
            "analysis_id": str(uuid4()),
            "user_id": user,
            "ticker": "AMD",
            "evaluation_days": 30
        }
        await event_listener._handle_decision_outcome_evaluated({
            **base, "alpha": 0.032,
            "outperformed": True
        })
        await event_listener._handle_decision_outcome_evaluated({
            **base, "alpha": -0.01,
            "outperformed": False
        })
        await event_listener._handle_decision_outcome_evaluated({
            **base, "alpha": None,
            "outperformed": None
        })
        await event_listener._handle_decision_outcome_evaluated({
            **base, "user_id": ""
        })
        assert [c["user_id"] for c in notified_user_calls] == [user] * 3
        assert {c["type"]
                for c in notified_user_calls} == {"decision_evaluated"}
        assert (
            "outperformed the benchmark (+3.2 percentage points)"
            in notified_user_calls[0]["content"])
        assert (
            "did not outperform the benchmark (-1.0"
            in notified_user_calls[1]["content"])
        assert "no price data" in notified_user_calls[2]["content"]
        assert "after 30 days" in notified_user_calls[0]["content"]


class TestEventListener:

    async def test_document_status_persistence_and_recommendation_notice(
            self, mock_cross_service, monkeypatch):
        for payload in ({"document_id": str(uuid4()), "status": "ready"},
                        {"document_id": "not-a-uuid",
                         "status": "ready"}, {"status": "ready"}):
            await _persist_document_status(payload)

        import event_listener as event_listener_module

        async def _fake_broadcast(channel, envelope, **kwargs):
            pass

        monkeypatch.setattr(
            event_listener_module.manager, "broadcast", _fake_broadcast)

        document_id = await _create_document()
        await _broadcast_document_status_changed({
            "document_id": str(document_id),
            "status": "ready"
        })

        async with SessionLocal() as session:
            document = (
                await session.scalars(
                    select(Document).where(Document.id == document_id))).one()
            assert document.status == "uploaded"

        async with SessionLocal() as session:
            doc = await DocumentRepository(session).create(
                owner_id=uuid4(), organization_id=None,
                filename=f"st-{uuid4()}.txt", document_type="txt",
                category="news", ticker="AMD", storage_url="documents/st.txt")
            await session.commit()

        for status in ("parsing", "ready", "parsing", "failed"):
            await _persist_document_status({
                "document_id": str(doc.id),
                "status": status
            })
        async with SessionLocal() as session:
            stmt = select(Document).where(Document.id == doc.id)
            row = (await session.scalars(stmt)).one()
            assert row.status == "ready"

        async def _fake_broadcast(channel, envelope, **kwargs):
            pass

        monkeypatch.setattr(
            event_listener_module.manager, "broadcast", _fake_broadcast)

        real_user_id = str(uuid4())
        analysis_id = str(uuid4())
        await _handle_recommendation_ready({
            "analysis_id": analysis_id,
            "ticker": "MSFT",
            "user_id": real_user_id
        })

        async with SessionLocal() as session:
            notification = (
                await session.scalars(
                    select(Notification).where(
                        Notification.user_id == UUID(real_user_id)))).one()
            assert notification.title == "A new recommendation is ready"


class TestCrossServiceEvents:

    async def test_subscriber_reconnects_unique_ids_and_publish_failure(
            self, monkeypatch):
        import shared.events as events

        received: list[dict[str, Any]] = []

        async def _handler(payload):
            received.append(payload)

        first = _FakePubSub(fail=True, messages=[])
        second = _FakePubSub(
            fail=False, messages=[{
                "type": "message",
                "channel": "events:x.y",
                "data": '{"a": 1}'
            }])
        client = _FakeClient([first, second])
        monkeypatch.setattr(
            events, "get_events_client", lambda settings: client)
        monkeypatch.setattr(events, "RECONNECT_BACKOFF_SECONDS", (0.01, ))

        sub = events.CrossServiceSubscriber(
            settings=None  # type: ignore[arg-type]
        )
        sub.on("x.y", _handler)
        task = asyncio.create_task(sub.run())
        for _ in range(100):
            if received:
                break
            await asyncio.sleep(0.01)
        assert received == [{"a": 1}]
        assert first.closed
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert second.closed

        import event_listener

        keys = set()

        class _Redis:

            async def set(self, key, value, nx, ex):
                if key in keys:
                    return None
                keys.add(key)
                return True

        monkeypatch.setattr(
            event_listener, "get_events_client", lambda _s: _Redis())
        body = {"analysis_id": "a1", "user_id": "u1"}
        assert await event_listener._claim_event(
            "x", {
                **body, "event_id": "e1"
            })
        assert await event_listener._claim_event(
            "x", {
                **body, "event_id": "e2"
            })
        assert not await event_listener._claim_event(
            "x", {
                **body, "event_id": "e2"
            })
        assert await event_listener._claim_event("x", body)
        assert not await event_listener._claim_event("x", dict(body))

        import json

        sent = []

        class _PublishRedis:

            async def publish(self, channel, message):
                sent.append(json.loads(message))

        monkeypatch.setattr(
            events, "get_events_client", lambda _s: _PublishRedis())
        from config import settings

        await events.publish_cross_service(settings, "e", {"a": 1})
        await events.publish_cross_service(settings, "e", {"a": 1})
        assert sent[0]["a"] == 1
        assert (
            sent[0]["event_id"] and sent[0]["event_id"] != sent[1]["event_id"])

        import outbox

        async def _down(*args, **kwargs):
            raise ConnectionError("redis down")

        monkeypatch.setattr(outbox, "publish_durable_event", _down)
        marked: list = []

        async def _mark(ids):
            marked.extend(ids)

        monkeypatch.setattr(outbox, "_mark_published", _mark)
        entry = outbox.OutboxEntry(
            uuid4(), "document.deleted", {"document_id": "x"})
        await outbox.publish_now([entry])
        assert marked == []

    async def test_dispatcher_and_purge_of_outbox_rows(
            self, committed_outbox_rows, published_events, monkeypatch):
        import outbox

        old, fresh = await committed_outbox_rows(
            ("portfolio.deleted", {
                "portfolio_id": "a"
            }, 60, None),
            ("portfolio.deleted", {
                "portfolio_id": "b"
            }, 0, None))
        await outbox.dispatch_pending()
        rows = await _rows_by_id([old, fresh])
        assert rows[old].published_at is not None and rows[old].attempts == 1
        assert rows[fresh].published_at is None
        assert ("portfolio.deleted", {"portfolio_id": "a"}) in published_events

        first, second = await committed_outbox_rows(
            ("document.deleted", {
                "document_id": "first"
            }, 120, None),
            ("document.deleted", {
                "document_id": "second"
            }, 60, None))
        calls: list[str] = []

        async def _down(settings, event_name, payload, **kwargs):
            calls.append(payload["document_id"])
            raise ConnectionError("redis down")

        monkeypatch.setattr(outbox, "publish_durable_event", _down)
        assert await outbox.dispatch_pending() == 0
        rows = await _rows_by_id([first, second])
        assert calls[-1] == "first" and "second" not in calls
        assert (
            rows[first].attempts == 1
            and "redis down" in rows[first].last_error)
        assert rows[first].published_at is None and rows[second].attempts == 0

        old, recent, pending = await committed_outbox_rows(
            ("document.deleted", {
                "document_id": "old"
            }, 999999, 8),
            ("document.deleted", {
                "document_id": "recent"
            }, 999999, 1),
            ("document.deleted", {
                "document_id": "pending"
            }, 999999, None))
        await outbox.purge_published()
        assert set(await _rows_by_id([old, recent,
                                      pending])) == {recent, pending}


class TestYjsCollaboration:

    async def test_malformed_updates_are_rejected_and_close_the_sender(
            self, monkeypatch, mock_cross_service, presence_calls, yjs_env):
        from domains.research import yjs_guard

        validator = yjs_guard.UpdateValidator(workers=1)
        try:
            good = yjs_guard.seed_state("hello")
            assert await validator.validate(good) is None
            for payload in (ABORTING_UPDATE, SEGFAULTING_UPDATE,
                            PANICKING_UPDATE, b"\x00\x01\x02garbage"):
                assert await validator.validate(payload) is not None
                assert await validator.validate(good) is None
        finally:
            await validator.aclose()

        class PanicLike(BaseException):
            pass

        def _panic():
            raise PanicLike("rust panic")

        with pytest.raises(yjs_guard.YjsDecodeError):
            yjs_guard.guarded(_panic, what="test")
        with pytest.raises(KeyboardInterrupt):
            yjs_guard.guarded(
                lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
                what="test")

        worker = yjs_guard._DecoderWorker()
        worker.kill()
        assert worker._process is None
        process = worker._ensure()
        process.kill()
        process.wait()
        worker.kill()
        assert worker._process is None

        import api.yjs_websocket as yjs_ws
        import main
        import yjs_relay
        from fastapi.testclient import TestClient
        from websocket_manager import manager

        async def _allow(note_id, identity):
            from domains.research.service import NoteAccess

            return NoteAccess(can_read=True, can_write=True)

        async def _no_snapshot(note_id):
            return None

        async def _noop_publish(note_id, update):
            return None

        async def _persist(note_id, state):
            return True

        monkeypatch.setattr(yjs_ws, "_note_access", _allow)
        monkeypatch.setattr(yjs_relay, "_load_snapshot", _no_snapshot)
        monkeypatch.setattr(yjs_relay, "_publish_cross_replica", _noop_publish)
        monkeypatch.setattr(yjs_relay, "_persist_snapshot", _persist)

        me, token = _token()
        note_id = str(uuid4())
        client = TestClient(main.app)
        with client.websocket_connect(f"/ws/yjs/{note_id}?token={token}") as s:
            s.receive_bytes()
            assert note_id in yjs_relay.relay._rooms
            s.send_bytes(b"\x00\x01\x02garbage")
            assert _text_close_code(s) == 1008
        assert note_id not in yjs_relay.relay._rooms
        assert manager._user_counts.get(me, 0) == 0

        with client.websocket_connect(f"/ws/yjs/{note_id}?token={token}") as s:
            s.receive_bytes()
            s.send_text("not binary")
            assert _text_close_code(s) == 1003
        assert note_id not in yjs_relay.relay._rooms

        writer_a, token_a = _token()
        writer_b, token_b = _token()
        attacker, token_c = _token()
        for user in (writer_a, writer_b, attacker):
            yjs_env[user] = (True, True)
        note_id = str(uuid4())
        with _shared_client() as client:
            url = f"/ws/yjs/{note_id}?token="
            with (client.websocket_connect(url + token_a) as
                  a, client.websocket_connect(url + token_b) as b):
                a.receive_bytes()
                b.receive_bytes()
                editor = _Editor()
                first = editor.type("hello")
                a.send_bytes(first)
                assert b.receive_bytes() == first

                for payload in (ABORTING_UPDATE, SEGFAULTING_UPDATE,
                                PANICKING_UPDATE, b"\x00\x01\x02garbage"):
                    with client.websocket_connect(url + token_c) as c:
                        c.receive_bytes()
                        c.send_bytes(payload)
                        assert _bytes_close_code(c) == 1008

                original_limit = yjs_relay.max_update_bytes
                monkeypatch.setattr(yjs_relay, "max_update_bytes", lambda: 64)
                with client.websocket_connect(url + token_c) as c:
                    c.receive_bytes()
                    c.send_bytes(_update("x" * 200))
                    assert _bytes_close_code(c) == 1008
                monkeypatch.setattr(
                    yjs_relay, "max_update_bytes", original_limit)

                second = editor.type(" world")
                a.send_bytes(second)
                assert b.receive_bytes() == second
                assert _room_text(client, note_id) == "hello world"
            assert _wait_room_evicted(note_id)

    def test_viewer_and_downgraded_socket_cannot_write(self, yjs_env):
        writer, writer_token = _token()
        viewer, viewer_token = _token()
        guest, guest_token = _token(role="guest")
        yjs_env[writer] = (True, True)
        yjs_env[viewer] = (True, False)
        yjs_env[guest] = (True, False)
        _, stranger_token = _token()
        note_id = str(uuid4())
        with _shared_client() as client:
            url = f"/ws/yjs/{note_id}?token="
            with (client.websocket_connect(url + writer_token) as
                  w, client.websocket_connect(url + viewer_token) as
                  v, client.websocket_connect(url + guest_token) as g):
                w.receive_bytes()
                v.receive_bytes()
                g.receive_bytes()
                editor = _Editor()
                one = editor.type("from writer")
                w.send_bytes(one)
                assert v.receive_bytes() == one
                assert g.receive_bytes() == one

                v.send_bytes(_update("viewer edit"))
                g.send_bytes(_update("guest edit"))
                two = editor.type(" again")
                w.send_bytes(two)
                assert v.receive_bytes() == two
                assert _room_text(client, note_id) == "from writer again"
            assert _wait_room_evicted(note_id)

            with client.websocket_connect(url + stranger_token) as s:
                assert _bytes_close_code(s) == 4403

        import api.yjs_websocket as yjs_ws

        writer, writer_token = _token()
        member, member_token = _token()
        yjs_env[writer] = (True, True)
        yjs_env[member] = (True, True)
        note_id = str(uuid4())
        with _shared_client() as client:
            url = f"/ws/yjs/{note_id}?token="
            with (client.websocket_connect(url + writer_token) as
                  w, client.websocket_connect(url + member_token) as m):
                w.receive_bytes()
                m.receive_bytes()
                editor = _Editor()

                yjs_env[member] = (True, False)
                assert (
                    client.portal.call(
                        functools.partial(
                            yjs_ws.recheck_user_sessions, member,
                            reason="role")) == 1)
                m.send_bytes(_update("member edit"))
                first = editor.type("from writer")
                w.send_bytes(first)
                assert m.receive_bytes() == first
                assert _room_text(client, note_id) == "from writer"

                yjs_env[member] = (False, False)
                assert (
                    client.portal.call(
                        functools.partial(
                            yjs_ws.recheck_user_sessions, member,
                            reason="removed")) == 1)
                assert _bytes_close_code(m) == 4403

                assert (
                    client.portal.call(
                        functools.partial(
                            yjs_ws.recheck_user_sessions, writer, reason="off",
                            deactivated=True)) == 1)
                assert _bytes_close_code(w) == 4401
            assert _wait_room_evicted(note_id)

    async def test_room_markers_listener_and_access_recheck(
            self, monkeypatch, note_access):
        import yjs_relay

        marked: list[str] = []

        async def _mark(note_id):
            marked.append(note_id)

        monkeypatch.setattr(yjs_relay, "_mark_room_active", _mark)
        monkeypatch.setattr(yjs_relay, "ACTIVE_HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr(
            yjs_relay.relay, "local_active_note_ids", lambda: ["n1"])
        task = asyncio.create_task(yjs_relay.run_active_room_heartbeat())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert marked and set(marked) == {"n1"}

        import shared.events as events

        applied: list[tuple[str, bytes]] = []

        async def _apply(note_id, update):
            applied.append((note_id, update))

        first = _FakePubSub(fail=True, messages=[])
        second = _FakePubSub(
            fail=False, messages=[{
                "type": "pmessage",
                "channel": "yjs:note:n1",
                "data": "otherreplica:AAE="
            }])
        client = _FakeClient([first, second])
        monkeypatch.setattr(
            events, "get_events_client", lambda settings: client)
        monkeypatch.setattr(events, "RECONNECT_BACKOFF_SECONDS", (0.01, ))
        monkeypatch.setattr(yjs_relay.relay, "apply_remote_update", _apply)

        task = asyncio.create_task(yjs_relay.run_cross_replica_listener())
        for _ in range(100):
            if applied:
                break
            await asyncio.sleep(0.01)
        assert applied == [("n1", b"\x00\x01")]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        import api.yjs_websocket as yjs_ws

        user = str(uuid4())
        revoked = _session(user, can_write=True)
        demoted = _session(user, can_write=True)
        unchanged = _session(user, can_write=True)
        other = _session(str(uuid4()), can_write=True)
        note_access[revoked.note_id] = (False, False)
        note_access[demoted.note_id] = (True, False)

        changed = await yjs_ws.recheck_user_sessions(user, reason="test")
        assert changed == 2
        assert revoked.websocket.closed == (
            4403, "access to this note was revoked")
        assert demoted.websocket.closed is None
        assert (
            demoted.access.can_write is False
            and demoted.access.can_read is True)
        assert (
            unchanged.websocket.closed is None and unchanged.access.can_write)
        assert other.websocket.closed is None
        assert yjs_ws._sessions_by_user[user] == {demoted, unchanged}

        async def _down(note_id, identity):
            raise ConnectionError("auth down")

        monkeypatch.setattr(yjs_ws, "_note_access", _down)
        user = str(uuid4())
        session = _session(user, can_write=True)
        assert await yjs_ws.recheck_user_sessions(user, reason="test") == 0
        assert session.websocket.closed is None and session.access.can_write

    async def test_room_saves_and_failed_sends(self, monkeypatch):
        import y_py as Y
        import yjs_relay

        saved = []

        async def _persist(note_id, state):
            saved.append(note_id)
            return True

        monkeypatch.setattr(yjs_relay, "_persist_snapshot", _persist)
        relay = yjs_relay.YjsRelay()
        due = yjs_relay.YjsRoom("due", None)
        fresh = yjs_relay.YjsRoom("fresh", None)
        clean = yjs_relay.YjsRoom("clean", None)
        for room in (due, fresh):
            source = Y.YDoc()
            with source.begin_transaction() as txn:
                source.get_text("t").extend(txn, "hello")
            room.apply(
                Y.encode_state_as_update(source), counts_toward_snapshot=True)
        due._last_snapshot_at -= 3600
        for room in (due, fresh, clean):
            room.connections.add(object())  # type: ignore[arg-type]
        relay._rooms = {"due": due, "fresh": fresh, "clean": clean}
        assert await relay.flush_due_snapshots() == 1
        assert saved == ["due"]
        assert not due.should_snapshot()
        fresh._last_snapshot_at -= 3600

        async def _fail(note_id, state):
            return False

        monkeypatch.setattr(yjs_relay, "_persist_snapshot", _fail)
        assert await relay.flush_due_snapshots() == 0
        assert fresh.should_snapshot()

        saved: list[str] = []

        async def _ok(note_id, state):
            saved.append(note_id)
            return True

        monkeypatch.setattr(yjs_relay, "_persist_snapshot", _ok)
        relay = yjs_relay.YjsRelay()
        relay._rooms = {
            "a": _dirty_room("a"),
            "b": yjs_relay.YjsRoom("b", None)
        }
        assert await relay.persist_all_dirty() == 1
        assert saved == ["a"]

        async def _fail(note_id, state):
            raise RuntimeError("database down")

        async def _noop(note_id):
            return None

        monkeypatch.setattr(yjs_relay, "_persist_snapshot", _fail)
        monkeypatch.setattr(yjs_relay, "_mark_room_inactive", _noop)
        relay = yjs_relay.YjsRelay()
        room = _dirty_room("n1")
        socket = object()
        room.connections.add(socket)  # type: ignore[arg-type]
        relay._rooms = {"n1": room}
        await relay.disconnect(socket, room)  # type: ignore[arg-type]
        assert relay.has_local_room("n1")

        saved: list[str] = []

        async def _ok(note_id, state):
            saved.append(note_id)
            return True

        monkeypatch.setattr(yjs_relay, "_persist_snapshot", _ok)
        assert await relay.flush_due_snapshots() == 1
        assert saved == ["n1"]
        assert not relay.has_local_room("n1")

        room = yjs_relay.YjsRoom("n-broadcast", None)
        good = _BroadcastSocket()
        broken = _BroadcastSocket(send_fails=True)
        broken_twice = _BroadcastSocket(send_fails=True, close_fails=True)
        sender = _BroadcastSocket()
        room.connections.update([good, broken, broken_twice,
                                 sender]  # type: ignore[list-item]
                                )

        await yjs_relay.relay._broadcast_local(
            room, b"update", exclude=sender)  # type: ignore[arg-type]

        assert good.sent == [b"update"] and not good.closed
        assert sender.sent == [] and not sender.closed
        assert broken.closed and broken_twice.closed
        assert room.connections == {good, sender}

    async def test_close_note_sessions_on_hide_and_delete(
            self, _clean_rooms, monkeypatch):
        room, sockets = await _room_with_sockets("n-hide", 2, dirty=True)
        closed = await yjs_relay.relay.close_note_sessions(
            "n-hide", reason="note hidden by a moderator", discard_edits=False)
        assert closed == 2
        assert all(
            s.closed == (4403, "note hidden by a moderator") for s in sockets)
        assert room._dirty

        async def _inactive(note_id):
            return None

        monkeypatch.setattr(yjs_relay, "_mark_room_inactive", _inactive)
        room, _ = await _room_with_sockets("n-del", 1, dirty=True)
        room.connections.clear()
        assert (
            await yjs_relay.relay.close_note_sessions(
                "n-del", reason="note deleted", discard_edits=True) == 0)
        assert not room._dirty
        assert "n-del" not in yjs_relay.relay._rooms
        assert (
            await yjs_relay.relay.close_note_sessions(
                "missing", reason="x", discard_edits=True) == 0)
