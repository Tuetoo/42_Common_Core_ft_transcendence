from typing import Any
from uuid import UUID
import httpx
import json
from shared.exceptions import UpstreamServiceError
from shared.http_client import get_http_client
from shared.redis import get_cache_client
from shared.schemas.refs import OrganizationRef, UserRef
from config import settings

_CACHE_TTL_SECONDS = 60
_ORG_MEMBERSHIP_CACHE_TTL_SECONDS = 15
_TIMEOUT = 3.0


async def get_user_ref(user_id: str) -> UserRef:
    cache_key = f"core:auth_client:user:{user_id}"
    cache = get_cache_client(settings)
    cached = await cache.get(cache_key)
    if cached:
        return UserRef.model_validate_json(cached)
    url = f"{settings.AUTH_SERVICE_URL}/internal/users/{user_id}"
    try:
        response = await get_http_client(_TIMEOUT).get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise UpstreamServiceError("auth-service unreachable while resolving "
                                   f"user {user_id}") from exc
    user_ref = UserRef.model_validate(response.json())
    await cache.set(cache_key, user_ref.model_dump_json(),
                    ex=_CACHE_TTL_SECONDS)
    return user_ref


async def get_organization_ref(org_id: str) -> OrganizationRef:
    cache_key = f"core:auth_client:org:{org_id}"
    cache = get_cache_client(settings)
    cached = await cache.get(cache_key)
    if cached:
        return OrganizationRef.model_validate_json(cached)
    url = f"{settings.AUTH_SERVICE_URL}/internal/organizations/{org_id}"
    try:
        response = await get_http_client(_TIMEOUT).get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise UpstreamServiceError("auth-service unreachable while resolving "
                                   f"org {org_id}") from exc
    org_ref = OrganizationRef.model_validate(response.json())
    await cache.set(cache_key, org_ref.model_dump_json(),
                    ex=_CACHE_TTL_SECONDS)
    return org_ref


async def are_users_connected(user_a: str, user_b: str) -> bool:
    url = f"{settings.AUTH_SERVICE_URL}/internal/connections/check"
    try:
        response = await get_http_client(_TIMEOUT).get(
            url, params={"user_a": user_a, "user_b": user_b})
        response.raise_for_status()
    except httpx.HTTPError:
        return False
    return bool(response.json().get("connected"))


async def list_user_organization_ids(user_id: str) -> list[UUID]:
    cache_key = f"core:auth_client:orgs:{user_id}"
    cache = get_cache_client(settings)
    cached = await cache.get(cache_key)
    if cached:
        return [UUID(org_id) for org_id in json.loads(cached)]
    url = f"{settings.AUTH_SERVICE_URL}/internal/users/{user_id}/organizations"
    try:
        response = await get_http_client(_TIMEOUT).get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise UpstreamServiceError("auth-service unreachable while resolving "
                                   "organizations for user "
                                   f"{user_id}") from exc
    org_ids = [UUID(org["id"]) for org in response.json()]
    await cache.set(cache_key, json.dumps([str(org_id) for org_id in org_ids]),
                    ex=_ORG_MEMBERSHIP_CACHE_TTL_SECONDS)
    return org_ids


async def get_notification_settings(user_id: str) -> dict[str, Any]:
    url = (f"{settings.AUTH_SERVICE_URL}/internal/users/{user_id}"
           "/notification-settings")
    try:
        response = await get_http_client(_TIMEOUT).get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise UpstreamServiceError("auth-service unreachable while resolving "
                                   "notification settings for user "
                                   f"{user_id}") from exc
    result: dict[str, Any] = response.json()
    return result
