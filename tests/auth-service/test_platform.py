from uuid import uuid4
import pytest
from helpers import (PASSWORD, enable_2fa, login_response, make_user,
                     next_code, register, register_and_login,
                     reset_rate_limits)
from pydantic import ValidationError
from shared.config import BaseServiceSettings, get_settings
from tests.alembic_contract import *  # noqa: F401,F403

ORGS = "/api/v1/organizations"
INTERNAL_TOKEN = "internal-token-for-tests"
STRONG_JWT = "x" * 40


@pytest.fixture
def alembic_schema() -> str:
    return "auth"


@pytest.fixture
def small_limits(monkeypatch):
    from config import settings

    for name, value in {"RATE_LIMIT_LOGIN_PER_EMAIL": 3,
                        "RATE_LIMIT_REGISTER_PER_IP": 2,
                        "RATE_LIMIT_PASSWORD_RESET_PER_EMAIL": 2,
                        "RATE_LIMIT_RESEND_VERIFICATION_PER_EMAIL": 2,
                        "RATE_LIMIT_INVITATIONS_PER_ORG": 2,
                        "RATE_LIMIT_2FA_FAILURES": 3}.items():
        monkeypatch.setattr(settings, name, value)


class TestInternalToken:

    async def test_internal_token_and_jwt_secret_rules(
            self, client, monkeypatch):
        client.headers.pop("X-Internal-Token", None)
        monkeypatch.setattr(get_settings(), "INTERNAL_API_TOKEN", "")
        for app_env, status in (("development", 404), ("production", 401)):
            monkeypatch.setattr(get_settings(), "APP_ENV", app_env)
            r = await client.get(f"/internal/users/{uuid4()}")
            assert r.status_code == status, app_env

        with pytest.raises(ValidationError, match="INTERNAL_API_TOKEN"):
            BaseServiceSettings(
                APP_ENV="production", JWT_SECRET=STRONG_JWT,
                INTERNAL_API_TOKEN="")
        with pytest.raises(ValidationError, match="INTERNAL_API_TOKEN"):
            BaseServiceSettings(
                APP_ENV="staging", JWT_SECRET=STRONG_JWT,
                INTERNAL_API_TOKEN="")

        s = BaseServiceSettings(
            APP_ENV="production", JWT_SECRET=STRONG_JWT,
            INTERNAL_API_TOKEN=INTERNAL_TOKEN)
        assert s.INTERNAL_API_TOKEN == INTERNAL_TOKEN
        assert BaseServiceSettings(
            APP_ENV="development", INTERNAL_API_TOKEN="")
        assert BaseServiceSettings(APP_ENV="test", INTERNAL_API_TOKEN="")

        for bad in ("CHANGE_ME", "", "short-secret"):
            with pytest.raises(ValueError):
                BaseServiceSettings(APP_ENV="production", JWT_SECRET=bad)
        BaseServiceSettings(APP_ENV="development", JWT_SECRET="CHANGE_ME")
        BaseServiceSettings(APP_ENV="staging", JWT_SECRET="x" * 32)


class TestHttpSurface:

    async def test_request_ids_and_hidden_internal_routes(
            self, client, mock_email):
        fresh = await client.get("/health")
        assert len(fresh.headers["x-request-id"]) == 32
        echoed = await client.get(
            "/health", headers={"X-Request-ID": "gw-123_ab.c"})
        assert echoed.headers["x-request-id"] == "gw-123_ab.c"
        unsafe = await client.get(
            "/health", headers={"X-Request-ID": "bad id\r\nx"})
        assert unsafe.headers["x-request-id"] != "bad id\r\nx"

        headers, _ = await register_and_login(
            client, mock_email, "trace@example.com", "trace_user")
        r = await client.get(
            "/api/v1/auth/me", headers={
                **headers, "X-Request-ID": "trace-1"
            })
        assert r.status_code == 200
        assert r.json()["request_id"] == "trace-1"
        r = await client.get("/api/v1/auth/me", headers=headers)
        assert r.json()["request_id"] == r.headers["x-request-id"]
        bad = await client.post(
            "/api/v1/auth/login", json={
                "email": "trace@example.com",
                "password": "wrong-pass-x1"
            }, headers={"X-Request-ID": "trace-2"})
        assert bad.status_code == 401
        assert bad.json()["error"]["request_id"] == "trace-2"

        spec = (await client.get("/api/openapi-auth.json")).json()
        assert not [p for p in spec["paths"] if p.startswith("/internal")]


class TestRateLimits:

    async def test_login_two_factor_and_register_limits(
            self, client, mock_email, small_limits, monkeypatch):
        await register(
            client, mock_email, "limit_login@example.com", "limit_login")
        for _ in range(3):
            await login_response(
                client, "limit_login@example.com", password="wrong-password-x")
        r = await login_response(client, "limit_login@example.com")
        assert r.status_code == 429
        assert int(r.headers["Retry-After"]) > 0
        r = await login_response(client, "LIMIT_LOGIN@Example.com")
        assert r.status_code == 429

        await reset_rate_limits()
        from config import settings

        monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_PER_EMAIL", 10)
        headers, _ = await register_and_login(
            client, mock_email, "lockout@example.com", "lockout")
        secret = await enable_2fa(client, headers)
        for _ in range(3):
            r = await login_response(
                client, "lockout@example.com", totp_code="000000")
            assert r.status_code == 401
        locked = await login_response(
            client, "lockout@example.com", totp_code=next_code(secret))
        assert locked.status_code == 429 and "Retry-After" in locked.headers

        await reset_rate_limits()
        for i in range(2):
            r = await client.post(
                "/api/v1/auth/register", json={
                    "email": f"limit_reg{i}@example.com",
                    "username": f"limit_reg{i}",
                    "password": PASSWORD
                })
            assert r.status_code == 201
        r = await client.post(
            "/api/v1/auth/register", json={
                "email": "limit_reg9@example.com",
                "username": "limit_reg9",
                "password": PASSWORD
            })
        assert r.status_code == 429

    async def test_email_organization_and_friend_request_limits(
            self, client, mock_email, small_limits, db_session, monkeypatch):
        for url, email, last_email in (("/api/v1/auth/password-reset/request",
                                        "x@example.com", "X@example.com"),
                                       ("/api/v1/auth/verify-email/resend",
                                        "y@example.com", "y@example.com")):
            for _ in range(2):
                r = await client.post(url, json={"email": email})
                assert r.status_code == 200, url
            r = await client.post(url, json={"email": last_email})
            assert r.status_code == 429, url

        await reset_rate_limits()
        headers, _ = await register_and_login(
            client, mock_email, "limit_org@example.com", "limit_org")
        created = await client.post(ORGS, json={"name": "RL"}, headers=headers)
        org = created.json()["data"]
        codes = []
        for i in range(3):
            r = await client.post(
                f"{ORGS}/{org['id']}/invitations",
                json={"email": f"inv{i}@example.com"}, headers=headers)
            codes.append(r.status_code)
        assert codes == [201, 201, 429]

        await reset_rate_limits()
        from config import settings

        monkeypatch.setattr(settings, "RATE_LIMIT_REGISTER_PER_IP", 10)
        sender = await make_user(client, mock_email, db_session)
        others = [
            await make_user(client, mock_email, db_session) for _ in range(3)
        ]
        monkeypatch.setattr(settings, "RATE_LIMIT_FRIEND_REQUESTS_PER_USER", 2)
        codes = []
        for other in others:
            r = await client.post(
                "/api/v1/connections", headers=sender["headers"],
                json={"addressee_username": other["username"]})
            codes.append(r.status_code)
        assert codes == [201, 201, 429]
        r = await client.post(
            "/api/v1/connections", headers=sender["headers"],
            json={"addressee_username": "nobody-at-all"})
        assert r.status_code == 429
        body = r.json()
        assert body["error"]["message"] == (
            "Too many attempts. Please try again later.")


class TestSharedErrors:

    async def test_unavailable_error_uses_the_envelope_and_retry_after(self):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from shared.exceptions import (ServiceUnavailableError,
                                       register_exception_handlers)

        class _Busy(ServiceUnavailableError):
            code = "busy"
            retry_after_seconds = 30

        app = FastAPI()
        register_exception_handlers(app)

        @app.get("/busy")
        async def _busy() -> None:
            raise _Busy("try later")

        @app.get("/down")
        async def _down() -> None:
            raise ServiceUnavailableError()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            busy = await c.get("/busy")
            down = await c.get("/down")
        assert busy.status_code == 503
        assert busy.headers["Retry-After"] == "30"
        assert busy.json()["error"]["code"] == "busy"
        assert busy.json()["error"]["message"] == "try later"
        assert down.status_code == 503
        assert "Retry-After" not in down.headers
        assert down.json()["error"]["code"] == "service_unavailable"


class TestSharedLogging:

    def test_uvicorn_lines_lose_their_query_string(self):
        import logging
        from shared.logging import install_query_string_filter

        install_query_string_filter()
        install_query_string_filter()
        records = []

        class _Keep(logging.Handler):

            def emit(self, record):
                records.append(record.getMessage())

        handler = _Keep()
        access = logging.getLogger("uvicorn.access")
        error = logging.getLogger("uvicorn.error")
        for target in (access, error):
            target.addHandler(handler)
            target.setLevel(logging.INFO)
        try:
            access.info(
                '%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET",
                "/api/v1/auth/oauth/google/callback?code=c&state=s", "1.1",
                200)
            error.info(
                '%s - "WebSocket %s" [accepted]', "1.2.3.4:5",
                "/ws/notifications?token=eyJsecret")
            error.info("Started server process [%d]", 7)
        finally:
            for target in (access, error):
                target.removeHandler(handler)
        assert records == [
            '1.2.3.4:5 - "GET /api/v1/auth/oauth/google/callback'
            '?[redacted] HTTP/1.1" 200',
            '1.2.3.4:5 - "WebSocket /ws/notifications?[redacted]" '
            "[accepted]", "Started server process [7]"
        ]
        assert sum(
            type(f).__name__ == "QueryStringFilter"
            for f in access.filters) == 1


class TestSharedDurableEvents:

    async def _wait_for(self, received, expected):
        import asyncio

        for _ in range(100):
            if received == expected:
                return
            await asyncio.sleep(0.05)

    async def test_recovery_and_unexpected_task_end(self, monkeypatch):
        import asyncio
        from config import settings
        import shared.events as events
        from shared.redis import get_queue_client

        monkeypatch.setattr(events, "RECONNECT_BACKOFF_SECONDS", (0.05, ))
        name = f"test.recover.{uuid4().hex[:8]}"
        real = get_queue_client(settings)
        calls = {"n": 0}

        class _FlakyOnce:

            def __getattr__(self, attr):
                return getattr(real, attr)

            async def xgroup_create(self, *args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ConnectionError("redis restarting")
                return await real.xgroup_create(*args, **kwargs)

        monkeypatch.setattr(
            events, "get_queue_client", lambda _s: _FlakyOnce())
        received = []

        async def handler(payload):
            received.append(payload["n"])

        subscriber = events.DurableEventSubscriber(settings, "t-worker")
        subscriber.on(name, handler)
        task = events.start_background_task(subscriber.run(), "t-durable")
        try:
            await events.publish_durable_event(settings, name, {"n": 1})
            await self._wait_for(received, [1])
            assert received == [1]
            assert calls["n"] >= 2

            await real.delete(f"stream:{name}")
            await events.publish_durable_event(settings, name, {"n": 2})
            await self._wait_for(received, [1, 2])
            assert received == [1, 2]
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await real.delete(f"stream:{name}")

        logged = []
        monkeypatch.setattr(
            events.logger, "error", lambda event, **kw: logged.append(
                (event, kw["task"])))

        async def _done():
            return None

        task = events.start_background_task(_done(), "done")
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        assert logged == [("background_task_ended", "done")]
