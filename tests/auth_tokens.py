# pyright: reportMissingImports=false
# Shared by the service suites: `config` and `database` resolve to the
# modules of whichever service's tests import this file.
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from jose import jwt

_ACCESS_TTL = timedelta(minutes=15)


def access_token(
        user_id: Any = None, role: str = "user", *,
        ttl: timedelta = _ACCESS_TTL) -> tuple[str, str]:
    from config import settings

    user = str(user_id) if user_id else str(uuid4())
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": user,
        "type": "access",
        "role": role,
        "iat": now,
        "exp": now + ttl
    }
    token = jwt.encode(
        claims, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return user, token


def auth_headers(
        user_id: Any = None, role: str = "user", *,
        ttl: timedelta = _ACCESS_TTL) -> tuple[str, dict[str, str]]:
    user, token = access_token(user_id, role, ttl=ttl)
    return user, {"Authorization": f"Bearer {token}"}


def bearer_headers(user_id: Any, role: str = "user") -> dict[str, str]:
    return auth_headers(user_id, role)[1]
