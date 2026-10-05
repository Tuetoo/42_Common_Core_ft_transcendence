import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from uuid import uuid4
import httpx
import main
import pytest
from config import settings
from helpers import create_portfolio
from jose import jwt
from sqlalchemy import text
from scripts import e2e_check as e2e
from shared import user_state as user_state_module
from shared.config import get_settings
from shared.market_calendar import (is_trading_day, nyse_holidays,
                                    previous_trading_day)
from shared.redis import get_events_client
from shared.user_state import (UserState, get_user_state, store_user_state,
                               user_state_key)
from tests.alembic_contract import *  # noqa: F401,F403
from tests.alembic_contract import head, revision, upgrade_to
from tests.auth_tokens import auth_headers as _headers

_REPO = Path(__file__).resolve().parents[2]
INTERNAL_TOKEN = "internal-token-for-tests"
JWT = (
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwidHlwZSI6ImVtYWlsIn0"
    ".abc_DEF-123")

DEPENDENCIES = {
    "database": "check_database_connection",
    "redis": "check_redis_connection",
    "storage": "check_storage_connection"
}


def _patch_checks(monkeypatch, failing: str | None = None) -> None:
    for key, func_name in DEPENDENCIES.items():
        result = key != failing

        async def _check(*_args, _result=result, **_kwargs) -> bool:
            return _result

        monkeypatch.setattr(main, func_name, _check)


@pytest.fixture
def internal_token(monkeypatch):
    monkeypatch.setattr(get_settings(), "INTERNAL_API_TOKEN", INTERNAL_TOKEN)
    return INTERNAL_TOKEN


def _url() -> str:
    return f"/internal/portfolios/{uuid4()}/holdings"


def _capturing_client(seen: list[httpx.Request], payload):
    from shared.http_client import build_http_client

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload)

    return build_http_client(3.0, transport=httpx.MockTransport(handler))


def _raw_email(link: str, *, cte: str | None = None) -> str:
    message = EmailMessage()
    message["From"] = "noreply@alphamind.local"
    message["To"] = "e2e-abc123-alice@example.com"
    message["Subject"] = "Verify your AlphaMind account"
    message.set_content("Please confirm your email address.")
    body = f'<p><a href="{link.replace("&", "&amp;")}">Verify my email</a></p>'
    if cte:
        message.add_alternative(body, subtype="html", cte=cte)
    else:
        message.add_alternative(body, subtype="html")
    return message.as_string()


def _access_token_with_claims(**claims: Any) -> str:
    from config import settings

    now = datetime.now(UTC)
    body: dict[str, Any] = {
        "type": "access",
        "role": "user",
        "iat": now,
        "exp": now + timedelta(minutes=5)
    }
    body.update(claims)
    return jwt.encode(
        body, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


class _StubResponse:

    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeCache:

    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


@pytest.fixture
def alembic_schema() -> str:
    return "core"


async def _insert_document(engine, schema, *, owner, filename, ticker=None):
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f'INSERT INTO "{schema}".documents (owner_id, filename, '
                "document_type, category, ticker, storage_url, status) "
                "VALUES (:owner, :filename, 'txt', 'macro_research', "
                ":ticker, :url, 'ready')"), {
                    "owner": owner,
                    "filename": filename,
                    "ticker": ticker,
                    "url": f"documents/{uuid4()}.txt"
                })


class TestHealth:

    async def test_health_and_internal_owner_lookup(self, client, monkeypatch):
        _patch_checks(monkeypatch)
        response = await client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["service"]
        for key in DEPENDENCIES:
            assert body[key] is True

        response = await client.get(f"/internal/portfolios/{uuid4()}/owner")
        assert response.status_code == 404

        client.headers.pop("X-Internal-Token", None)
        monkeypatch.setattr(get_settings(), "INTERNAL_API_TOKEN", "")
        monkeypatch.setattr(get_settings(), "APP_ENV", "development")
        assert (await client.get(_url())).status_code == 200

    async def test_background_task_end_is_logged_and_clients_send_token(
            self, monkeypatch, internal_token):
        import asyncio
        from shared import events

        logged: list[dict] = []

        class _Logger:

            def error(self, event, **fields):
                logged.append({"event": event, "task": fields["task"]})

        monkeypatch.setattr(events, "logger", _Logger())

        async def _fails():
            raise ConnectionError("redis down")

        async def _sleeps():
            await asyncio.sleep(60)

        failed = main.start_background_task(_fails(), "durable")
        cancelled = main.start_background_task(_sleeps(), "cancelled")
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.gather(failed, cancelled, return_exceptions=True)
        await asyncio.sleep(0)
        assert logged == [{
            "event": "background_task_crashed",
            "task": "durable"
        }]

        from clients import ai_client, auth_client

        seen: list[httpx.Request] = []
        fake = _capturing_client(seen, {"connected": True})
        monkeypatch.setattr(
            auth_client, "get_http_client", lambda timeout=5.0: fake)
        assert await auth_client.are_users_connected(
            str(uuid4()), str(uuid4()))
        await auth_client.set_user_presence(str(uuid4()), online=True)

        seen_ai: list[httpx.Request] = []
        fake_ai = _capturing_client(seen_ai, {"rate": "0.9"})
        monkeypatch.setattr(
            ai_client, "get_http_client", lambda timeout=5.0: fake_ai)
        await ai_client.get_fx_rate("USD", "EUR")

        assert len(seen) == 2 and len(seen_ai) == 1
        for request in seen + seen_ai:
            assert request.url.path.startswith("/internal/")
            assert request.headers.get("X-Internal-Token") == INTERNAL_TOKEN
        await fake.aclose()
        await fake_ai.aclose()


class TestServiceClients:

    async def test_ai_client_routes_and_daily_closes(self, monkeypatch):
        import clients.ai_client as real

        seen: dict[str, object] = {}

        class _Response:

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "data": {
                        "id": str(uuid4()),
                        "ticker": "AMD",
                        "status": "pending",
                        "created_at": "2026-09-28T13:30:00Z"
                    }
                }

        class _PostOnlyHttp:

            async def post(self, url, json):
                seen["url"], seen["json"] = url, json
                return _Response()

        monkeypatch.setattr(
            real, "get_http_client", lambda timeout: _PostOnlyHttp())
        ref = await real.request_analysis("AMD")
        assert ref is not None and ref.ticker == "AMD"
        assert str(seen["url"]).endswith("/internal/analyses")
        assert seen["json"] == {"ticker": "AMD", "portfolio_id": None}

        seen = []
        rec = {
            "id": str(uuid4()),
            "analysis_id": str(uuid4()),
            "ticker": "AMD",
            "action": "buy",
            "confidence": 0.7,
            "created_at": "2026-09-29T10:00:00Z"
        }

        class _Http:

            async def get(self, url, params=None):
                seen.append(("GET", url, params))
                if "recommendations" in url:
                    return _StubResponse(rec)
                return _StubResponse({"data": [{"close": 12.5}]})

            async def post(self, url, json):
                seen.append(("POST", url, json))
                return _StubResponse({
                    "data": {
                        "id": str(uuid4()),
                        "ticker": "AMD",
                        "status": "pending",
                        "created_at": "2026-09-29T13:30:00Z"
                    }
                })

        cache = _FakeCache()
        monkeypatch.setattr(real, "get_http_client", lambda timeout: _Http())
        monkeypatch.setattr(real, "get_cache_client", lambda settings: cache)

        pid, owner = uuid4(), uuid4()
        ref = await real.get_latest_recommendation("AMD", pid)
        assert ref is not None and ref.ticker == "AMD"
        _, url, params = seen[-1]
        assert url.endswith("/internal/recommendations/latest")
        assert params == {"ticker": "AMD", "portfolio_id": str(pid)}
        assert any(str(pid) in k for k in cache.store)

        await real.request_analysis("AMD", pid, user_id=owner)
        assert seen[-1][2] == {
            "ticker": "AMD",
            "portfolio_id": str(pid),
            "user_id": str(owner)
        }

        from clients.ai_client import bar_session_date

        assert bar_session_date("2026-09-29T04:00:00Z") == date(2026, 9, 29)
        assert bar_session_date("2026-09-29T04:00:00+00:00") == date(
            2026, 9, 29)
        assert bar_session_date("2026-09-29T02:00:00") == date(2026, 9, 28)

        seen = []

        class _ClosesHttp:

            async def get(self, url, params=None):
                seen.append((url, params))
                return httpx.Response(
                    200, json={
                        "data": [{
                            "timestamp": "2026-09-29T04:00:00Z",
                            "close": "251.5"
                        }, {
                            "timestamp": "2026-09-28T04:00:00Z",
                            "close": "NaN"
                        }, {
                            "timestamp": "2026-09-26T04:00:00Z",
                            "close": 249
                        }, {
                            "close": "1"
                        }]
                    }, request=httpx.Request("GET", url))

        monkeypatch.setattr(
            real, "get_http_client", lambda timeout: _ClosesHttp())
        closes = await real.get_daily_closes("AMD", lookback_days=40)
        assert closes == {
            date(2026, 9, 29): Decimal("251.5"),
            date(2026, 9, 26): Decimal("249")
        }
        assert seen[-1][0].endswith("/internal/market/instruments/AMD/prices")
        assert seen[-1][1] == {"interval": "1d", "lookback_days": 40}

        class _FailingHttp:

            async def get(self, url, params=None):
                raise httpx.ConnectError("down")

        monkeypatch.setattr(
            real, "get_http_client", lambda timeout: _FailingHttp())
        assert await real.get_daily_closes("AMD") is None

    async def test_clients_survive_a_redis_error(self, monkeypatch):
        import clients.ai_client as ai_client
        import httpx

        class _Down:

            async def get(self, key):
                raise ConnectionError("redis down")

            async def set(self, *args, **kwargs):
                raise ConnectionError("redis down")

        body = {
            "id": str(uuid4()),
            "analysis_id": str(uuid4()),
            "ticker": "AMD",
            "action": "buy",
            "confidence": 0.7,
            "created_at": datetime.now(UTC).isoformat()
        }

        class _Http:

            async def get(self, url, params=None):
                return httpx.Response(
                    200, json=body, request=httpx.Request("GET", url))

        monkeypatch.setattr(ai_client, "get_cache_client", lambda _s: _Down())
        monkeypatch.setattr(ai_client, "get_http_client", lambda _t: _Http())
        ref = await ai_client.get_latest_recommendation("AMD", uuid4())
        assert ref is not None and ref.ticker == "AMD"

        import clients.auth_client as auth_client

        user_id = str(uuid4())

        class _UnavailableCache:

            async def get(self, key):
                raise ConnectionError("redis down")

            async def set(self, *args, **kwargs):
                raise ConnectionError("redis down")

        class _Response:

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "id": user_id,
                    "username": "u",
                    "avatar_url": "https://example.com/a.svg",
                    "is_online": False
                }

        class _UserHttp:

            async def get(self, url, **kwargs):
                return _Response()

        monkeypatch.setattr(
            auth_client, "get_cache_client", lambda _s: _UnavailableCache())
        monkeypatch.setattr(
            auth_client, "get_http_client", lambda _t: _UserHttp())
        ref = await auth_client.get_user_ref(user_id)
        assert str(ref.id) == user_id

    async def test_missing_role_from_auth_fails_closed(
            self, monkeypatch, mock_cross_service):
        import clients.auth_client as auth_client

        org_id = uuid4()

        class _Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return [{"id": str(org_id), "name": "Old auth"}]

        class _Client:

            async def get(self, url):
                return _Response()

        monkeypatch.setattr(
            auth_client, "get_http_client", lambda timeout: _Client())
        roles = await auth_client.list_user_organization_roles(str(uuid4()))
        assert roles == {org_id: "viewer"}


class TestJobHealth:

    async def test_heartbeat_endpoint_rules(self, client, monkeypatch):
        import job_health

        stored = {}

        class _Cache:

            async def hset(self, key, field, value):
                stored[(key, field)] = value

        monkeypatch.setattr(
            job_health, "get_cache_client", lambda _s: _Cache())
        response = await client.post("/internal/scheduler/heartbeat/backup")
        assert response.status_code == 200, response.text
        value = job_health.JOB_LAST_SUCCESS.labels(
            scheduled_job="backup")._value.get()
        assert abs(value - datetime.now(UTC).timestamp()) < 60
        assert ("core:job_last_success", "backup") in stored

        response = await client.post("/internal/scheduler/heartbeat/not-a-job")
        assert response.status_code == 404

        class _Down:

            async def hset(self, *args):
                raise ConnectionError("redis down")

        monkeypatch.setattr(job_health, "get_cache_client", lambda _s: _Down())
        response = await client.post(
            "/internal/scheduler/heartbeat/rag-ingest")
        assert response.status_code == 200

    async def test_job_registry_baseline_alerts_and_skip_count(
            self, monkeypatch):
        import job_health

        assert job_health.JOB_LAST_SUCCESS._labelnames == ("scheduled_job", )

        class _Cache:

            async def hgetall(self, key):
                return {
                    b"market-close": b"1700000000.0",
                    b"unknown": b"1.0",
                    b"backup": b"garbage"
                }

        monkeypatch.setattr(
            job_health, "get_cache_client", lambda _s: _Cache())
        await job_health.load_job_health()
        gauge = job_health.JOB_LAST_SUCCESS
        assert (
            gauge.labels(
                scheduled_job="market-close")._value.get() == 1700000000.0)
        labels = {
            sample.labels.get("scheduled_job")
            for metric in gauge.collect()
            for sample in metric.samples
        }
        assert "unknown" not in labels

        from pathlib import Path

        repo = Path(__file__).resolve().parents[2]
        scheduler = repo / "infra" / "scheduler"
        reported = set()
        for script in scheduler.glob("*.sh"):
            for line in script.read_text().splitlines():
                if "heartbeat.sh" in line and not line.lstrip().startswith(
                        "#"):
                    parts = line.split("heartbeat.sh", 1)[1].split()
                    if parts and parts[0] != '"$job"':
                        reported.add(parts[0])
        call_job = (scheduler / "call-job.sh").read_text()
        for line in (scheduler / "crontab").read_text().splitlines():
            if "call-job.sh" in line and not line.lstrip().startswith("#"):
                job = line.split("call-job.sh", 1)[1].split()[0]
                assert f"    {job})" in call_job
                reported.add(job)
        assert reported == set(job_health.SCHEDULED_JOBS)

        hashes: dict[str, dict[str, str]] = {
            "core:job_last_success": {
                "backup": "1700000000.0"
            }
        }

        class _EmptyCache:

            async def get(self, key):
                return None

            async def hgetall(self, key):
                return {
                    k.encode(): v.encode()
                    for k, v in hashes.get(key, {}).items()
                }

            async def hsetnx(self, key, field, value):
                hashes.setdefault(key, {}).setdefault(field, value)

            async def hget(self, key, field):
                value = hashes.get(key, {}).get(field)
                return None if value is None else value.encode()

        def _values():
            return {
                sample.labels["scheduled_job"]: sample.value
                for metric in job_health.JOB_LAST_SUCCESS.collect()
                for sample in metric.samples
            }

        monkeypatch.setattr(
            job_health, "get_cache_client", lambda _s: _EmptyCache())
        await job_health.load_job_health(started_at=1800000000.0)
        values = _values()
        assert set(job_health.SCHEDULED_JOBS) <= set(values)
        assert values["backup"] == 1700000000.0
        assert values["rag-ingest"] == 1800000000.0
        assert "backup" not in hashes["core:job_tracked_since"]

        await job_health.load_job_health(started_at=1900000000.0)
        assert _values()["rag-ingest"] == 1800000000.0

        class _Down:

            async def get(self, key):
                raise ConnectionError("redis down")

            async def hgetall(self, key):
                raise ConnectionError("redis down")

        monkeypatch.setattr(job_health, "get_cache_client", lambda _s: _Down())
        await job_health.load_job_health(started_at=2000000000.0)
        assert _values()["backup"] == 2000000000.0

        import re

        repo = Path(__file__).resolve().parents[2]
        rules = (repo / "infra" / "prometheus" / "alert_rules.yml").read_text()
        covered: list[str] = []
        for pattern in re.findall(r'scheduled_job=~"([^"]+)"', rules):
            covered.extend(pattern.split("|"))
        assert sorted(covered) == sorted(job_health.SCHEDULED_JOBS)

        store: dict[str, str] = {}

        class _DictCache:

            async def set(self, key, value):
                store[key] = value

            async def get(self, key):
                return store.get(key)

            async def hgetall(self, key):
                return {}

        monkeypatch.setattr(
            job_health, "get_cache_client", lambda _s: _DictCache())
        await job_health.record_market_close_skipped(3)
        job_health.MARKET_CLOSE_SKIPPED.set(0)
        await job_health.load_job_health()
        assert job_health.MARKET_CLOSE_SKIPPED._value.get() == 3

        class _DownOnSet:

            async def set(self, *args):
                raise ConnectionError("redis down")

        monkeypatch.setattr(
            job_health, "get_cache_client", lambda _s: _DownOnSet())
        await job_health.record_market_close_skipped(2)
        assert job_health.MARKET_CLOSE_SKIPPED._value.get() == 2


class TestUserState:

    async def test_http_rejects_bad_claims_and_guest_cannot_write(
            self, client):
        for claims in ({"sub": "<ADMIN_USER_ID>"}, {}, {"sub": 7}):
            token = _access_token_with_claims(**claims)
            r = await client.get(
                "/api/v1/portfolios",
                headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 401, claims

        owner_id, owner = _headers()
        pid = await create_portfolio(client, owner)
        holding = (
            await client.post(
                f"/api/v1/portfolios/{pid}/holdings", json={
                    "ticker": "AAPL",
                    "quantity": "1",
                    "average_cost": "10"
                }, headers=owner)).json()["data"]
        wl = (
            await client.post(
                "/api/v1/watchlists", json={"name": "W"},
                headers=owner)).json()["data"]
        key = (
            await
            client.post("/api/v1/api-keys", json={"name": "k"},
                        headers=owner)).json()["data"]
        _, guest = _headers(owner_id, role="guest")
        now = datetime.now(UTC).isoformat()
        writes = [("post", "/api/v1/portfolios", {
            "name": "x"
        }), ("patch", f"/api/v1/portfolios/{pid}", {
            "name": "x"
        }), ("delete", f"/api/v1/portfolios/{pid}", None),
                  (
                      "post", f"/api/v1/portfolios/{pid}/holdings", {
                          "ticker": "MSFT",
                          "quantity": "1",
                          "average_cost": "1"
                      }),
                  (
                      "patch",
                      f"/api/v1/portfolios/{pid}/holdings/{holding['id']}", {
                          "quantity": "2"
                      }),
                  (
                      "delete",
                      f"/api/v1/portfolios/{pid}/holdings/{holding['id']}",
                      None),
                  (
                      "post", f"/api/v1/portfolios/{pid}/transactions", {
                          "ticker": "CASH",
                          "transaction_type": "deposit",
                          "quantity": "1",
                          "transaction_date": now
                      }), ("post", "/api/v1/watchlists", {
                          "name": "x"
                      }),
                  ("patch", f"/api/v1/watchlists/{wl['id']}", {
                      "name": "x"
                  }), ("delete", f"/api/v1/watchlists/{wl['id']}", None),
                  (
                      "post", f"/api/v1/watchlists/{wl['id']}/items", {
                          "ticker": "IBM"
                      }),
                  ("delete", f"/api/v1/watchlists/{wl['id']}/items/IBM", None),
                  ("post", "/api/v1/api-keys", {
                      "name": "x"
                  }),
                  ("patch", f"/api/v1/api-keys/{key['id']}", {
                      "name": "x"
                  }), ("delete", f"/api/v1/api-keys/{key['id']}", None)]
        for method, url, body in writes:
            kwargs = {"headers": guest}
            if body is not None:
                kwargs["json"] = body
            response = await getattr(client, method)(url, **kwargs)
            assert response.status_code == 403, (method, url, response.text)

        for url in (f"/api/v1/portfolios/{pid}",
                    f"/api/v1/portfolios/{pid}/holdings",
                    f"/api/v1/portfolios/{pid}/transactions",
                    f"/api/v1/portfolios/{pid}/transactions.csv",
                    "/api/v1/watchlists", "/api/v1/api-keys"):
            assert (
                await client.get(url, headers=guest)).status_code == 200, url

    async def test_websocket_subject_and_user_state_lookup(self, monkeypatch):
        from api.ws_common import authenticate

        assert authenticate(_access_token_with_claims(sub="admin")) is None
        assert authenticate(_access_token_with_claims()) is None
        user = str(uuid4())
        identity = authenticate(_access_token_with_claims(sub=user))
        assert identity is not None and identity.user_id == user

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(404)))
        monkeypatch.setattr(
            user_state_module, "get_http_client", lambda timeout: client)
        user_id = uuid4()
        assert await get_user_state(settings,
                                    user_id) == UserState("user", False)
        await get_events_client(settings).delete(user_state_key(user_id))

        user_id = uuid4()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={
                    "role": "user",
                    "is_active": True
                })

        real_get = get_events_client(settings).get

        async def _get_then_auth_writes(key):
            await store_user_state(settings, user_id, UserState("user", False))
            return None

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(
            user_state_module, "get_http_client", lambda timeout: client)
        redis = get_events_client(settings)
        monkeypatch.setattr(redis, "get", _get_then_auth_writes)
        await get_user_state(settings, user_id)
        monkeypatch.setattr(redis, "get", real_get)
        assert await get_user_state(settings,
                                    user_id) == UserState("user", False)
        await redis.delete(user_state_key(user_id))


class TestOwnershipRules:

    async def test_stranger_gets_404_and_org_reader_403(
            self, client, mock_cross_service, organization_roles):
        org_id, roles = organization_roles
        owner, owner_h = _headers()
        reader, reader_h = _headers()
        _, stranger_h = _headers()
        roles.update({owner: "member", reader: "member"})
        shared = (
            await client.post(
                "/api/v1/portfolios", headers=owner_h, json={
                    "name": "Shared",
                    "organization_id": str(org_id),
                    "visibility": "organization"
                })).json()["data"]
        private = (
            await client.post(
                "/api/v1/portfolios", headers=owner_h,
                json={"name": "Private"})).json()["data"]

        def rename(pid, headers):
            return client.patch(
                f"/api/v1/portfolios/{pid}", json={"name": "x"},
                headers=headers)

        assert (await rename(shared["id"], reader_h)).status_code == 403
        assert (await rename(shared["id"], stranger_h)).status_code == 404
        assert (await rename(private["id"], reader_h)).status_code == 404

        watchlist = (
            await client.post(
                "/api/v1/watchlists", json={"name": "W"},
                headers=owner_h)).json()["data"]
        assert (
            await client.delete(
                f"/api/v1/watchlists/{watchlist['id']}",
                headers=stranger_h)).status_code == 404


class TestSharedContracts:

    def test_shared_contracts(self, monkeypatch):
        assert is_trading_day(date(2021, 12, 31))
        assert date(2022, 1, 1) not in nyse_holidays(2022)

        assert is_trading_day(date(2026, 9, 29))
        assert not is_trading_day(date(2026, 10, 3))
        assert not is_trading_day(date(2026, 11, 26))
        assert previous_trading_day(date(2026, 11, 27)) == date(2026, 11, 25)
        assert previous_trading_day(date(2026, 9, 28)) == date(2026, 9, 25)

        import api.internal as internal

        class _FakeDateTime(datetime):

            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 11, 27, 3, 0, tzinfo=UTC)

        import shared.market_calendar as market_calendar

        monkeypatch.setattr(market_calendar, "datetime", _FakeDateTime)
        assert internal._market_today() == date(2026, 11, 26)

        from prometheus_client import Counter, Gauge
        from shared.metrics import get_or_create

        name = f"alphamind_test_{uuid4().hex[:8]}"
        first = get_or_create(Gauge, name, "doc", ("a", ))
        assert get_or_create(Gauge, name, "doc", ("a", )) is first
        counter = get_or_create(Counter, f"{name}_c", "doc")
        assert get_or_create(Counter, f"{name}_c", "doc") is counter

        import main

        paths = main.app.openapi()["paths"]
        assert not [p for p in paths if p.startswith("/internal")]

        from shared.enums import AgentRole, NotificationType, WorkflowStatus

        values = {t.value for t in NotificationType}
        assert "system" not in values and "transaction_deleted" not in values
        assert "cancelled" not in {s.value for s in WorkflowStatus}
        assert not {"supervisor", "judge", "reflection"
                    } & {r.value
                         for r in AgentRole}

        import clients.ai_client as ai_client
        from domains.research.repository import DocumentRepository

        assert not hasattr(ai_client, "get_current_price")
        assert not hasattr(DocumentRepository, "update_status")

        import domains.portfolio.ledger as ledger
        from shared.market_calendar import MARKET_TZ

        assert ledger.MARKET_TZ is MARKET_TZ is ai_client.MARKET_TZ

        conf = (_REPO / "infra" / "gateway" / "nginx.conf").read_text()
        assert "$proxy_add_x_forwarded_for" not in conf
        assert (
            conf.count("proxy_set_header X-Forwarded-For $remote_addr;") == 3)


class TestE2eCheckScript:

    def test_mail_pdf_csv_redaction_sections_and_stats(self):
        old = _raw_email(f"https://x/verify-email?token={JWT}old")
        new = _raw_email(f"https://x/verify-email?token={JWT}new")
        other = _raw_email(f"https://x/invitations/accept?token={JWT}inv")
        result = {
            "items": [{
                "Created": "2026-09-30T10:00:00Z",
                "Raw": {
                    "Data": old
                }
            }, {
                "Created": "2026-09-30T10:05:00Z",
                "Raw": {
                    "Data": other
                }
            }, {
                "Created": "2026-09-30T10:02:00Z",
                "Raw": {
                    "Data": new
                }
            }]
        }
        assert e2e.token_from_mailhog(result, "/verify-email") == JWT + "new"
        assert (
            e2e.token_from_mailhog(result,
                                   "/invitations/accept") == JWT + "inv")
        assert e2e.token_from_mailhog({"items": []}, "/verify-email") is None

        item = {
            "Created": "2026-09-30T10:00:00Z",
            "Content": {
                "Headers": {
                    "Content-Type": ["text/html; charset=utf-8"]
                },
                "Body": f'<a href="https://x/verify-email?token={JWT}">v</a>'
            }
        }
        assert (
            e2e.token_from_mailhog({"items": [item]}, "/verify-email") == JWT)

        pypdf = pytest.importorskip("pypdf")
        data = e2e.build_pdf(
            ["Hello (AMD) marker zq123quasar", "second \\ line"])
        assert data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")
        reader = pypdf.PdfReader(io.BytesIO(data))
        assert len(reader.pages) == 1
        text = reader.pages[0].extract_text()
        assert "zq123quasar" in text
        assert "(AMD)" in text
        xref_at = int(data.rsplit(b"startxref\n", 1)[1].split(b"\n", 1)[0])
        assert data[xref_at:].startswith(b"xref")

        header, rows = e2e.parse_csv(
            "id,transaction_type,quantity\n1,buy,10\n2,adjustment,5\n")
        assert header == ["id", "transaction_type", "quantity"]
        assert rows[1] == {
            "id": "2",
            "transaction_type": "adjustment",
            "quantity": "5"
        }
        assert e2e.parse_csv("") == ([], [])

        assert e2e.fmt_decimal(Decimal("100.000000")) == "100"
        assert e2e.fmt_decimal(Decimal("9480")) == "9480"
        assert e2e.fmt_decimal(Decimal("1.50")) == "1.5"

        text = (
            f'{{"access_token": "{JWT}", "refresh_token": "abc", '
            f'"key": "am_secret"}} Bearer {JWT} password=hunter22x')
        out = e2e.redact(text, ["hunter22x"])
        assert (
            JWT not in out and "am_secret" not in out
            and "hunter22x" not in out)
        assert '"refresh_token": "<redacted>"' in out

        sections, added = e2e.resolve_sections(["yjs"])
        assert sections == ["auth", "org", "yjs", "cleanup"]
        assert added == ["auth", "org"]
        everything, none_added = e2e.resolve_sections(None)
        assert everything == list(e2e.SECTIONS) and none_added == []
        with pytest.raises(ValueError):
            e2e.resolve_sections(["nope"])

        before = {
            "auth": {
                "users_total": 7,
                "users_deleted": 10
            },
            "core": {
                "notes": 4
            }
        }
        after = {
            "auth": {
                "users_total": 7,
                "users_deleted": 13
            },
            "core": {
                "notes": 5
            }
        }
        assert (
            e2e.stat_value(after, "auth", "users_ever") -
            e2e.stat_value(before, "auth", "users_ever") == 3)
        assert e2e.stat_value(after, "core", "notes") == 5
        assert (
            e2e.stat_value({"auth": {
                "users_total": 7
            }}, "auth", "users_ever") is None)
        assert e2e.stat_value({"core": None}, "core", "notes") is None

    async def test_replay_ledger_yclient_peers_and_run_lock(self):
        rows = [{
            "transaction_date": "2026-09-30T10:00:00+00:00",
            "transaction_type": "deposit",
            "ticker": "CASH",
            "quantity": "10000.000",
            "price": None
        }, {
            "transaction_date": "2026-09-30T10:00:01+00:00",
            "transaction_type": "buy",
            "ticker": "AMD",
            "quantity": "10",
            "price": "100"
        }, {
            "transaction_date": "2026-09-30T10:00:02+00:00",
            "transaction_type": "adjustment",
            "ticker": "AMD",
            "quantity": "5",
            "price": "130"
        }, {
            "transaction_date": "2026-09-30T10:00:03+00:00",
            "transaction_type": "sell",
            "ticker": "AMD",
            "quantity": "4",
            "price": "120"
        }, {
            "transaction_date": "2026-09-30T10:00:04+00:00",
            "transaction_type": "adjustment",
            "ticker": "AMD",
            "quantity": "-1",
            "price": None
        }, {
            "transaction_date": "2026-09-30T10:00:05+00:00",
            "transaction_type": "buy",
            "ticker": "NVDA",
            "quantity": "1",
            "price": "50"
        }, {
            "transaction_date": "2026-09-30T10:00:06+00:00",
            "transaction_type": "sell",
            "ticker": "NVDA",
            "quantity": "1",
            "price": "60"
        }]
        holdings, cash = e2e.replay_ledger(reversed(rows))
        assert cash == Decimal("10000") - 1000 + 480 - 50 + 60
        assert set(holdings) == {"AMD"}
        qty, cost = holdings["AMD"]
        assert qty == 10
        assert cost == Decimal(10 * 100 + 5 * 130) / 15

        import y_py as Y

        seed_doc = Y.YDoc()
        seed_text = seed_doc.get_text("content")
        seed_doc.transact(lambda txn: seed_text.extend(txn, "body"))
        seed = bytes(Y.encode_state_as_update(seed_doc))

        alice, bob = e2e.YClient(), e2e.YClient()
        alice.apply(seed)
        bob.apply(seed)
        bob.apply(alice.append(" [alice]"))
        alice.apply(bob.append(" [bob]"))
        assert alice.value() == bob.value() == "body [alice] [bob]"
        with pytest.raises(Exception):
            e2e.YClient().apply(
                b"\x00\xff\xff\xff\xff\xff\xff\xff\xff\x0fgarbage")

        from config import settings
        from shared import e2e_lock

        await e2e_lock.release(settings, "nobody")
        assert await e2e_lock.acquire(settings, "run-a") is None
        assert await e2e_lock.acquire(settings, "run-b") == "run-a"
        await e2e_lock.release(settings, "run-b")
        assert await e2e_lock.acquire(settings, "run-c") == "run-a"
        assert (
            await e2e_lock.acquire(settings, "run-c", break_lock=True) is None)
        await e2e_lock.release(settings, "run-c")
        assert await e2e_lock.acquire(settings, "run-d") is None
        await e2e_lock.release(settings, "run-d")

    def test_admin_token_help_and_main_refusals(self, monkeypatch, capsys):
        assert "_create_token('<ADMIN_USER_ID>', timedelta(hours=2)" in (
            e2e.ADMIN_TOKEN_HELP.replace("\\\n", ""))
        assert "E2E_ADMIN_TOKEN" in e2e.ADMIN_TOKEN_HELP

        monkeypatch.delenv("E2E_ADMIN_TOKEN", raising=False)
        assert e2e.main(["--only", "auth"]) == 2
        assert "E2E_ADMIN_TOKEN" in capsys.readouterr().err
        assert e2e.main(["--only", "bogus"]) == 2

        from shared import e2e_lock

        async def _held(settings, holder, *, break_lock=False):
            return "e2e_check abc123 2026-09-30T01:00:00+00:00"

        async def _usable(self):
            return None

        monkeypatch.setattr(e2e.E2E, "admin_token_problem", _usable)
        monkeypatch.setattr(e2e_lock, "acquire", _held)
        monkeypatch.setenv("E2E_ADMIN_TOKEN", "t")
        assert e2e.main(["--only", "platform"]) == 2
        err = capsys.readouterr().err
        assert "another end-to-end run is in progress (e2e_check abc123" in err
        assert "--break-lock" in err

        async def _expired(self):
            return "GET /api/v1/auth/me -> 401"

        async def _no_lock(*args, **kwargs):
            raise AssertionError("the lock must not be taken")

        monkeypatch.setattr(e2e.E2E, "admin_token_problem", _expired)
        monkeypatch.setattr(e2e_lock, "acquire", _no_lock)
        monkeypatch.setenv("E2E_ADMIN_TOKEN", "expired")
        assert e2e.main(["--only", "auth"]) == 2
        err = capsys.readouterr().err
        assert (
            "E2E_ADMIN_TOKEN is not usable: GET /api/v1/auth/me -> 401" in err)
        assert "nothing was created" in err


class TestMigrations:

    async def test_status_changed_at_is_filled_from_uploaded_at(self, scratch):
        schema, engine, _ = scratch
        await upgrade_to(engine, schema, "f1200c000001")
        await _insert_document(
            engine, schema, owner=uuid4(), filename="old.txt")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f'UPDATE "{schema}".documents '
                    "SET uploaded_at = now() - interval '3 days'"))
        await upgrade_to(engine, schema, "f1250c000001")
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT uploaded_at = status_changed_at FROM "
                        f'"{schema}".documents'))).one()
        assert row[0] is True

    async def test_upgrade_refuses_duplicate_documents_then_succeeds(
            self, scratch):
        schema, engine, conversion = scratch
        await upgrade_to(engine, schema, conversion.baseline)
        owner = uuid4()
        await _insert_document(
            engine, schema, owner=owner, filename="fomc.txt")
        await _insert_document(
            engine, schema, owner=owner, filename="fomc.txt")

        with pytest.raises(Exception, match=r"fomc\.txt.*2 copies"):
            await upgrade_to(engine, schema, "head")
        assert await revision(engine, schema) == conversion.baseline

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f'DELETE FROM "{schema}".documents WHERE id = '
                    f'(SELECT id FROM "{schema}".documents '
                    "WHERE filename = 'fomc.txt' LIMIT 1)"))
        await upgrade_to(engine, schema, "head")
        assert await revision(engine, schema) == head()
        with pytest.raises(
                Exception,
                match="uq_documents_owner_ticker_category_filename"):
            await _insert_document(
                engine, schema, owner=owner, filename="fomc.txt")
