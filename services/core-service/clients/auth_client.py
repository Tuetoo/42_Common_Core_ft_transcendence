from typing import Any
from uuid import UUID
import httpx
import structlog
from config import settings
from shared.exceptions import PermissionDeniedError, UpstreamServiceError
from shared.http_client import get_http_client
from shared.user_state import organization_roles
from shared.redis import get_cache_client
from shared.schemas import UserRef

logger = structlog.get_logger(__name__)

_CACHE_TTL_SECONDS = 60
_TIMEOUT = 3.0


async def get_user_ref(user_id: str) -> UserRef:
    cache_key = f"core:auth_client:user:{user_id}"
    cache = get_cache_client(settings)
    try:
        cached = await cache.get(cache_key)
    except Exception as exc:
        logger.warning("user_ref_cache_read_failed", error=str(exc))
        cached = None
    if cached:
        return UserRef.model_validate_json(cached)
    url = f"{settings.AUTH_SERVICE_URL}/internal/users/{user_id}"
    try:
        response = await get_http_client(_TIMEOUT).get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise UpstreamServiceError(
            f"auth-service unreachable while resolving user {user_id}"
        ) from exc
    user_ref = UserRef.model_validate(response.json())
    try:
        await cache.set(
            cache_key, user_ref.model_dump_json(), ex=_CACHE_TTL_SECONDS)
    except Exception as exc:
        logger.warning("user_ref_cache_write_failed", error=str(exc))
    return user_ref


async def are_users_connected(user_a: str, user_b: str) -> bool:
    url = f"{settings.AUTH_SERVICE_URL}/internal/connections/check"
    try:
        response = await get_http_client(_TIMEOUT).get(
            url, params={
                "user_a": user_a,
                "user_b": user_b
            })
        response.raise_for_status()
    except httpx.HTTPError:
        return False
    return bool(response.json().get("connected"))


async def list_friend_ids(user_id: str) -> list[str]:
    url = f"{settings.AUTH_SERVICE_URL}/internal/users/{user_id}/friends"
    try:
        response = await get_http_client(_TIMEOUT).get(url)
        response.raise_for_status()
        return [str(friend) for friend in response.json()["friend_ids"]]
    except Exception:
        logger.warning(
            "auth_friend_lookup_failed", user_id=user_id, exc_info=True)
        return []


async def list_user_organization_roles(user_id: str) -> dict[UUID, str]:
    return await organization_roles(
        settings, get_http_client(_TIMEOUT), settings.AUTH_SERVICE_URL,
        user_id)


async def list_user_organization_ids(user_id: str) -> list[UUID]:
    return list(await list_user_organization_roles(user_id))


ORGANIZATION_WRITE_ROLES = frozenset({"owner", "admin", "member"})


async def get_organization_role(
        user_id: str, organization_id: UUID) -> str | None:
    roles = await list_user_organization_roles(user_id)
    return roles.get(organization_id)


async def ensure_organization_writer(
        user_id: UUID, organization_id: UUID) -> None:
    role = await get_organization_role(str(user_id), organization_id)
    if role is None:
        raise PermissionDeniedError(
            "You are not a member of the target organization.")
    if role not in ORGANIZATION_WRITE_ROLES:
        raise PermissionDeniedError(
            "Organization viewers have read-only access.")


async def get_notification_settings(user_id: str) -> dict[str, Any]:
    url = (
        f"{settings.AUTH_SERVICE_URL}/internal/users/{user_id}"
        f"/notification-settings")
    try:
        response = await get_http_client(_TIMEOUT).get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise UpstreamServiceError(
            "auth-service unreachable while resolving "
            f"notification settings for user {user_id}") from exc
    result: dict[str, Any] = response.json()
    return result


async def set_user_presence(user_id: str, *, online: bool) -> None:
    url = f"{settings.AUTH_SERVICE_URL}/internal/users/{user_id}/presence"
    try:
        response = await get_http_client(_TIMEOUT).post(
            url, json={"online": online})
        response.raise_for_status()
    except Exception:
        logger.warning(
            "auth_presence_update_failed", user_id=user_id, online=online,
            exc_info=True)
