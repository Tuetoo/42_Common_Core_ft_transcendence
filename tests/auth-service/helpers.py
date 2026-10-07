import re
import time
from uuid import UUID, uuid4
import pyotp

PASSWORD = "correct-horse-battery"


def uid() -> str:
    return uuid4().hex[:10]


async def register(client, mock_email, email, username, password=PASSWORD):
    r = await client.post(
        "/api/v1/auth/register", json={
            "email": email,
            "username": username,
            "password": password
        })
    assert r.status_code == 201, r.text
    body = mock_email[-1]["html_body"]
    token = re.search(r"verify-email\?token=([^\s\"'<&]+)", body).group(1)
    r = await client.post("/api/v1/auth/verify-email", json={"token": token})
    assert r.status_code == 200, r.text


async def login_response(client, email, password=PASSWORD, **extra):
    return await client.post(
        "/api/v1/auth/login", json={
            "email": email,
            "password": password,
            **extra
        })


async def login(client, email, password=PASSWORD):
    r = await login_response(client, email, password)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    return {"Authorization": f"Bearer {data['access_token']}"}, data


async def register_and_login(client, mock_email, email, username):
    await register(client, mock_email, email, username)
    return await login(client, email)


async def enable_2fa(client, headers):
    setup = await client.post("/api/v1/auth/2fa/setup", headers=headers)
    secret = setup.json()["data"]["secret"]
    r = await client.post(
        "/api/v1/auth/2fa/verify", json={"code": pyotp.TOTP(secret).now()},
        headers=headers)
    assert r.status_code == 204
    return secret


def next_code(secret):
    return pyotp.TOTP(secret).at(time.time() + 30)


async def current_user_id(client, headers):
    r = await client.get("/api/v1/auth/me", headers=headers)
    return r.json()["data"]["id"]


async def set_role(db_session, user_id, role):
    from user_state_sync import mirror_user
    from domains.auth.repository import UserRepository

    user = await UserRepository(db_session).get_by_id(UUID(str(user_id)))
    user.system_role = role
    await db_session.flush()
    await mirror_user(user)
    return user


async def make_user(client, mock_email, db_session, role="user", prefix="u"):
    token = uid()
    email = f"{prefix}{token}@Example.com".lower()
    username = f"{prefix}{token}"
    await register(client, mock_email, email, username)
    from domains.auth.repository import UserRepository

    user = await UserRepository(db_session).get_by_email(email)
    if role != "user":
        await set_role(db_session, user.id, role)
    headers, tokens = await login(client, email)
    return {
        "id": str(user.id),
        "email": email,
        "username": username,
        "headers": headers,
        "refresh_token": tokens["refresh_token"]
    }


async def demote_all_admins(db_session):
    from sqlalchemy import text

    await db_session.execute(
        text(
            "UPDATE users SET system_role = 'user' WHERE system_role = 'admin'"
        ))
    await db_session.flush()


async def reset_rate_limits():
    from config import settings
    from shared.redis import get_rate_limit_client

    await get_rate_limit_client(settings).flushdb()


def events_named(events, name):
    return [payload for event_name, payload in events if event_name == name]
