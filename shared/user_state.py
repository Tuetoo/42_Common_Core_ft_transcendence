import json
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID
import httpx
import structlog
from redis.exceptions import RedisError
from shared.config import BaseServiceSettings
from shared.exceptions import UpstreamServiceError
from shared.http_client import get_http_client
from shared.redis import get_cache_client, get_events_client

logger = structlog.get_logger(__name__)

_KEY_PREFIX = "auth:user_state:"
_HTTP_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class UserState:
    role: str
    is_active: bool

    def to_json(self) -> str:
        return json.dumps({"role": self.role, "is_active": self.is_active})

    @classmethod
    def from_json(cls, raw: str | bytes) -> "UserState":
        data = json.loads(raw)
        return cls(role=str(data["role"]), is_active=bool(data["is_active"]))


def user_state_key(user_id: str | UUID) -> str:
    return f"{_KEY_PREFIX}{user_id}"


async def store_user_state(
        settings: BaseServiceSettings, user_id: str | UUID,
        state: UserState) -> None:
    client = get_events_client(settings)
    await client.set(user_state_key(user_id), state.to_json())


async def get_user_state(
        settings: BaseServiceSettings,
        user_id: str | UUID) -> UserState | None:
    key = user_state_key(user_id)
    client = get_events_client(settings)
    try:
        raw = await client.get(key)
        if raw is not None:
            return UserState.from_json(raw)
    except Exception as exc:
        logger.warning(
            "user_state.redis_read_failed", user_id=str(user_id),
            error=str(exc))
    url = f"{settings.AUTH_SERVICE_URL}/internal/users/{user_id}/state"
    try:
        response = await get_http_client(_HTTP_TIMEOUT_SECONDS).get(url)
        if response.status_code == 404:
            state = UserState(role="user", is_active=False)
        else:
            response.raise_for_status()
            state = UserState.from_json(response.content)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.warning(
            "user_state.auth_lookup_failed", user_id=str(user_id),
            error=str(exc))
        return None
    try:
        await client.set(
            key, state.to_json(), ex=settings.USER_STATE_CACHE_TTL_SECONDS,
            nx=True)
    except Exception as exc:
        logger.warning(
            "user_state.redis_write_failed", user_id=str(user_id),
            error=str(exc))
    return state


MEMBERSHIP_CACHE_TTL_SECONDS = 60
FALLBACK_ORGANIZATION_ROLE = "viewer"


def org_roles_key(user_id: str | UUID) -> str:
    return f"auth_client:org_roles:{user_id}"


async def organization_roles(
        settings: BaseServiceSettings, client: httpx.AsyncClient,
        auth_service_url: str, user_id: str) -> dict[UUID, str]:
    key = org_roles_key(user_id)
    cache = get_cache_client(settings)
    try:
        cached = await cache.get(key)
    except (RedisError, OSError) as exc:
        logger.warning(
            "org_membership_cache.get_failed", key=key, error=str(exc))
        cached = None
    if cached:
        return {
            UUID(org_id): role
            for org_id, role in json.loads(cached).items()
        }
    url = f"{auth_service_url}/internal/users/{user_id}/organizations"
    try:
        response = await client.get(url)
        response.raise_for_status()
        items = response.json()
        roles: dict[UUID, str] = {}
        for item in items:
            role = item.get("role")
            if not isinstance(role, str) or not role:
                logger.warning(
                    "org_membership_cache.role_missing", user_id=user_id,
                    organization_id=item.get("id"))
                role = FALLBACK_ORGANIZATION_ROLE
            roles[UUID(str(item["id"]))] = role.lower()
    except (httpx.HTTPError, ValueError, KeyError, TypeError,
            AttributeError) as exc:
        raise UpstreamServiceError(
            f"auth-service unreachable while resolving organizations for user "
            f"{user_id}") from exc
    try:
        await cache.set(
            key,
            json.dumps({
                str(org_id): role
                for org_id, role in roles.items()
            }), ex=MEMBERSHIP_CACHE_TTL_SECONDS)
    except (RedisError, OSError) as exc:
        logger.warning(
            "org_membership_cache.set_failed", key=key, error=str(exc))
    return roles


async def invalidate_membership_cache(
        settings: BaseServiceSettings,
        user_ids: Iterable[str | UUID | None]) -> int:
    keys = sorted({org_roles_key(user_id) for user_id in user_ids if user_id})
    if not keys:
        return 0
    try:
        return int(await get_cache_client(settings).delete(*keys))
    except (RedisError, OSError) as exc:
        logger.warning(
            "org_membership_cache.invalidate_failed", keys=len(keys),
            error=str(exc))
        return 0
