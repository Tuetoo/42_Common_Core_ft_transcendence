import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlparse
from uuid import UUID, uuid4
import httpx
import pyotp
import pytest
from helpers import (PASSWORD, current_user_id, enable_2fa, login,
                     login_response, make_user, next_code, register,
                     register_and_login, reset_rate_limits)
from jose import jwt

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def presence_event(user_id, online):
    return ("user.presence.changed", {"user_id": user_id, "is_online": online})


def signed_token(**claims: Any) -> str:
    from config import settings

    now = datetime.now(UTC)
    body: dict[str, Any] = {
        "type": "access",
        "role": "admin",
        "iat": now,
        "exp": now + timedelta(minutes=5)
    }
    body.update(claims)
    return jwt.encode(
        body, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


async def two_factor_row(db_session, user_id):
    from sqlalchemy import select
    from domains.auth.model import TwoFactorSecret

    result = await db_session.execute(
        select(TwoFactorSecret).where(
            TwoFactorSecret.user_id == UUID(user_id)))
    return result.scalar_one()


async def user_with_plain_text_secret(client, mock_email, db_session):
    user = await make_user(client, mock_email, db_session)
    setup = await client.post(
        "/api/v1/auth/2fa/setup", headers=user["headers"])
    secret = setup.json()["data"]["secret"]
    row = await two_factor_row(db_session, user["id"])
    row.secret = secret
    await db_session.flush()
    return user, secret


@pytest.fixture
def presence_events(monkeypatch):
    sent = []

    async def _fake(settings, name, payload, **kw):
        if str(name) == "user.presence.changed":
            sent.append((str(name), payload))

    import events_out

    monkeypatch.setattr(events_out, "publish_cross_service", _fake)
    return sent


class FakeProvider:

    def __init__(self):
        self.token_status = 200
        self.token_body = {"access_token": "prov-token"}
        self.userinfo = {}
        self.emails = []
        self.raise_network = False
        self.token_requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.raise_network:
            raise httpx.ConnectError("down", request=request)
        url = str(request.url)
        if url.endswith("/token") or url.endswith("/access_token"):
            self.token_requests.append(parse_qs(request.content.decode()))
            return httpx.Response(self.token_status, json=self.token_body)
        if url.endswith("/user/emails"):
            return httpx.Response(200, json=self.emails)
        return httpx.Response(200, json=self.userinfo)


@pytest.fixture
def provider(monkeypatch):
    fake = FakeProvider()
    import domains.auth.service as svc

    monkeypatch.setattr(
        svc, "_oauth_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    return fake


NONCE_COOKIE = "alphamind_oauth_nonce"


async def oauth_start(client, provider_name="google"):
    auth = await client.get(
        f"/api/v1/auth/oauth/{provider_name}/authorize",
        follow_redirects=False)
    query = parse_qs(urlparse(auth.headers["location"]).query)
    cookie = auth.headers["set-cookie"]
    nonce = cookie.split(";")[0].split("=", 1)[1]
    return query, nonce, cookie


async def oauth_callback(client, provider_name, state, nonce, **params):
    headers = {"Cookie": f"{NONCE_COOKIE}={nonce}"} if nonce else {}
    return await client.get(
        f"/api/v1/auth/oauth/{provider_name}/callback", params={
            "code": "c",
            "state": state,
            **params
        }, headers=headers)


def oauth_result(response) -> dict[str, str]:
    from config import settings

    assert response.status_code == 303, response.text
    target, _, fragment = response.headers["location"].partition("#")
    assert target == f"{settings.FRONTEND_BASE_URL}/oauth/complete"
    return dict(parse_qsl(fragment))


async def oauth_login(client, provider_name="google", **params):
    query, nonce, _ = await oauth_start(client, provider_name)
    state = query["state"][0]
    response = await oauth_callback(
        client, provider_name, state, nonce, **params)
    return oauth_result(response), state


class TestRegistration:

    async def test_password_rules_email_and_unknown_accounts(
            self, client, mock_email, monkeypatch):
        for case, password in (("too-short", "short"), ("over-72-ascii-bytes",
                                                        "e" * 73),
                               ("over-72-multibyte-bytes", "é" * 37)):
            response = await client.post(
                "/api/v1/auth/register", json={
                    "email": "bob@example.com",
                    "username": "bob",
                    "password": password
                })
            assert response.status_code == 422, case
            assert not mock_email, case

        response = await client.post(
            "/api/v1/auth/register", json={
                "email": "alice@example.com",
                "username": "alice",
                "password": PASSWORD
            })
        assert response.status_code == 201
        assert len(mock_email) == 1
        assert mock_email[0]["to"] == "alice@example.com"
        assert "verify-email?token=" in mock_email[0]["html_body"]

        r = await client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": "nobody@example.com"})
        assert r.status_code == 200 and len(mock_email) == 1

        import security

        calls = []
        real = security.dummy_verify_password
        monkeypatch.setattr(
            security, "dummy_verify_password",
            lambda p: calls.append(p) or real(p))
        response = await login_response(client, "nobody@example.com")
        assert response.status_code == 401
        assert calls == [PASSWORD]

        import shared.email as email_module

        async def _down(*args, **kwargs):
            raise email_module.EmailSendError("smtp down")

        monkeypatch.setattr(email_module, "send_email", _down)
        name = f"smtpdown_{uuid4().hex[:8]}"
        response = await client.post(
            "/api/v1/auth/register", json={
                "email": f"{name}@example.com",
                "username": name,
                "password": PASSWORD
            })
        assert response.status_code == 201, response.text

    async def test_concurrent_requests_conflict_instead_of_duplicating(
            self, real_client, mock_email):
        tag = uuid4().hex[:8]
        body = {"email": f"race_{tag}@example.com", "password": PASSWORD}
        results = await asyncio.gather(
            real_client.post(
                "/api/v1/auth/register", json={
                    **body, "username": f"ra_{tag}"
                }),
            real_client.post(
                "/api/v1/auth/register", json={
                    **body, "username": f"rb_{tag}"
                }))
        assert sorted(r.status_code for r in results) == [201, 409]

        race_a, _ = await register_and_login(
            real_client, mock_email, "race_a@example.com", "race_a")
        race_b, _ = await register_and_login(
            real_client, mock_email, "race_b@example.com", "race_b")
        results = await asyncio.gather(
            real_client.post(
                "/api/v1/connections", json={"addressee_username": "race_b"},
                headers=race_a),
            real_client.post(
                "/api/v1/connections", json={"addressee_username": "race_a"},
                headers=race_b), return_exceptions=True)
        statuses = sorted(
            r.status_code for r in results if not isinstance(r, Exception))
        assert statuses == [201, 409]
        listed = await real_client.get("/api/v1/connections", headers=race_a)
        assert len(listed.json()["data"]) == 1


class TestTokens:

    async def test_refresh_rotation_supersede_logout_and_reuse(
            self, client, mock_email, db_session):
        _, data = await register_and_login(
            client, mock_email, "grace@example.com", "grace")
        old_refresh = data["refresh_token"]
        refreshed = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": old_refresh})
        assert refreshed.status_code == 200
        new_refresh = refreshed.json()["data"]["refresh_token"]
        assert new_refresh != old_refresh

        headers, _ = await register_and_login(
            client, mock_email, "heidi@example.com", "heidi")
        unauthenticated = await client.get("/api/v1/auth/me")
        assert unauthenticated.status_code == 401
        me = await client.get("/api/v1/auth/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["data"]["email"] == "heidi@example.com"
        assert me.json()["data"]["profile"]["avatar_url"]

        await register(client, mock_email, "ivan@example.com", "ivan")
        _, first = await login(client, "ivan@example.com")
        _, other = await login(client, "ivan@example.com")
        rotated = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": first["refresh_token"]})
        assert rotated.status_code == 200
        duplicate = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": first["refresh_token"]})
        assert duplicate.status_code == 401
        assert duplicate.json()["error"]["code"] == "refresh_token_superseded"

        out = await client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": other["refresh_token"]})
        assert out.status_code == 204
        after_logout = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": other["refresh_token"]})
        assert after_logout.status_code == 401

        successor = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": rotated.json()["data"]["refresh_token"]})
        assert successor.status_code == 200

        from domains.auth.model import SessionRevokeReason
        from domains.auth.repository import UserRepository

        await register(client, mock_email, "judy@example.com", "judy")
        _, safari = await login(client, "judy@example.com")
        _, chrome = await login(client, "judy@example.com")
        old = safari["refresh_token"]
        middle = (await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": old})).json()["data"]["refresh_token"]
        newest = (await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": middle})).json()["data"]["refresh_token"]

        repo = UserRepository(db_session)
        old_row = await repo.get_session_by_refresh_token(old)
        middle_row = await repo.get_session_by_refresh_token(middle)
        assert middle_row.created_at == old_row.revoked_at
        # Inside the grace period (a thief who refreshed first) the answer
        # only says "superseded", so the victim keeps the token and its
        # later reuse still revokes the thief's chain.
        early = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": old})
        assert early.json()["error"]["code"] == "refresh_token_superseded"
        await db_session.refresh(old_row)
        assert old_row.revoked_reason == SessionRevokeReason.ROTATED
        old_row.revoked_at -= timedelta(days=3)
        middle_row.created_at -= timedelta(days=3)
        await db_session.flush()

        reused = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": old})
        assert reused.status_code == 401
        newest_row = await repo.get_session_by_refresh_token(newest)
        await db_session.refresh(newest_row)
        assert newest_row.revoked_reason == SessionRevokeReason.REUSE
        stolen = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": newest})
        assert stolen.status_code == 401
        kept = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": chrome["refresh_token"]})
        assert kept.status_code == 200

    async def test_concurrent_refresh_and_chain_revocation_races(
            self, real_client, mock_email, db_session):
        from domains.auth.model import SessionRevokeReason
        from domains.auth.repository import UserRepository

        tag = uuid4().hex[:8]
        _, data = await register_and_login(
            real_client, mock_email, f"rr_{tag}@example.com", f"rr_{tag}")
        token = data["refresh_token"]
        results = await asyncio.gather(*(
            real_client.post(
                "/api/v1/auth/refresh", json={"refresh_token": token})
            for _ in range(2)))
        assert sorted(r.status_code for r in results) == [200, 401]
        winner, loser = sorted(results, key=lambda r: r.status_code)
        assert loser.json()["error"]["code"] == "refresh_token_superseded"
        successor = await real_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": winner.json()["data"]["refresh_token"]})
        assert successor.status_code == 200
        row = await UserRepository(db_session).get_session_by_refresh_token(
            token)
        assert row.revoked_reason == SessionRevokeReason.ROTATED

        from database import SessionLocal

        tag = uuid4().hex[:8]
        _, data = await register_and_login(
            real_client, mock_email, f"rc_{tag}@example.com", f"rc_{tag}")
        old = data["refresh_token"]
        current = (await real_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": old})).json()["data"]["refresh_token"]
        stolen = f"attacker-{tag}"
        async with SessionLocal() as thief_db, SessionLocal() as victim_db:
            thief = UserRepository(thief_db)
            claimed = await thief.claim_session_for_rotation(current)
            await thief.create_session(
                user_id=claimed.user_id, refresh_token=stolen,
                expires_at=claimed.expires_at, created_at=claimed.revoked_at)
            victim = UserRepository(victim_db)
            reused = await victim.get_session_by_refresh_token(old)
            revoking = asyncio.create_task(
                victim.revoke_rotation_successors(
                    reused, reason=SessionRevokeReason.REUSE))
            await asyncio.sleep(0.5)
            assert not revoking.done()
            await thief_db.commit()
            assert await revoking == 1
            await victim_db.commit()
        async with SessionLocal() as check_db:
            row = await UserRepository(
                check_db).get_session_by_refresh_token(stolen)
            assert row.revoked_reason == SessionRevokeReason.REUSE

    async def test_malformed_tokens_are_rejected(self, client):
        for case, claims in (("subject-not-a-uuid",
                              {"sub": "<ADMIN_USER_ID>"
                               }), ("subject-not-a-string", {"sub": 42}),
                             ("subject-missing", {})):
            headers = {"Authorization": f"Bearer {signed_token(**claims)}"}
            r = await client.get("/api/v1/auth/me", headers=headers)
            assert r.status_code == 401, case

        import security

        token = security.create_email_verification_token(
            "not-a-uuid", "a@example.com")
        with pytest.raises(security.SpecialTokenError):
            security.decode_special_token(
                token, security.EMAIL_VERIFICATION_TOKEN_TYPE)
        r = await client.post(
            "/api/v1/auth/verify-email", json={"token": token})
        assert r.status_code == 422


class TestSessions:

    async def test_logout_revokes_one_session_and_expired_are_purged(
            self, client, mock_email, db_session):
        name = f"logout_{uuid4().hex[:8]}"
        email = f"{name}@example.com"
        await register(client, mock_email, email, name)

        async def _login():
            response = await login_response(client, email)
            assert response.status_code == 200, response.text
            return response.json()["data"]["refresh_token"]

        first, second = await _login(), await _login()
        out = await client.post(
            "/api/v1/auth/logout", json={"refresh_token": first})
        assert out.status_code == 204
        kept = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": second})
        assert kept.status_code == 200
        second = kept.json()["data"]["refresh_token"]
        gone = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": first})
        assert gone.status_code == 401
        still = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": second})
        assert still.status_code == 200
        again = await client.post(
            "/api/v1/auth/logout", json={"refresh_token": first})
        assert again.status_code == 204
        unknown = await client.post(
            "/api/v1/auth/logout", json={"refresh_token": "not-a-token"})
        assert unknown.status_code == 204

        from sqlalchemy import select
        from domains.auth.model import UserSession
        from domains.auth.repository import UserRepository

        repo = UserRepository(db_session)
        name = f"purge_{uuid4().hex[:8]}"
        user = await repo.create(
            email=f"{name}@example.com", username=name, password_hash="x")
        now = datetime.now(UTC)
        old = await repo.create_session(
            user_id=user.id, refresh_token="old",
            expires_at=now - timedelta(days=30))
        recent = await repo.create_session(
            user_id=user.id, refresh_token="recent",
            expires_at=now - timedelta(days=1))
        await repo.create_session(
            user_id=user.id, refresh_token="new",
            expires_at=now + timedelta(days=7))
        ids = set((
            await db_session.execute(
                select(UserSession.id).where(UserSession.user_id == user.id)
            )).scalars())
        assert old.id not in ids
        assert recent.id in ids
        assert len(ids) == 2

    async def test_presence_follows_sessions_and_never_breaks_login(
            self, client, db_session, mock_email, presence_events,
            monkeypatch):
        headers, tokens = await register_and_login(
            client, mock_email, "pres@example.com", "presuser")
        user_id = await current_user_id(client, headers)
        assert presence_events == [presence_event(user_id, True)]

        presence_events.clear()
        url = f"/internal/users/{user_id}/presence"
        r = await client.post(url, json={"online": True})
        assert r.status_code == 204
        assert presence_events == []

        r = await client.post(url, json={"online": False})
        assert r.status_code == 204
        assert presence_events == [presence_event(user_id, False)]
        state = await client.get(f"/internal/users/{user_id}")
        assert state.json()["is_online"] is False

        presence_events.clear()
        await client.post(url, json={"online": True})
        assert presence_events == [presence_event(user_id, True)]

        await client.post(url, json={"online": False})
        r = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": tokens["refresh_token"]})
        assert r.status_code == 200
        state = await client.get(f"/internal/users/{user_id}")
        assert state.json()["is_online"] is True

        missing = await client.post(
            f"/internal/users/{uuid4()}/presence", json={"online": True})
        assert missing.status_code == 404

        from sqlalchemy import update
        from domains.auth.model import UserSession

        email = f"lo{uuid4().hex[:8]}@example.com"
        headers, _ = await register_and_login(
            client, mock_email, email,
            email.split("@")[0])
        user_id = await current_user_id(client, headers)
        await db_session.execute(
            update(UserSession).where(
                UserSession.user_id == UUID(user_id)).values(
                    created_at=datetime.now(UTC) - timedelta(hours=2)))
        await db_session.flush()
        first = (await login_response(client, email)).json()["data"]
        second = (await login_response(client, email)).json()["data"]
        presence_events.clear()

        await client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": first["refresh_token"]})
        assert presence_events == []
        await client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": second["refresh_token"]})
        assert presence_events == [presence_event(user_id, False)]
        state = await client.get(f"/internal/users/{user_id}")
        assert state.json()["is_online"] is False

        presence_events.clear()
        assert (await login_response(client, email)).status_code == 200
        assert presence_events == [presence_event(user_id, True)]

        async def _boom(*a, **k):
            raise ConnectionError("redis down")

        import events_out

        monkeypatch.setattr(events_out, "publish_cross_service", _boom)
        await register(client, mock_email, "presfail@example.com", "presfail")
        response = await login_response(client, "presfail@example.com")
        assert response.status_code == 200


class TestTwoFactor:

    async def test_login_code_and_secret_storage(
            self, client, mock_email, db_session):
        headers, _ = await register_and_login(
            client, mock_email, "frank@example.com", "frank")
        secret = await enable_2fa(client, headers)
        no_code = await login_response(client, "frank@example.com")
        assert no_code.status_code == 401
        with_code = await login_response(
            client, "frank@example.com", totp_code=next_code(secret))
        assert with_code.status_code == 200

        import security

        user, secret = await user_with_plain_text_secret(
            client, mock_email, db_session)
        verify = await client.post(
            "/api/v1/auth/2fa/verify", json={"code": pyotp.TOTP(secret).now()},
            headers=user["headers"])
        assert verify.status_code == 204
        row = await two_factor_row(db_session, user["id"])
        assert security.is_encrypted(row.secret)
        assert security.decrypt_secret(row.secret) == secret

        user = await make_user(
            client, mock_email, db_session, prefix="unreadable")
        secret = await enable_2fa(client, user["headers"])
        row = await two_factor_row(db_session, user["id"])
        row.secret = "fernet:written-with-a-key-that-is-gone"
        await db_session.flush()
        response = await login_response(
            client, user["email"], totp_code=next_code(secret))
        assert response.status_code == 409
        assert "Ask an administrator to reset your 2FA" in (
            response.json()["error"]["message"])
        disable = await client.post(
            "/api/v1/auth/2fa/disable", json={"code": next_code(secret)},
            headers=user["headers"])
        assert disable.status_code == 409

    def test_both_keys_can_read_and_the_key_is_checked(self, monkeypatch):
        import security
        from config import AuthServiceSettings, settings
        from cryptography.fernet import Fernet

        plain = "JBSWY3DPEHPK3PXP"
        written_without_key = security.encrypt_secret(plain)
        monkeypatch.setattr(
            settings, "TOTP_ENCRYPTION_KEY",
            Fernet.generate_key().decode())
        written_with_key = security.encrypt_secret(plain)
        assert security.decrypt_secret(written_without_key) == plain
        assert security.decrypt_secret(written_with_key) == plain

        long_secret = "x" * 32
        with pytest.raises(ValueError,
                           match="TOTP_ENCRYPTION_KEY must be set"):
            AuthServiceSettings(
                APP_ENV="production", JWT_SECRET=long_secret,
                INTERNAL_API_TOKEN="t" * 32, TOTP_ENCRYPTION_KEY="")
        with pytest.raises(ValueError, match="not a valid Fernet key"):
            AuthServiceSettings(TOTP_ENCRYPTION_KEY="not-a-key")
        AuthServiceSettings(
            APP_ENV="production", JWT_SECRET=long_secret,
            INTERNAL_API_TOKEN="t" * 32,
            TOTP_ENCRYPTION_KEY=Fernet.generate_key().decode())


class TestOAuth:

    async def test_two_factor_challenge_and_callback_errors(
            self, client, mock_email, provider):
        headers, _ = await register_and_login(
            client, mock_email, "oauth2fa@example.com", "oauth2fa")
        secret = await enable_2fa(client, headers)
        provider.userinfo = {
            "sub": "g-2fa",
            "email": "oauth2fa@example.com",
            "email_verified": True
        }
        data, _ = await oauth_login(client)
        assert data["expires_in"] == "300"
        assert "access_token" not in data
        bad = await client.post(
            "/api/v1/auth/oauth/2fa", json={
                "challenge_token": data["challenge_token"],
                "code": "000000"
            })
        assert bad.status_code == 401
        ok = await client.post(
            "/api/v1/auth/oauth/2fa", json={
                "challenge_token": data["challenge_token"],
                "code": next_code(secret)
            })
        assert ok.status_code == 200 and ok.json()["data"]["access_token"]
        again = await client.post(
            "/api/v1/auth/oauth/2fa", json={
                "challenge_token": data["challenge_token"],
                "code": next_code(secret)
            })
        assert again.status_code == 401

        denied = oauth_result(await client.get(
            "/api/v1/auth/oauth/google/callback",
            params={"error": "access_denied"}))
        assert denied["error"] == "oauth_failed"
        assert "access_denied" in denied["message"]
        missing = await client.get("/api/v1/auth/oauth/google/callback")
        assert oauth_result(missing)["error"] == "oauth_failed"

        provider.token_body = {"error": "bad_verification_code"}
        failures = [(await oauth_login(client, "github"))[0]]
        provider.token_status, provider.token_body = 500, {"x": 1}
        failures.append((await oauth_login(client))[0])
        provider.raise_network = True
        failures.append((await oauth_login(client))[0])
        assert [r["error"] for r in failures] == ["oauth_failed"] * 3

        provider.raise_network = False
        provider.token_status = 200
        provider.token_body = {"access_token": "prov-token"}
        provider.userinfo = {
            "sub": "g-state",
            "email": "state@example.com",
            "email_verified": True
        }
        first, state = await oauth_login(client)
        assert first["access_token"] and first["refresh_token"]
        replay = await client.get(
            "/api/v1/auth/oauth/google/callback", params={
                "code": "c",
                "state": state
            })
        assert oauth_result(replay)["error"] == "oauth_failed"

    async def test_browser_binding_and_new_account_verification(
            self, client, provider, db_session):
        import base64
        import hashlib
        from domains.auth.repository import UserRepository

        provider.userinfo = {
            "sub": "g-bind",
            "email": "bind@example.com",
            "email_verified": True
        }
        query, nonce, cookie = await oauth_start(client)
        attributes = cookie.lower()
        for attribute in ("httponly", "secure", "samesite=lax",
                          "path=/api/v1/auth/oauth", "max-age=300"):
            assert attribute in attributes
        assert query["code_challenge_method"] == ["S256"]

        state = query["state"][0]
        missing = await oauth_callback(client, "google", state, None)
        burned = await oauth_callback(client, "google", state, nonce)
        query, _, _ = await oauth_start(client)
        foreign = await oauth_callback(
            client, "google", query["state"][0], "nonce-of-another-browser")
        for failed in (missing, burned, foreign):
            assert oauth_result(failed)["error"] == "oauth_failed"
        assert provider.token_requests == []

        query, nonce, _ = await oauth_start(client)
        ok = await oauth_callback(client, "google", query["state"][0], nonce)
        tokens = oauth_result(ok)
        assert "max-age=0" in ok.headers["set-cookie"].lower()
        verifier = provider.token_requests[-1]["code_verifier"][0]
        digest = hashlib.sha256(verifier.encode()).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        assert query["code_challenge"] == [expected]

        session_row = await UserRepository(
            db_session).get_session_by_refresh_token(
                tokens["refresh_token"])
        assert session_row.ip_address
        assert session_row.device_info.startswith("python-httpx")

        provider.userinfo = {"id": 4242, "email": "public@example.com"}
        provider.emails = [{
            "email": "gh@example.com",
            "primary": True,
            "verified": True
        }]
        r, _ = await oauth_login(client, "github")
        token = r["access_token"]
        me = await client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.json()["data"]["email"] == "gh@example.com"
        assert me.json()["data"]["is_verified"] is True

        provider.userinfo = {
            "sub": "g-unv",
            "email": "unv@example.com",
            "email_verified": False
        }
        r, _ = await oauth_login(client)
        token = r["access_token"]
        me = await client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.json()["data"]["is_verified"] is False
        assert me.json()["data"]["email"] != "unv@example.com"
        username = me.json()["data"]["username"]
        assert re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username)


class TestProfile:

    async def test_validation_clearing_and_avatar_files(
            self, client, mock_email, monkeypatch, tmp_path):
        headers, _ = await register_and_login(
            client, mock_email, "profile@example.com", "profileuser")
        for body in ({"bio": "x" * 501}, {"timezone": "Mars/Olympus"},
                     {"theme": "neon"}, {"username": "bad name!"},
                     {"first_name": "x" * 101}, {"preferred_currency": "usd"},
                     {"notification_preferences": {"sms": True}
                      }, {"username": None}):
            r = await client.patch(
                "/api/v1/auth/me", json=body, headers=headers)
            assert r.status_code == 422, body

        r = await client.patch(
            "/api/v1/auth/me", json={
                "bio": "hello",
                "timezone": "Europe/Paris",
                "theme": "dark",
                "notification_preferences": {
                    "email": True
                }
            }, headers=headers)
        assert r.status_code == 200
        r = await client.patch(
            "/api/v1/auth/me", json={"bio": None}, headers=headers)
        data = r.json()["data"]
        assert data["profile"]["bio"] is None
        assert data["profile"]["timezone"] == "Europe/Paris"
        assert data["settings"]["theme"] == "dark"

        from config import settings

        monkeypatch.setattr(settings, "AVATAR_STORAGE_DIR", str(tmp_path))
        url = "/api/v1/auth/me/avatar"
        fake = await client.post(
            url, headers=headers, files={
                "file": ("x.png", b"<html>not an image</html>", "image/png")
            })
        assert fake.status_code == 422
        mismatch = await client.post(
            url, headers=headers, files={"file": ("x.jpg", PNG, "image/jpeg")})
        assert mismatch.status_code == 422

        first = await client.post(
            url, headers=headers, files={"file": ("x.png", PNG, "image/png")})
        assert first.status_code == 200
        first_name = first.json()["data"]["avatar_url"].rsplit("/", 1)[1]
        assert (tmp_path / first_name).exists()
        second = await client.post(
            url, headers=headers,
            files={"file": ("y.gif", b"GIF89a" + b"\x00" * 16, "image/gif")})
        assert second.status_code == 200
        assert not (tmp_path / first_name).exists()
        assert len(list(tmp_path.iterdir())) == 1


class TestConnections:

    async def test_only_blocker_can_unblock(self, client, mock_email):
        a, _ = await register_and_login(
            client, mock_email, "blk_a@example.com", "blk_a")
        b, _ = await register_and_login(
            client, mock_email, "blk_b@example.com", "blk_b")
        created = await client.post(
            "/api/v1/connections", json={"addressee_username": "blk_b"},
            headers=a)
        conn = created.json()["data"]
        url = f"/api/v1/connections/{conn['id']}"
        blocked = await client.post(f"{url}/block", headers=a)
        assert blocked.status_code == 200
        seen_by_a = (await client.get(
            "/api/v1/connections", headers=a)).json()["data"]
        assert [c["status"] for c in seen_by_a] == ["blocked"]
        for query in ("", "?status=blocked"):
            seen_by_b = await client.get(
                f"/api/v1/connections{query}", headers=b)
            assert seen_by_b.json()["data"] == []

        assert (await client.delete(url, headers=b)).status_code == 403
        assert (
            await client.post(f"{url}/block", headers=b)).status_code == 403
        again = await client.post(
            "/api/v1/connections", json={"addressee_username": "blk_a"},
            headers=b)
        assert again.status_code == 409
        assert (await client.delete(url, headers=a)).status_code == 204


class TestEmails:

    async def test_values_are_escaped_and_text_keeps_the_link(
            self, mock_email):
        from config import settings
        from shared.email import (_esc, _strip_html, send_notification_email,
                                  send_organization_invitation_email)

        await send_organization_invitation_email(
            settings=settings, to="a@example.com",
            organization_name='<a href="http://evil">Click</a>\r\nBcc: x@y',
            invitation_link='http://app/invitations/accept?token=t&x="1"')
        sent = mock_email[-1]
        assert '<a href="http://evil">' not in sent["html_body"]
        assert "&lt;a href=&quot;http://evil&quot;&gt;" in sent["html_body"]
        assert "\r" not in sent["subject"] and "\n" not in sent["subject"]
        await send_notification_email(
            settings=settings, to="a@example.com", title="<script>x</script>",
            content="<b>c</b>")
        assert "<script>" not in mock_email[-1]["html_body"]
        assert "&lt;b&gt;" in mock_email[-1]["html_body"]

        link = "https://localhost:8443/verify?token=a&b=1"
        html = (
            f'<p>Hi</p>\n  <p><a href="{_esc(link)}">'
            "Verify my email</a></p>")
        text = _strip_html(html)
        assert text == f"Hi\nVerify my email: {link}"


def _users(db_session):
    from domains.auth.repository import UserRepository

    return UserRepository(db_session)


def _claims(token):
    return jwt.get_unverified_claims(token)


def _stale_token(user_id, minutes):
    import security

    auth_time = int(
        (datetime.now(UTC) - timedelta(minutes=minutes)).timestamp())
    return security.create_access_token(
        user_id, extra_claims={
            "role": "user",
            "auth_time": auth_time
        })


class TestRecentLogin:

    async def test_two_factor_setup_needs_a_recent_login(
            self, client, mock_email, db_session, provider):
        user = await make_user(client, mock_email, db_session)
        assert _claims(user["headers"]["Authorization"][7:])["auth_time"]
        old = {"Authorization": f"Bearer {_stale_token(user['id'], 11)}"}
        recent = {"Authorization": f"Bearer {_stale_token(user['id'], 9)}"}
        legacy = {"Authorization": f"Bearer {signed_token(sub=user['id'])}"}
        for headers in (old, legacy):
            r = await client.post("/api/v1/auth/2fa/setup", headers=headers)
            assert r.status_code == 403, r.text
            assert r.json()["error"]["code"] == "recent_login_required"
            r = await client.post(
                "/api/v1/auth/2fa/verify", json={"code": "123456"},
                headers=headers)
            assert r.status_code == 403
            assert r.json()["error"]["code"] == "recent_login_required"
        assert (await client.get("/api/v1/auth/me", headers=old)).is_success
        r = await client.post("/api/v1/auth/2fa/setup", headers=recent)
        assert r.status_code == 200, r.text
        await enable_2fa(client, user["headers"])

        import security

        user = await make_user(client, mock_email, db_session)
        first = _claims(user["refresh_token"])["auth_time"]
        r = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": user["refresh_token"]})
        assert r.status_code == 200, r.text
        pair = r.json()["data"]
        assert _claims(pair["access_token"])["auth_time"] == first
        assert _claims(pair["refresh_token"])["auth_time"] == first

        legacy = security.create_refresh_token(user["id"], None)
        await _users(db_session).create_session(
            user_id=UUID(user["id"]), refresh_token=legacy,
            expires_at=datetime.now(UTC) + timedelta(days=1), ip_address=None,
            device_info=None)
        r = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": legacy})
        token = r.json()["data"]["access_token"]
        assert "auth_time" not in _claims(token)
        r = await client.post(
            "/api/v1/auth/2fa/setup",
            headers={"Authorization": f"Bearer {token}"})
        assert r.json()["error"]["code"] == "recent_login_required"

        provider.userinfo = {
            "sub": "g-recent",
            "email": "recent-oauth@example.com",
            "email_verified": True
        }
        r, _ = await oauth_login(client)
        token = r["access_token"]
        assert _claims(token)["auth_time"]
        headers = {"Authorization": f"Bearer {token}"}
        secret = await enable_2fa(client, headers)
        r, _ = await oauth_login(client)
        challenge = r["challenge_token"]
        ok = await client.post(
            "/api/v1/auth/oauth/2fa", json={
                "challenge_token": challenge,
                "code": next_code(secret)
            })
        assert _claims(ok.json()["data"]["access_token"])["auth_time"]


async def _unverified_user(client, mock_email, db_session, *, days_old):
    from sqlalchemy import update
    from domains.auth.model import User

    token = uuid4().hex[:10]
    email = f"unv{token}@example.com"
    r = await client.post(
        "/api/v1/auth/register", json={
            "email": email,
            "username": f"unv{token}",
            "password": PASSWORD
        })
    assert r.status_code == 201, r.text
    user = await _users(db_session).get_by_email(email)
    await db_session.execute(
        update(User).where(User.id == user.id).values(
            created_at=datetime.now(UTC) - timedelta(days=days_old)))
    await db_session.flush()
    return user.id, email


PURGE = "/internal/scheduler/purge-unverified"


class TestUnverifiedPurge:

    async def test_only_old_never_verified_accounts_are_deleted(
            self, client, mock_email, db_session, captured_events,
            monkeypatch):
        from domains.auth.model import OAuthAccount

        old_id, old_email = await _unverified_user(
            client, mock_email, db_session, days_old=8)
        young_id, _ = await _unverified_user(
            client, mock_email, db_session, days_old=6)
        linked_id, _ = await _unverified_user(
            client, mock_email, db_session, days_old=30)
        db_session.add(
            OAuthAccount(
                user_id=linked_id, provider="github", provider_user_id="77"))
        seen_id, _ = await _unverified_user(
            client, mock_email, db_session, days_old=30)
        users = _users(db_session)
        seen = await users.get_by_id(seen_id)
        seen.last_seen_at = datetime.now(UTC) - timedelta(days=20)
        verified = await make_user(client, mock_email, db_session)
        await db_session.flush()

        r = await client.post(PURGE)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["retention_days"] == 7
        assert body["deleted"] >= 1 and body["failed"] == 0
        deleted = {
            payload["user_id"]
            for name, payload in captured_events if name == "user.deleted"
        }
        assert str(old_id) in deleted
        for kept in (young_id, linked_id, seen_id, verified["id"]):
            assert str(kept) not in deleted
            row = await users.get_by_id(UUID(str(kept)))
            assert row.deleted_at is None
        gone = await users.get_by_id(old_id)
        assert gone.deleted_at is not None and gone.email != old_email
        await reset_rate_limits()
        r = await client.post(
            "/api/v1/auth/register", json={
                "email": old_email,
                "username": f"again{uuid4().hex[:8]}",
                "password": PASSWORD
            })
        assert r.status_code == 201, r.text

        captured_events.clear()
        again = await client.post(PURGE)
        assert again.status_code == 200
        assert str(old_id) not in {
            payload["user_id"]
            for name, payload in captured_events if name == "user.deleted"
        }

        from config import settings
        from domains.admin.service import AdminService

        user_id, _ = await _unverified_user(
            client, mock_email, db_session, days_old=3)
        monkeypatch.setattr(settings, "UNVERIFIED_ACCOUNT_RETENTION_DAYS", 2)

        async def _broken(self, user, actor_id):
            raise RuntimeError("database gone")

        monkeypatch.setattr(AdminService, "_delete", _broken)
        await db_session.commit()
        captured_events.clear()
        r = await client.post(PURGE)
        assert r.status_code == 500
        assert r.json()["all_units_failed"] == ["accounts"]
        assert r.json()["failed"] >= 1
        assert not [n for n, _ in captured_events if n == "user.deleted"]
        row = await _users(db_session).get_by_id(user_id)
        assert row.deleted_at is None
