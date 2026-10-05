import hmac
from collections.abc import Awaitable, Callable
from typing import Any, TypeGuard
from uuid import UUID
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from shared.config import BaseServiceSettings, get_settings
from shared.http_client import INTERNAL_TOKEN_HEADER
from shared.permissions import Permission, has_permission
from shared.user_state import get_user_state

bearer_scheme = HTTPBearer(auto_error=False)


class CurrentUser:

    def __init__(
            self, user_id: str | UUID, role: str,
            auth_time: int | None = None) -> None:
        self.user_id = UUID(str(user_id))
        self.role = role
        self.auth_time = auth_time


def is_uuid_claim(value: object) -> TypeGuard[str]:
    if not isinstance(value, str):
        return False
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def decode_token(token: str, settings: BaseServiceSettings) -> dict[str, Any]:
    try:
        payload: dict[str, Any] = jwt.decode(
            token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token") from exc


async def _current_user_from_payload(
        payload: dict[str, Any], settings: BaseServiceSettings) -> CurrentUser:
    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Wrong token type")
    if not is_uuid_claim(payload.get("sub")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token subject")
    role = str(payload.get("role", "user"))
    if settings.USER_STATE_CHECK_ENABLED:
        state = await get_user_state(settings, str(payload["sub"]))
        if state is not None:
            if not state.is_active:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Account is deactivated")
            role = state.role
    auth_time = payload.get("auth_time")
    return CurrentUser(
        user_id=payload["sub"], role=role, auth_time=(
            auth_time if isinstance(auth_time, int)
            and not isinstance(auth_time, bool) else None))


def make_get_current_user(
        settings: BaseServiceSettings
) -> Callable[..., Awaitable[CurrentUser]]:

    async def get_current_user(
                credentials: HTTPAuthorizationCredentials
            | None = Depends(bearer_scheme)) -> CurrentUser:
        if credentials is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing bearer token")
        payload = decode_token(credentials.credentials, settings)
        return await _current_user_from_payload(payload, settings)

    return get_current_user


RoleCheckedDep = Callable[..., Awaitable[CurrentUser]]


def require_permission(
        permission: Permission) -> Callable[[RoleCheckedDep], RoleCheckedDep]:

    def _factory(get_current_user_dep: RoleCheckedDep) -> RoleCheckedDep:

        async def _checker(
                user: CurrentUser = Depends(get_current_user_dep)
        ) -> CurrentUser:
            if not has_permission(user.role, permission):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Insufficient permissions")
            return user

        return _checker

    return _factory


def ensure_permission(user: CurrentUser, permission: Permission) -> None:
    if not has_permission(user.role, permission):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions")


async def require_internal_token(
        x_internal_token: str | None = Header(
            default=None, alias=INTERNAL_TOKEN_HEADER)) -> None:
    settings = get_settings()
    expected = settings.INTERNAL_API_TOKEN
    if not expected:
        if settings.APP_ENV in ("development", "test"):
            return
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Internal token not configured")
    if x_internal_token is None or not hmac.compare_digest(
            x_internal_token.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid internal token")
