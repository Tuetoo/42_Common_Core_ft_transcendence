from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
import pytest
from tests.auth_tokens import auth_headers as _headers

_STORAGE: dict[str, bytes] = {}


@pytest.fixture
def member_roles_and_no_sentiment(monkeypatch, no_sentiment_analysis):
    import clients.auth_client as auth_client

    async def _roles(user_id):
        ids = await auth_client.list_user_organization_ids(user_id)
        return dict.fromkeys(ids, "member")

    monkeypatch.setattr(auth_client, "list_user_organization_roles", _roles)


@pytest.fixture
def research_doubles(monkeypatch, no_sentiment_analysis):
    import outbox
    import domains.notification.service as notification_service
    import domains.research.service as research_service

    async def _upload(settings, *, bucket, key, content, content_type=None):
        _STORAGE[key] = content

    async def _download(settings, *, bucket, key):
        return _STORAGE[key]

    async def _delete(settings, *, bucket, key):
        _STORAGE.pop(key, None)

    published: list[tuple[str, dict[str, Any]]] = []

    async def _publish(settings, event_name, payload, *, log=True):
        published.append((event_name, payload))

    notified: list[tuple[str, str, str]] = []

    async def _notify_users(user_ids, *, type, title, content):
        for uid in dict.fromkeys(str(u) for u in user_ids if u):
            notified.append((uid, str(type), title))

    monkeypatch.setattr(research_service, "upload_bytes", _upload)
    monkeypatch.setattr(research_service, "download_bytes", _download)
    monkeypatch.setattr(research_service, "delete_object", _delete)
    monkeypatch.setattr(outbox, "publish_durable_event", _publish)
    monkeypatch.setattr(notification_service, "notify_users", _notify_users)
    return {"published": published, "notified": notified}


async def _fake_upload_bytes(
        settings, *, bucket, key, content, content_type=None):
    _STORAGE[key] = content


class _StorageRecorder:

    def __init__(self) -> None:
        self.uploaded: list[str] = []
        self.deleted: list[str] = []

    async def upload(
            self, settings, *, bucket, key, content, content_type=None):
        self.uploaded.append(key)
        _STORAGE[key] = content

    async def download(self, settings, *, bucket, key):
        return _STORAGE[key]

    async def delete(self, settings, *, bucket, key):
        self.deleted.append(key)
        _STORAGE.pop(key, None)


@pytest.fixture
def storage_recorder(monkeypatch):
    import domains.research.service as research_service

    rec = _StorageRecorder()
    monkeypatch.setattr(research_service, "upload_bytes", rec.upload)
    monkeypatch.setattr(research_service, "download_bytes", rec.download)
    monkeypatch.setattr(research_service, "delete_object", rec.delete)
    return rec


async def _create_note(client, headers, **body):
    body.setdefault("title", "Thesis")
    response = await client.post(
        "/api/v1/research/notes", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _upload(client, headers, name, **data):
    data.setdefault("category", "news")
    response = await client.post(
        "/api/v1/research/documents", headers=headers, files={
            "file": (
                name, b"%PDF-1.4 x" if name.endswith(".pdf") else b"text",
                "application/octet-stream")
        }, data=data)
    assert response.status_code == 201, response.text
    return response.json()["data"]


@pytest.fixture
async def committed_note(_schema_ready):
    from database import SessionLocal
    from domains.research.model import ResearchNote

    note_id = uuid4()
    async with SessionLocal() as session:
        session.add(
            ResearchNote(
                id=note_id, author_id=uuid4(), title="Collab",
                content="Seed text"))
        await session.commit()
    yield note_id
    async with SessionLocal() as session:
        row = await session.get(ResearchNote, note_id)
        if row is not None:
            await session.delete(row)
            await session.commit()


async def _note_row(note_id):
    from database import SessionLocal
    from domains.research.model import ResearchNote

    async with SessionLocal() as session:
        return await session.get(ResearchNote, note_id)


@pytest.fixture
async def committed_sentiment_rows(_schema_ready):
    from database import SessionLocal
    from domains.research.model import ResearchComment, ResearchNote

    note_id, comment_id = uuid4(), uuid4()
    async with SessionLocal() as session:
        session.add(
            ResearchNote(
                id=note_id, author_id=uuid4(), title="AMD",
                content="Great margins"))
        await session.flush()
        session.add(
            ResearchComment(
                id=comment_id, note_id=note_id, author_id=uuid4(),
                content="Agreed"))
        await session.commit()
    yield note_id, comment_id
    async with SessionLocal() as session:
        row = await session.get(ResearchNote, note_id)
        if row is not None:
            await session.delete(row)
            await session.commit()


class _SentimentResponse:

    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _document(**overrides):
    from domains.research.model import Document

    values = {
        "owner_id": uuid4(),
        "filename": f"news_digest_{uuid4().hex[:8]}.txt",
        "document_type": "txt",
        "category": "news",
        "ticker": "AMD",
        "storage_url": f"documents/{uuid4()}.txt",
        "uploaded_at": datetime(2026, 9, 1, tzinfo=UTC)
    }
    values.update(overrides)
    return Document(**values)


class _DeletionRecorder:

    def __init__(self, fail_delete_for=None):
        self.deleted_keys: list[tuple[str, str]] = []
        self.published: list[tuple[str, dict]] = []
        self._fail_delete_for = fail_delete_for

    async def delete_object(self, settings, *, bucket, key):
        if key == self._fail_delete_for:
            raise RuntimeError("storage unavailable")
        self.deleted_keys.append((bucket, key))

    async def publish(self, entries):
        self.published.extend((e.event_name, e.payload) for e in entries)


@pytest.mark.usefixtures("member_roles_and_no_sentiment")
class TestNotesAndDocumentUploads:

    async def test_note_tags_and_member_org_upload_event(
            self, client, mock_cross_service, monkeypatch):
        _, headers = _headers()
        first = await client.post(
            "/api/v1/research/notes", json={
                "title": "First",
                "tags": ["AI"]
            }, headers=headers)
        second = await client.post(
            "/api/v1/research/notes", json={
                "title": "Second",
                "tags": [" ai ", "ai"]
            }, headers=headers)
        assert first.json()["data"]["tags"] == ["ai"]
        assert second.json()["data"]["tags"] == ["ai"]

        results = await client.get(
            "/api/v1/research/notes", params={"tag": "AI"}, headers=headers)
        assert results.json()["total"] == 2

        _, headers = _headers()
        url = "/api/v1/research/notes"
        assert (
            await client.post(
                url, json={
                    "title": "t",
                    "content": "x" * 200_001
                }, headers=headers)).status_code == 422
        assert (
            await client.post(
                url, json={
                    "title": "t",
                    "tags": ["x" * 51]
                }, headers=headers)).status_code == 422
        assert (
            await client.post(
                url, json={
                    "title": "t",
                    "tags": [f"t{i}" for i in range(21)]
                }, headers=headers)).status_code == 422
        ok = await client.post(
            url, json={
                "title": "t",
                "tags": ["  ", "", "Keep "]
            }, headers=headers)
        assert ok.status_code == 201
        assert ok.json()["data"]["tags"] == ["keep"]
        note_id = ok.json()["data"]["id"]
        assert (
            await client.patch(
                f"{url}/{note_id}", json={"tags": ["y" * 51]},
                headers=headers)).status_code == 422
        assert (
            await client.post(
                f"{url}/{note_id}/comments", json={"content": "c" * 10_001},
                headers=headers)).status_code == 422

        import clients.auth_client as auth_client
        import domains.research.service as research_service

        monkeypatch.setattr(
            research_service, "upload_bytes", _fake_upload_bytes)
        my_org_id = uuid4()

        async def _fake_list_ids(user_id):
            return [my_org_id]

        monkeypatch.setattr(
            auth_client, "list_user_organization_ids", _fake_list_ids)
        _, headers = _headers()
        response = await client.post(
            "/api/v1/research/documents", files={
                "file": ("filing.pdf", b"%PDF-1.4 fake", "application/pdf")
            }, data={
                "category": "filing",
                "organization_id": str(my_org_id)
            }, headers=headers)
        assert response.status_code == 201
        assert response.json()["data"]["organization_id"] == str(my_org_id)

        import outbox

        monkeypatch.setattr(
            research_service, "upload_bytes", _fake_upload_bytes)
        published = []

        async def _fake_publish(settings, event_name, payload, *, log=True):
            published.append((event_name, payload))

        monkeypatch.setattr(outbox, "publish_durable_event", _fake_publish)
        _, headers = _headers()
        response = await client.post(
            "/api/v1/research/documents",
            files={"file": ("note.md", b"# Macro note", "text/markdown")},
            data={"category": "macro_research"}, headers=headers)
        assert response.status_code == 201
        assert len(published) == 1
        event_name, payload = published[0]
        assert event_name == "document.uploaded"
        assert payload["document_id"] == response.json()["data"]["id"]

    async def test_failed_duplicate_retry_and_ticker_normalization(
            self, client, db_session, mock_cross_service, storage_recorder,
            published_events):
        from domains.research.model import Document

        _, headers = _headers()
        kwargs = {
            "files": {
                "file": ("retry.pdf", b"%PDF-1.4 v1", "application/pdf")
            },
            "data": {
                "category": "filing",
                "ticker": "AAPL"
            },
            "headers": headers
        }
        first = await client.post("/api/v1/research/documents", **kwargs)
        doc_id = UUID(first.json()["data"]["id"])
        row = await db_session.get(Document, doc_id)
        row.status = "failed"
        await db_session.flush()

        kwargs["files"] = {
            "file": ("retry.pdf", b"%PDF-1.4 v2", "application/pdf")
        }
        second = await client.post("/api/v1/research/documents", **kwargs)
        assert second.status_code == 201
        assert second.json()["data"]["status"] == "uploaded"
        assert storage_recorder.uploaded == [row.storage_url, row.storage_url]
        assert _STORAGE[row.storage_url] == b"%PDF-1.4 v2"
        assert len(published_events) == 2 and published_events[1][1][
            "document_id"] == str(doc_id)

        _, headers = _headers()
        document = await _upload(
            client, headers, "spaced.txt", ticker=" aapl ")
        assert document["ticker"] == "AAPL"
        assert published_events[-1][1]["ticker"] == "AAPL"
        blank = await _upload(client, headers, "blank.txt", ticker="  ")
        assert blank["ticker"] is None
        for bad in ("BERKSHIREHATH", "AA PL", "AAPL;"):
            response = await client.post(
                "/api/v1/research/documents", headers=headers,
                files={"file": ("bad.txt", b"text", "text/plain")}, data={
                    "category": "news",
                    "ticker": bad
                })
            assert response.status_code == 422, response.text
            assert response.json()["error"]["code"] == "validation_error"
        assert not any("bad.txt" in k for k in storage_recorder.uploaded)
        response = await client.post(
            "/internal/documents/system",
            files={"file": ("bad.txt", b"text", "text/plain")}, data={
                "category": "news",
                "ticker": "BRK B"
            })
        assert response.status_code == 422, response.text

    async def test_failed_or_lost_upload_deletes_its_object(
            self, monkeypatch, db_session, mock_cross_service,
            storage_recorder, published_events):
        from fastapi import BackgroundTasks
        from shared.exceptions import ConflictError
        import domains.research.service as research_service
        from domains.research.service import ResearchService

        owner = uuid4()
        service = ResearchService(db_session)
        winner = await service.documents.create(
            owner_id=owner, organization_id=None, filename="race.txt",
            document_type="txt", category="news", ticker="AMD",
            storage_url="documents/winner.txt")

        original = (
            research_service.DocumentRepository.get_by_ticker_category_filename
        )
        calls = {"n": 0}

        async def _first_call_misses(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            return await original(self, **kwargs)

        monkeypatch.setattr(
            research_service.DocumentRepository,
            "get_by_ticker_category_filename", _first_call_misses)
        tasks = BackgroundTasks()
        outcome = await service.upload(
            owner_id=owner, organization_id=None, filename="race.txt",
            content=b"x", category="news", ticker="amd",
            background_tasks=tasks)
        assert outcome.document.id == winner.id
        assert (
            len(storage_recorder.uploaded) == 1
            and storage_recorder.deleted == storage_recorder.uploaded)
        assert tasks.tasks == []

        async def _allow_writer(user_id, organization_id):
            return None

        org_id = uuid4()
        monkeypatch.setattr(
            research_service.auth_client, "ensure_organization_writer",
            _allow_writer)
        mine = await service.documents.create(
            owner_id=owner, organization_id=org_id, filename="org.txt",
            document_type="txt", category="news", ticker=None,
            storage_url="documents/org.txt")
        mine.status = "failed"
        await db_session.flush()
        uploads_before = list(storage_recorder.uploaded)
        with pytest.raises(ConflictError):
            await service.upload(
                owner_id=uuid4(), organization_id=org_id, filename="org.txt",
                content=b"other", category="news", background_tasks=tasks)
        assert mine.status == "failed"
        assert storage_recorder.uploaded == uploads_before

        async def _boom(self, **kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(
            research_service.DocumentRepository, "create", _boom)
        tasks = BackgroundTasks()
        with pytest.raises(RuntimeError):
            await ResearchService(db_session).upload(
                owner_id=uuid4(), organization_id=None, filename="x.txt",
                content=b"x", category="news", background_tasks=tasks)
        assert (
            storage_recorder.uploaded
            and storage_recorder.deleted == storage_recorder.uploaded)
        assert tasks.tasks == []

    async def test_comment_list_and_delete_permissions(
            self, client, monkeypatch, mock_cross_service):
        import clients.auth_client as auth_client

        org_id = uuid4()
        author, author_h = _headers()
        member, member_h = _headers()
        _, stranger_h = _headers()

        async def _orgs(user_id):
            return [org_id] if user_id in (author, member) else []

        monkeypatch.setattr(auth_client, "list_user_organization_ids", _orgs)
        note = (
            await client.post(
                "/api/v1/research/notes", json={
                    "title": "shared",
                    "organization_id": str(org_id)
                }, headers=author_h)).json()["data"]
        base = f"/api/v1/research/notes/{note['id']}/comments"
        c1 = (
            await
            client.post(base, json={"content": "by member"},
                        headers=member_h)).json()["data"]
        c2 = (
            await
            client.post(base, json={"content": "by author"},
                        headers=author_h)).json()["data"]
        for i in range(3):
            await client.post(
                base, json={"content": f"more {i}"}, headers=member_h)

        seen = []
        for page_no in (1, 2, 3):
            page = await client.get(
                base, params={
                    "page": page_no,
                    "page_size": 2
                }, headers=member_h)
            assert page.status_code == 200
            assert page.json()["total"] == 5
            seen += [c["content"] for c in page.json()["items"]]
        assert sorted(seen) == sorted(
            ["by member", "by author", "more 0", "more 1", "more 2"])
        assert (await client.get(base, headers=stranger_h)).status_code == 404

        assert (
            await client.delete(f"{base}/{c2['id']}",
                                headers=member_h)).status_code == 403
        assert (
            await client.delete(f"{base}/{c1['id']}",
                                headers=stranger_h)).status_code == 404
        assert (
            await client.delete(f"{base}/{c1['id']}",
                                headers=member_h)).status_code == 204
        assert (
            await client.delete(f"{base}/{c2['id']}",
                                headers=author_h)).status_code == 204
        assert (
            await client.delete(f"{base}/{c2['id']}",
                                headers=author_h)).status_code == 404
        assert (await client.get(base, headers=author_h)).json()["total"] == 3

        other = (
            await client.post(
                "/api/v1/research/notes", json={"title": "other"},
                headers=author_h)).json()["data"]
        c3 = (
            await client.post(base, json={"content": "x"},
                              headers=author_h)).json()["data"]
        wrong_note = (
            f"/api/v1/research/notes/{other['id']}/comments/{c3['id']}")
        assert (
            await client.delete(wrong_note,
                                headers=author_h)).status_code == 404


@pytest.mark.usefixtures("research_doubles")
class TestResearchContent:

    async def test_moderation_and_read_only_roles(
            self, client, organization_roles, research_doubles,
            mock_cross_service):
        org_id, roles = organization_roles
        author, author_h = _headers()
        member, member_h = _headers()
        _, moderator_h = _headers(role="moderator")
        roles.update({author: "member", member: "member"})
        note = await _create_note(
            client, author_h, organization_id=str(org_id), title="Spam spam",
            content="buy now")

        assert (
            await client.post(
                f"/api/v1/moderation/notes/{note['id']}/hide",
                headers=member_h)).status_code == 403

        hidden = await client.post(
            f"/api/v1/moderation/notes/{note['id']}/hide", headers=moderator_h)
        assert hidden.status_code == 200
        assert hidden.json()["data"]["is_hidden"] is True
        assert (author, "note_hidden",
                "Your note was hidden") in research_doubles["notified"]

        assert (await client.get("/api/v1/research/notes",
                                 headers=member_h)).json()["items"] == []
        assert (
            await client.get(
                "/api/v1/research/notes", params={"q": "spam"},
                headers=member_h)).json()["items"] == []
        assert (
            await client.get(
                f"/api/v1/research/notes/{note['id']}",
                headers=member_h)).status_code == 404
        assert (
            await client.get(
                f"/api/v1/research/notes/{note['id']}/comments",
                headers=member_h)).status_code == 404
        mine = (await client.get("/api/v1/research/notes",
                                 headers=author_h)).json()
        assert mine["items"][0]["is_hidden"] is True
        restored = await client.post(
            f"/api/v1/moderation/notes/{note['id']}/unhide",
            headers=moderator_h)
        assert restored.json()["data"]["is_hidden"] is False
        assert (
            len((await client.get("/api/v1/research/notes",
                                  headers=member_h)).json()["items"]) == 1)

        deleted = await client.delete(
            f"/api/v1/moderation/notes/{note['id']}", headers=moderator_h)
        assert deleted.status_code == 204
        assert (author, "note_deleted",
                "Your note was removed") in research_doubles["notified"]
        assert (
            await client.get(
                f"/api/v1/research/notes/{note['id']}",
                headers=author_h)).status_code == 404
        assert (
            await client.delete(
                f"/api/v1/moderation/notes/{note['id']}",
                headers=moderator_h)).status_code == 404

        org_id, roles = organization_roles
        author, author_h = _headers()
        commenter, commenter_h = _headers()
        other, other_h = _headers()
        _, moderator_h = _headers(role="admin")
        roles.update({author: "member", commenter: "member", other: "member"})
        note = await _create_note(
            client, author_h, organization_id=str(org_id))
        base = f"/api/v1/research/notes/{note['id']}/comments"
        comment = (
            await
            client.post(base, headers=commenter_h,
                        json={"content": "first"})).json()["data"]
        assert comment["updated_at"] is None
        expected = (author, "comment_created", "New comment on your note")
        assert expected in research_doubles["notified"]

        assert (
            await client.patch(
                f"{base}/{comment['id']}", headers=other_h,
                json={"content": "x"})).status_code == 403
        assert (
            await client.patch(
                f"{base}/{comment['id']}", headers=commenter_h,
                json={"content": ""})).status_code == 422
        patched = await client.patch(
            f"{base}/{comment['id']}", headers=commenter_h,
            json={"content": "edited"})
        assert patched.status_code == 200
        assert patched.json()["data"]["content"] == "edited"
        assert patched.json()["data"]["updated_at"] is not None

        hide = await client.post(
            f"/api/v1/moderation/comments/{comment['id']}/hide",
            headers=moderator_h)
        assert hide.json()["data"]["is_hidden"] is True
        assert (await client.get(base, headers=other_h)).json()["total"] == 0
        own = (await client.get(base, headers=commenter_h)).json()
        assert own["total"] == 1 and own["items"][0]["is_hidden"] is True
        assert (await client.get(base, headers=moderator_h)).status_code == 404
        assert (
            await client.patch(
                f"{base}/{comment['id']}", headers=other_h,
                json={"content": "x"})).status_code == 404

        await client.post(
            f"/api/v1/moderation/comments/{comment['id']}/unhide",
            headers=moderator_h)
        assert (await client.get(base, headers=other_h)).json()["total"] == 1
        assert (
            await client.delete(
                f"/api/v1/moderation/comments/{comment['id']}",
                headers=moderator_h)).status_code == 204
        expected = (commenter, "comment_deleted", "Your comment was removed")
        assert expected in research_doubles["notified"]
        assert (await client.get(base, headers=author_h)).json()["total"] == 0

        _, author_headers = _headers()
        note = await _create_note(client, author_headers)
        _, guest = _headers(role="guest")

        assert (
            await client.post(
                "/api/v1/research/notes", json={"title": "x"},
                headers=guest)).status_code == 403
        assert (
            await client.post(
                f"/api/v1/research/notes/{note['id']}/comments",
                json={"content": "hi"}, headers=guest)).status_code == 403
        upload = await client.post(
            "/api/v1/research/documents", headers=guest,
            files={"file": ("a.txt", b"hello", "text/plain")},
            data={"category": "news"})
        assert upload.status_code == 403
        listed = await client.get("/api/v1/research/notes", headers=guest)
        assert listed.status_code == 200

        org_id, roles = organization_roles
        owner, owner_h = _headers()
        member, member_h = _headers()
        viewer, viewer_h = _headers()
        roles.update({owner: "owner", member: "member", viewer: "viewer"})

        note = await _create_note(
            client, owner_h, organization_id=str(org_id), content="Body")
        assert note["can_edit"] is True

        read = await client.get(
            f"/api/v1/research/notes/{note['id']}", headers=viewer_h)
        assert (
            read.status_code == 200
            and read.json()["data"]["can_edit"] is False)
        denied = await client.post(
            "/api/v1/research/notes", headers=viewer_h, json={
                "title": "v",
                "organization_id": str(org_id)
            })
        assert denied.status_code == 403
        assert (
            await client.patch(
                f"/api/v1/research/notes/{note['id']}", headers=viewer_h,
                json={"title": "v"})).status_code == 403
        viewer_comment = await client.post(
            f"/api/v1/research/notes/{note['id']}/comments", headers=viewer_h,
            json={"content": "v"})
        assert viewer_comment.status_code == 403
        viewer_doc = await client.post(
            "/api/v1/research/documents", headers=viewer_h,
            files={"file": ("v.txt", b"viewer", "text/plain")}, data={
                "category": "news",
                "organization_id": str(org_id)
            })
        assert viewer_doc.status_code == 403

        listed = await client.get("/api/v1/research/notes", headers=member_h)
        assert listed.json()["items"][0]["can_edit"] is True
        edited = await client.patch(
            f"/api/v1/research/notes/{note['id']}", headers=member_h,
            json={"content": "Member body"})
        assert edited.status_code == 200
        assert edited.json()["data"]["content"] == "Member body"
        comment = await client.post(
            f"/api/v1/research/notes/{note['id']}/comments", headers=member_h,
            json={"content": "ok"})
        assert comment.status_code == 201
        created = await client.post(
            "/api/v1/research/notes", headers=member_h, json={
                "title": "m",
                "organization_id": str(org_id)
            })
        assert created.status_code == 201

    async def test_rest_body_edit_with_active_room_and_crdt_history(
            self, client, db_session, mock_cross_service, research_doubles):
        import yjs_relay

        _, headers = _headers()
        note = await _create_note(client, headers, content="Start")
        url = f"/api/v1/research/notes/{note['id']}"
        await yjs_relay._mark_room_active(note["id"])
        try:
            conflict = await client.patch(
                url, headers=headers, json={"content": "REST"})
            assert conflict.status_code == 409
            ok = await client.patch(
                url, headers=headers, json={
                    "title": "Renamed",
                    "tags": ["x"]
                })
            assert (
                ok.status_code == 200
                and ok.json()["data"]["title"] == "Renamed")
            same = await client.patch(
                url, headers=headers, json={"content": "Start"})
            assert same.status_code == 200
        finally:
            await yjs_relay._mark_room_inactive(note["id"])
        assert not await yjs_relay.is_note_room_active(note["id"])
        done = await client.patch(
            url, headers=headers, json={"content": "After"})
        assert (
            done.status_code == 200
            and done.json()["data"]["content"] == "After")

        from domains.research.model import ResearchNote

        row = await db_session.get(ResearchNote, UUID(note["id"]))
        await db_session.refresh(row)
        assert yjs_relay.extract_text(row.yjs_state) == "After"

        import y_py as Y

        _, headers = _headers()
        note = await _create_note(client, headers, content="first")
        url = f"/api/v1/research/notes/{note['id']}"
        row = await db_session.get(ResearchNote, UUID(note["id"]))

        async def _patch(text):
            response = await client.patch(
                url, headers=headers, json={"content": text})
            assert response.status_code == 200, response.text
            await db_session.refresh(row)
            return bytes(row.yjs_state)

        old_state = await _patch("Hello \U0001f600 world")
        held = Y.YDoc()
        Y.apply_update(held, old_state)
        new_state = await _patch("Hello \U0001f600 brave new world!")
        Y.apply_update(held, new_state)
        held_text = held.get_text("content")
        assert str(held_text) == "Hello \U0001f600 brave new world!"

        held.transact(lambda txn: held_text.extend(txn, " Bye"))
        server = Y.YDoc()
        Y.apply_update(server, new_state)
        update = Y.encode_state_as_update(held, Y.encode_state_vector(server))
        merged = yjs_relay.merge_states(new_state, update)
        assert yjs_relay.extract_text(merged) == str(held_text)

        row.yjs_state = b"\x01\x02broken"
        await db_session.flush()
        assert yjs_relay.extract_text(await _patch("reseeded")) == "reseeded"

    async def test_persist_and_load_of_note_state(
            self, committed_note, monkeypatch):
        import y_py as Y
        import yjs_relay

        scheduled: list[UUID] = []
        monkeypatch.setattr(
            yjs_relay, "schedule_note_sentiment", scheduled.append)

        state = await yjs_relay._load_snapshot(str(committed_note))
        assert yjs_relay.extract_text(state) == "Seed text"
        first = await _note_row(committed_note)
        assert first.yjs_state == state
        assert await yjs_relay._load_snapshot(str(committed_note)) == state

        doc = Y.YDoc()
        Y.apply_update(doc, state)
        text = doc.get_text("content")
        doc.transact(lambda txn: text.extend(txn, " plus collaborative edit"))
        new_state = Y.encode_state_as_update(doc)

        assert await yjs_relay._persist_snapshot(
            str(committed_note), new_state)
        row = await _note_row(committed_note)
        assert row.content == "Seed text plus collaborative edit"
        assert row.updated_at > first.updated_at
        assert scheduled == [committed_note]

        assert await yjs_relay._persist_snapshot(
            str(committed_note), new_state)
        again = await _note_row(committed_note)
        assert again.updated_at == row.updated_at
        assert scheduled == [committed_note]

        from database import SessionLocal
        from sqlalchemy import func, select
        from domains.research.model import ResearchNote

        async with SessionLocal() as session:
            found = ((
                await session.execute(
                    select(ResearchNote.id).where(
                        ResearchNote.search_vector.op("@@")(
                            func.plainto_tsquery("english", "collaborative")))
                )).scalars().all())
        assert committed_note in found

        monkeypatch.setattr(
            yjs_relay, "schedule_note_sentiment", lambda _id: None)
        async with SessionLocal() as session:
            row = await session.get(ResearchNote, committed_note)
            row.yjs_state = b"\x01\x02broken"
            await session.commit()
        room = yjs_relay.YjsRoom(str(committed_note), b"\x01\x02broken")
        room.apply(
            yjs_relay.seed_state("room text"), counts_toward_snapshot=True)
        assert await yjs_relay._persist_snapshot(
            str(committed_note), room.encode_state())
        row = await _note_row(committed_note)
        assert row.content == "room text"
        assert yjs_relay.extract_text(row.yjs_state) == "room text"

    async def test_document_list_and_title_patch(
            self, client, db_session, mock_cross_service, research_doubles):
        from domains.research.model import Document

        _, headers = _headers()
        a = await _upload(
            client, headers, "a.txt", ticker="AAA", title="Zeta",
            content_date="2026-01-10T00:00:00Z")
        b = await _upload(
            client, headers, "b.pdf", ticker="BBB", title="alpha",
            category="filing", content_date="2026-03-01T00:00:00Z")
        c = await _upload(client, headers, "c.md")
        assert "document_uploaded" in {
            n[1]
            for n in research_doubles["notified"]
        }
        doc_b = await db_session.get(Document, UUID(b["id"]))
        doc_b.status = "ready"
        doc_c = await db_session.get(Document, UUID(c["id"]))
        doc_c.uploaded_at = datetime(2025, 12, 31, tzinfo=UTC)
        await db_session.flush()

        async def ids(**params):
            response = await client.get(
                "/api/v1/research/documents", params=params, headers=headers)
            assert response.status_code == 200, response.text
            return [d["id"] for d in response.json()["items"]]

        assert await ids(sort="title") == [b["id"], c["id"], a["id"]]
        assert await ids(sort="-content_date") == [b["id"], a["id"], c["id"]]
        assert await ids(status="ready") == [b["id"]]
        assert await ids(category="filing") == [b["id"]]
        assert await ids(ticker="aaa") == [a["id"]]
        assert await ids(content_from="2026-02-01",
                         content_to="2026-12-31") == [b["id"]]
        assert set(await ids(created_from="2026-01-01")) == {a["id"], b["id"]}
        assert await ids(created_to="2025-12-31") == [c["id"]]
        assert (await ids(sort="status"))[0] == b["id"]
        assert len(await ids(sort="-created_at", page_size=1, page=3)) == 1

        for bad in ({"sort": "owner"}, {"status": "done"
                                        }, {"created_from": "yesterday"},
                    {"created_from": "2026-02-02",
                     "created_to": "2026-02-01"}, {"category": "memes"}):
            response = await client.get(
                "/api/v1/research/documents", params=bad, headers=headers)
            assert response.status_code == 422, bad

        user, headers = _headers()
        _, other = _headers()
        doc = await _upload(client, headers, "r.txt")
        url = f"/api/v1/research/documents/{doc['id']}"
        assert (
            await client.patch(url, headers=other,
                               json={"title": "x"})).status_code == 404
        renamed = await client.patch(
            url, headers=headers, json={"title": " Q3 notes "})
        assert (
            renamed.status_code == 200
            and renamed.json()["data"]["title"] == "Q3 notes")
        assert (await client.delete(url, headers=headers)).status_code == 204
        kinds = [n[1] for n in research_doubles["notified"] if n[0] == user]
        assert kinds == [
            "document_uploaded", "document_updated", "document_deleted"
        ]

    async def test_upload_type_checks_and_preview(
            self, client, mock_cross_service, db_session):
        _, headers = _headers()
        for name, content in (("fake.pdf", b"not a pdf"),
                              ("bin.txt", b"\xff\xfe\x00"), ("empty.md", b"")):
            response = await client.post(
                "/api/v1/research/documents", headers=headers,
                files={"file": (name, content, "application/octet-stream")},
                data={"category": "news"})
            assert response.status_code == 422, name

        from domains.research.model import Document

        user, headers = _headers()
        _, stranger = _headers()
        pdf = await _upload(client, headers, "report.pdf")
        md = await _upload(client, headers, "notes.md")

        response = await client.get(
            f"/api/v1/research/documents/{pdf['id']}/preview", headers=headers)
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["content-disposition"].startswith("inline;")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.content.startswith(b"%PDF-")

        text = await client.get(
            f"/api/v1/research/documents/{md['id']}/preview", headers=headers)
        assert text.headers["content-type"] == "text/plain; charset=utf-8"
        assert "sandbox" in text.headers["content-security-policy"]

        assert (
            await client.get(
                f"/api/v1/research/documents/{pdf['id']}/preview",
                headers=stranger)).status_code == 404

        docx = Document(
            owner_id=UUID(user), filename="x.docx", document_type="docx",
            category="news", storage_url="documents/x.docx", status="ready")
        db_session.add(docx)
        await db_session.flush()
        unsupported = await client.get(
            f"/api/v1/research/documents/{docx.id}/preview", headers=headers)
        assert unsupported.status_code == 415
        assert unsupported.json()["error"]["code"] == "unsupported_media_type"
        download = await client.get(
            f"/api/v1/research/documents/{pdf['id']}/content", headers=headers)
        assert download.status_code == 200 and download.content.startswith(
            b"%PDF-")

    async def test_system_documents_and_delete_order(
            self, client, mock_cross_service, research_doubles, db_session,
            monkeypatch):
        from shared.schemas import SYSTEM_DOCUMENT_OWNER_ID

        form = {
            "category": "filing",
            "ticker": "amd",
            "title": "AMD 10-Q",
            "content_date": "2026-08-05",
            "source_url": "https://sec.gov/x"
        }
        files = {
            "file": (
                "AMD_10-Q_2026-08-05.pdf", b"%PDF-1.7 body", "application/pdf")
        }
        first = await client.post(
            "/internal/documents/system", data=form, files=files)
        assert first.status_code == 201, first.text
        body = first.json()
        assert body["status"] == "uploaded" and body["duplicate"] is False
        event_name, payload = research_doubles["published"][-1]
        assert event_name == "document.uploaded"
        assert payload["owner_id"] == str(SYSTEM_DOCUMENT_OWNER_ID)
        assert payload["is_system"] is True and payload["ticker"] == "AMD"
        assert payload["content_date"].startswith("2026-08-05")

        again = await client.post(
            "/internal/documents/system", data=form, files=files)
        assert again.status_code == 201
        assert again.json() == {
            "id": body["id"],
            "status": "uploaded",
            "duplicate": True
        }
        assert len(research_doubles["published"]) == 1

        bad = await client.post(
            "/internal/documents/system", data={
                **form, "category": "memes"
            }, files=files)
        assert bad.status_code == 422
        fake_pdf = await client.post(
            "/internal/documents/system", data=form,
            files={"file": ("x.pdf", b"nope", "application/pdf")})
        assert fake_pdf.status_code == 422
        no_token = await client.post(
            "/internal/documents/system", data=form, files=files,
            headers={"X-Internal-Token": "wrong"})
        assert no_token.status_code == 401
        assert research_doubles["notified"] == []

        from domains.research.model import Document

        system_doc = Document(
            owner_id=SYSTEM_DOCUMENT_OWNER_ID,
            filename="AMD_10-Q_2026-08-05.txt", document_type="txt",
            category="filing", ticker="AMD",
            storage_url="documents/system/AMD_10-Q_2026-08-05.txt",
            status="ready")
        db_session.add(system_doc)
        await db_session.flush()
        _, headers = _headers()

        detail = await client.get(
            f"/api/v1/research/documents/{system_doc.id}", headers=headers)
        assert detail.status_code == 200, detail.text

        default = await client.get(
            "/api/v1/research/documents", params={"ticker": "AMD"},
            headers=headers)
        assert default.status_code == 200
        assert str(
            system_doc.id) not in [d["id"] for d in default.json()["items"]]
        listed = await client.get(
            "/api/v1/research/documents", params={
                "ticker": "AMD",
                "include_system": "true",
                "sort": "-content_date"
            }, headers=headers)
        assert listed.status_code == 200
        assert str(system_doc.id) in [d["id"] for d in listed.json()["items"]]

        rename = await client.patch(
            f"/api/v1/research/documents/{system_doc.id}",
            json={"title": "mine now"}, headers=headers)
        assert rename.status_code == 403
        delete = await client.delete(
            f"/api/v1/research/documents/{system_doc.id}", headers=headers)
        assert delete.status_code == 403

        import domains.research.service as research_service

        _, headers = _headers()
        document = await _upload(client, headers, "a.txt")
        stored_before = len(_STORAGE)
        order: list[str] = []
        real_delete_file = research_service.delete_object
        real_delete_row = research_service.DocumentRepository.delete

        async def _delete_file(settings, *, bucket, key):
            order.append("file")
            await real_delete_file(settings, bucket=bucket, key=key)

        async def _delete_row(self, doc):
            order.append("row")
            await real_delete_row(self, doc)

        monkeypatch.setattr(research_service, "delete_object", _delete_file)
        monkeypatch.setattr(
            research_service.DocumentRepository, "delete", _delete_row)
        response = await client.delete(
            f"/api/v1/research/documents/{document['id']}", headers=headers)
        assert response.status_code == 204, response.text
        assert order == ["row", "file"]
        assert len(_STORAGE) == stored_before - 1
        assert ("document.deleted", {
            "document_id": document["id"]
        }) in research_doubles["published"]

        _, headers = _headers()
        document = await _upload(client, headers, "b.txt")

        async def _storage_down(settings, *, bucket, key):
            raise ConnectionError("storage down")

        monkeypatch.setattr(research_service, "delete_object", _storage_down)
        response = await client.delete(
            f"/api/v1/research/documents/{document['id']}", headers=headers)
        assert response.status_code == 204
        assert (
            await client.get(
                f"/api/v1/research/documents/{document['id']}",
                headers=headers)).status_code == 404


class TestDocumentIngestion:

    async def test_duplicate_of_a_stuck_upload_republishes(
            self, client, db_session, monkeypatch):
        import outbox
        import domains.research.service as research_service
        from domains.research.model import Document

        monkeypatch.setattr(
            research_service, "upload_bytes", _fake_upload_bytes)
        published = []

        async def _fake_publish(settings, event_name, payload, *, log=True):
            published.append(payload["document_id"])

        monkeypatch.setattr(outbox, "publish_durable_event", _fake_publish)
        for age_minutes, status, republished in ((30, "uploaded", True),
                                                 (30, "embedding", False),
                                                 (90, "embedding",
                                                  True), (5, "parsing", False),
                                                 (90, "ready", False)):
            case = f"{status} for {age_minutes} minutes"
            published.clear()
            _, headers = _headers()
            kwargs = {
                "files": {
                    "file": (
                        f"{uuid4().hex}.pdf", b"%PDF-1.4 x", "application/pdf")
                },
                "data": {
                    "category": "filing",
                    "ticker": "AAPL"
                },
                "headers": headers
            }
            first = await client.post("/api/v1/research/documents", **kwargs)
            assert first.status_code == 201, case
            document = await db_session.get(
                Document, UUID(first.json()["data"]["id"]))
            document.uploaded_at = datetime.now(UTC) - timedelta(hours=3)
            document.status_changed_at = datetime.now(UTC) - timedelta(
                minutes=age_minutes)
            document.status = status
            await db_session.flush()
            second = await client.post("/api/v1/research/documents", **kwargs)
            assert len(published) == (2 if republished else 1), case
            if not republished:
                assert second.status_code == 409, case
                assert second.json()["error"]["code"] == "conflict", case
                continue
            assert second.status_code == 201, case
            first_id = first.json()["data"]["id"]
            assert second.json()["data"]["id"] == first_id, case

    async def test_hide_and_repository_rules(
            self, db_session, mock_cross_service, client, monkeypatch):
        from domains.research.model import ResearchComment, ResearchNote
        from domains.research.service import ResearchService
        from shared.security import CurrentUser

        note = ResearchNote(author_id=uuid4(), title="t", content="c")
        db_session.add(note)
        await db_session.flush()
        comment = ResearchComment(
            note_id=note.id, author_id=uuid4(), content="x")
        db_session.add(comment)
        await db_session.flush()
        moderator = CurrentUser(uuid4(), "moderator")
        service = ResearchService(db_session)

        first = await service.set_note_hidden(note.id, moderator, hidden=True)
        again = await service.set_note_hidden(note.id, moderator, hidden=True)
        assert first.state_changed and not again.state_changed
        assert again.note.hidden_at == first.note.hidden_at

        c_first = await service.set_comment_hidden(
            comment.id, moderator, hidden=True)
        stamp = comment.hidden_at
        c_again = await service.set_comment_hidden(
            comment.id, moderator, hidden=True)
        assert c_first.state_changed and not c_again.state_changed
        assert stamp is not None and comment.hidden_at == stamp

        from domains.research.repository import DocumentRepository

        ticker = f"ZZ{uuid4().hex[:6].upper()}"

        repo = DocumentRepository(db_session)
        await repo.create(
            owner_id=uuid4(), organization_id=None,
            filename=f"{ticker}_10-K_2026-01-01.txt", document_type="txt",
            category="filing", ticker=ticker, storage_url="documents/x/1.txt")
        await repo.create(
            owner_id=uuid4(), organization_id=None,
            filename=f"{ticker}_news_2026-01-01.txt", document_type="txt",
            category="news", ticker=ticker, storage_url="documents/x/2.txt")
        await repo.create(
            owner_id=uuid4(), organization_id=None,
            filename="MSFT_10-K_2026-01-01.txt", document_type="txt",
            category="filing", ticker="MSFT", storage_url="documents/x/3.txt")
        await db_session.commit()

        filenames = await repo.list_filenames(ticker=ticker, category="filing")
        assert filenames == [f"{ticker}_10-K_2026-01-01.txt"]
        stuck = await repo.create(
            owner_id=uuid4(), organization_id=None,
            filename=f"{ticker}_10-Q_2026-01-01.txt", document_type="txt",
            category="filing", ticker=ticker, storage_url="documents/x/4.txt")
        stuck.uploaded_at = datetime.now(UTC) - timedelta(hours=3)
        await db_session.flush()
        changed, applied = await repo.transition_status(stuck.id, "embedding")
        assert applied and changed is stuck
        assert datetime.now(UTC) - stuck.status_changed_at < timedelta(
            minutes=1)
        filenames = await repo.list_filenames(ticker=ticker, category="filing")
        assert sorted(filenames) == [
            f"{ticker}_10-K_2026-01-01.txt", f"{ticker}_10-Q_2026-01-01.txt"
        ]
        stuck.status_changed_at = datetime.now(UTC) - timedelta(hours=2)
        await db_session.flush()
        filenames = await repo.list_filenames(ticker=ticker, category="filing")
        assert filenames == [f"{ticker}_10-K_2026-01-01.txt"]
        stuck.status = "ready"
        await db_session.flush()
        filenames = await repo.list_filenames(ticker=ticker, category="filing")
        assert sorted(filenames) == [
            f"{ticker}_10-K_2026-01-01.txt", f"{ticker}_10-Q_2026-01-01.txt"
        ]

        from domains.research.repository import (_DOCUMENT_STATUS_RANK,
                                                 is_forward_status_transition)
        from shared.enums import DocumentStatus

        assert set(_DOCUMENT_STATUS_RANK) == {s.value for s in DocumentStatus}
        assert is_forward_status_transition("uploaded", "parsing")
        assert is_forward_status_transition("failed", "parsing")
        assert not is_forward_status_transition("ready", "parsing")
        assert not is_forward_status_transition("parsing", "unknown")

        from shared.schemas import SYSTEM_DOCUMENT_OWNER_ID

        async def _fake_upload_bytes(*args, **kwargs):
            return None

        import domains.research.service as research_service_module

        monkeypatch.setattr(
            research_service_module, "upload_bytes", _fake_upload_bytes)

        resp = await client.post(
            "/internal/documents", json={
                "filename": "AMD_10-K_2026-08-04.txt",
                "content": (
                    "Real filing text content, well over the length floor."),
                "category": "filing",
                "ticker": "AMD"
            })
        assert resp.status_code == 200
        body = resp.json()
        assert body["filename"] == "AMD_10-K_2026-08-04.txt"

        import uuid as uuid_module

        document = await DocumentRepository(db_session).get_by_id(
            uuid_module.UUID(body["id"]))
        assert document is not None
        assert document.owner_id == SYSTEM_DOCUMENT_OWNER_ID
        assert document.status == "uploaded"
        assert document.ticker == "AMD"

    async def test_delete_documents_script(self, db_session):
        from scripts.delete_documents import parse_ids

        a = str(uuid4())
        b = str(uuid4())
        assert parse_ids([a, f" {b} ", a]) == [UUID(a), UUID(b)]
        with pytest.raises(ValueError):
            parse_ids(["not-a-uuid"])

        from scripts.delete_documents import build_plan

        doc = _document()
        db_session.add(doc)
        await db_session.flush()
        missing = uuid4()

        plan = await build_plan(db_session, [doc.id, missing])

        assert [d.id for d in plan.documents] == [doc.id]
        assert plan.not_found == [missing]

        from scripts.delete_documents import apply_plan, build_plan
        from domains.research.model import Document

        failing = _document()
        ok = _document()
        db_session.add_all([failing, ok])
        await db_session.commit()
        failing_id, ok_id = failing.id, ok.id

        recorder = _DeletionRecorder(fail_delete_for=failing.storage_url)
        plan = await build_plan(db_session, [failing_id, ok_id])
        result = await apply_plan(
            db_session, plan, delete_object_fn=recorder.delete_object,
            publish_fn=recorder.publish)

        assert result.deleted == [failing_id, ok_id]
        assert [d for d, _ in result.failed] == [failing_id]
        assert "orphan object" in result.failed[0][1]
        assert [payload for _, payload in recorder.published] == [{
            "document_id": str(failing_id)
        }, {
            "document_id": str(ok_id)
        }]
        assert await db_session.get(Document, failing_id) is None
        assert await db_session.get(Document, ok_id) is None


class TestSentiment:

    async def test_client_contract_and_summary(
            self, monkeypatch, client, db_session, mock_cross_service):
        import clients.ai_client as client_module

        calls: list[dict] = []
        responses = iter([
            _SentimentResponse(
                200, {
                    "label": "Positive",
                    "score": 0.8,
                    "model": "lite"
                }),
            _SentimentResponse(503, {"detail": "down"}),
            _SentimentResponse(
                200, {
                    "label": "great",
                    "score": 0.8,
                    "model": "lite"
                }),
            _SentimentResponse(
                200, {
                    "label": "negative",
                    "score": 3.0,
                    "model": "lite"
                })
        ])

        class _Client:

            async def post(self, url, json):
                calls.append({"url": url, "json": json})
                return next(responses)

        timeouts: list[float] = []

        def _get_client(timeout):
            timeouts.append(timeout)
            return _Client()

        monkeypatch.setattr(client_module, "get_http_client", _get_client)
        ok = await client_module.analyze_sentiment("Strong quarter", "note")
        assert ok == client_module.SentimentResult("positive", 0.8, "lite")
        assert calls[0]["url"].endswith("/internal/sentiment")
        assert calls[0]["json"] == {"text": "Strong quarter", "kind": "note"}
        assert await client_module.analyze_sentiment("x", "comment") is None
        assert await client_module.analyze_sentiment("x", "comment") is None
        assert await client_module.analyze_sentiment("x", "comment") is None
        assert await client_module.analyze_sentiment("   ", "note") is None
        assert len(calls) == 4
        assert set(timeouts) == {client_module._SENTIMENT_TIMEOUT}
        assert client_module._SENTIMENT_TIMEOUT > 240.0

        import clients.auth_client as auth_client
        from domains.research.model import (NoteTag, ResearchComment,
                                            ResearchNote, ResearchTag)

        me, headers = _headers()
        _, moderator = _headers(role="moderator")
        org_id = uuid4()

        async def _ids(user_id):
            return [org_id]

        monkeypatch.setattr(auth_client, "list_user_organization_ids", _ids)
        day1 = datetime(2026, 9, 1, 12, tzinfo=UTC)
        day2 = datetime(2026, 9, 2, 12, tzinfo=UTC)

        def _note(label, score, created, **kw):
            return ResearchNote(
                author_id=UUID(me), title=kw.pop("title", "NVDA"),
                content=kw.pop("content", "text"), created_at=created,
                sentiment_label=label, sentiment_score=score, **kw)

        tagged = _note(
            "positive", 0.8, day1, title="Chips", content="no ticker words")
        mentions = _note("negative", -0.6, day2, content="NVDA guidance cut")
        hidden = _note("negative", -1.0, day2, is_hidden=True)
        unanalysed = _note(None, None, day2)
        other_ticker = _note(
            "positive", 1.0, day1, title="AMD", content="AMD only")
        stranger = ResearchNote(
            author_id=uuid4(), title="NVDA", content="NVDA", created_at=day1,
            sentiment_label="positive", sentiment_score=1.0)
        db_session.add_all(
            [tagged, mentions, hidden, unanalysed, other_ticker, stranger])
        await db_session.flush()
        tag = ResearchTag(name="nvda")
        db_session.add(tag)
        await db_session.flush()
        db_session.add(NoteTag(note_id=tagged.id, tag_id=tag.id))
        db_session.add(
            ResearchComment(
                note_id=tagged.id, author_id=uuid4(), content="nice",
                created_at=day2, sentiment_label="neutral",
                sentiment_score=0.0))
        db_session.add(
            ResearchComment(
                note_id=tagged.id, author_id=uuid4(), content="bad",
                created_at=day2, is_hidden=True, sentiment_label="negative",
                sentiment_score=-0.9))
        await db_session.flush()

        url = "/api/v1/research/sentiment/summary"
        body = (
            await client.get(url, params={"ticker": "nvda"},
                             headers=headers)).json()
        assert body["ticker"] == "NVDA"
        assert (body["total"], body["notes"], body["comments"]) == (4, 3, 1)
        assert body["counts"] == {"positive": 1, "neutral": 1, "negative": 2}
        assert body["average_score"] == pytest.approx(
            (0.8 - 0.6 + 0.0 - 1.0) / 4, abs=1e-4)
        assert [d["date"]
                for d in body["series"]] == ["2026-09-01", "2026-09-02"]
        assert body["series"][1]["counts"] == {
            "positive": 0,
            "neutral": 1,
            "negative": 2
        }
        assert "from" in body and "to" in body

        ranged = (
            await client.get(
                url, headers=headers, params={
                    "ticker": "NVDA",
                    "from": "2026-09-02",
                    "to": "2026-09-02"
                })).json()
        assert ranged["total"] == 3

        monkeypatch.setattr(auth_client, "list_user_organization_ids", _ids)
        mod_body = (
            await
            client.get(url, params={"ticker": "NVDA"},
                       headers=moderator)).json()
        assert mod_body["total"] == 0
        mine_as_mod = (
            await client.get(
                url, params={"ticker": "NVDA"},
                headers=_headers(me, role="moderator")[1])).json()
        assert mine_as_mod["total"] == 5

        everything = (await client.get(url, headers=headers)).json()
        assert everything["total"] == 5

        for bad in ({"from": "2026-09-03", "to": "2026-09-01"},
                    {"ticker": "N V"}, {"from": "not-a-date"}):
            assert (
                await client.get(url, params=bad,
                                 headers=headers)).status_code == 422

    async def test_refresh_is_single_flight_and_does_not_block(
            self, monkeypatch):
        import asyncio
        from domains.research import sentiment

        gate = asyncio.Event()
        calls: list[UUID] = []

        async def _refresh(item_id):
            calls.append(item_id)
            await gate.wait()
            return True

        monkeypatch.setattr(sentiment, "refresh_note_sentiment", _refresh)
        monkeypatch.setattr(sentiment, "refresh_comment_sentiment", _refresh)
        note_id, comment_id = uuid4(), uuid4()
        sentiment.schedule_note_sentiment(note_id)
        await sentiment.schedule_after_commit(
            sentiment.schedule_comment_sentiment, comment_id)
        await asyncio.sleep(0)
        for _ in range(5):
            sentiment.schedule_note_sentiment(note_id)
        await asyncio.sleep(0)
        assert calls == [note_id, comment_id]
        gate.set()
        await asyncio.gather(*sentiment._running.values())
        assert (calls.count(note_id), calls.count(comment_id)) == (2, 1)
        assert not sentiment._running and not sentiment._rerun

    async def test_refresh_skips_stale_result_and_failure(
            self, committed_sentiment_rows, monkeypatch):
        import clients.ai_client as client_module
        from database import SessionLocal
        from sqlalchemy import update
        from domains.research import sentiment
        from domains.research.model import ResearchNote

        note_id, _ = committed_sentiment_rows

        async def _edit_while_analysing(text, kind):
            async with SessionLocal() as session:
                await session.execute(
                    update(ResearchNote).where(
                        ResearchNote.id == note_id).values(
                            content="Edited meanwhile"))
                await session.commit()
            return client_module.SentimentResult("negative", -0.4, "m")

        monkeypatch.setattr(
            client_module, "analyze_sentiment", _edit_while_analysing)
        assert not await sentiment.refresh_note_sentiment(note_id)

        async def _unavailable(text, kind):
            return None

        monkeypatch.setattr(client_module, "analyze_sentiment", _unavailable)
        assert not await sentiment.refresh_note_sentiment(note_id)
        assert not await sentiment.refresh_note_sentiment(uuid4())
        async with SessionLocal() as session:
            assert (
                await session.get(ResearchNote,
                                  note_id)).sentiment_label is None

    async def test_backfill_plan_then_refresh_stores_result(
            self, committed_sentiment_rows, monkeypatch, capsys):
        from database import SessionLocal
        from scripts import backfill_sentiment

        note_id, comment_id = committed_sentiment_rows
        async with SessionLocal() as session:
            plan = await backfill_sentiment.build_plan(
                session, kind="all", limit=None)
        assert note_id in plan.note_ids and comment_id in plan.comment_ids
        assert plan.notes_missing >= 1 and plan.comments_missing >= 1

        from sqlalchemy import update
        from domains.research.model import ResearchComment, ResearchNote

        labelled_at = datetime.now(UTC)
        async with SessionLocal() as session:
            await session.execute(
                update(ResearchNote).where(ResearchNote.id == note_id).values(
                    sentiment_label="positive",
                    sentiment_analyzed_at=labelled_at,
                    updated_at=labelled_at - timedelta(minutes=1)))
            await session.execute(
                update(ResearchComment).where(
                    ResearchComment.id == comment_id).values(
                        sentiment_label="positive",
                        sentiment_analyzed_at=labelled_at, updated_at=None))
            await session.commit()
            current = await backfill_sentiment.build_plan(
                session, kind="all", limit=None)
            assert note_id not in current.note_ids
            assert comment_id not in current.comment_ids
            for model, row_id in ((ResearchNote, note_id),
                                  (ResearchComment, comment_id)):
                await session.execute(
                    update(model).where(model.id == row_id).values(
                        updated_at=labelled_at + timedelta(minutes=1)))
            await session.commit()
            stale = await backfill_sentiment.build_plan(
                session, kind="all", limit=None)
            assert note_id in stale.note_ids
            assert comment_id in stale.comment_ids

        only_mine = backfill_sentiment.Plan(
            note_ids=[note_id], comment_ids=[comment_id])
        now = [0.0]
        sleeps: list[float] = []

        async def _sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        async def _note(row_id):
            return True

        async def _comment(row_id):
            return False

        result = await backfill_sentiment.apply_plan(
            only_mine, rate_per_minute=30, refresh_note=_note,
            refresh_comment=_comment, sleep=_sleep, clock=lambda: now[0])
        assert result.stored == 1 and result.failed == [
            ("comment", comment_id)
        ]
        assert sleeps == [2.0]

        async def _boom(*args, **kwargs):
            raise AssertionError("preview must not call ai-service")

        async def _noop():
            return None

        class _Engine:

            async def dispose(self):
                return None

        monkeypatch.setattr(backfill_sentiment, "apply_plan", _boom)
        monkeypatch.setattr(backfill_sentiment, "engine", _Engine())
        monkeypatch.setattr(
            backfill_sentiment, "close_redis_connections", _noop)
        monkeypatch.setattr(backfill_sentiment, "close_http_clients", _noop)
        assert await backfill_sentiment.main(["--kind", "note"]) == 0
        out = capsys.readouterr().out
        assert "Preview only" in out
        assert "Notes without current sentiment" in out
        assert await backfill_sentiment.main(["--rate", "0"]) == 2

        import clients.ai_client as client_module
        from domains.research import sentiment
        from domains.research.model import ResearchComment, ResearchNote

        note_id, comment_id = committed_sentiment_rows
        seen: list[tuple[str, str]] = []

        async def _analyze(text, kind):
            seen.append((kind, text))
            return client_module.SentimentResult("positive", 0.6, "lite-model")

        monkeypatch.setattr(client_module, "analyze_sentiment", _analyze)
        async with SessionLocal() as session:
            before = (await session.get(ResearchNote, note_id)).updated_at

        assert await sentiment.refresh_note_sentiment(note_id)
        assert await sentiment.refresh_comment_sentiment(comment_id)
        assert seen == [("note", "AMD\n\nGreat margins"),
                        ("comment", "Agreed")]
        async with SessionLocal() as session:
            note = await session.get(ResearchNote, note_id)
            comment = await session.get(ResearchComment, comment_id)
        assert (note.sentiment_label,
                note.sentiment_score) == ("positive", 0.6)
        assert (
            note.sentiment_model == "lite-model"
            and note.sentiment_analyzed_at)
        assert note.updated_at == before
        assert comment.sentiment_label == "positive"
