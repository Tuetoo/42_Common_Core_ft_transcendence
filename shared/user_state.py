import json
from dataclasses import dataclass
from uuid import UUID
import httpx
import structlog
from shared.config import BaseServiceSettings
from shared.http_client import get_http_client
from shared.redis import get_events_client

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
