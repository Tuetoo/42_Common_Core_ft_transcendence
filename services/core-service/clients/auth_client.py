import httpx
from shared.exceptions import UpstreamServiceError
from shared.redis import get_cache_client
from shared.schemas.refs import OrganizationRef, UserRef
from config import settings

_CACHE_TTL_SECONDS = 60


async def get_user_ref(user_id: str) -> UserRef:
    cache_key = f"core:auth_client:user:{user_id}"
    cache = get_cache_client(settings)
    cached = await cache.get(cache_key)
    if cached:
        return UserRef.model_validate_json(cached)
    url = f"{settings.AUTH_SERVICE_URL}/internal/users/{user_id}"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(url)
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
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise UpstreamServiceError("auth-service unreachable while resolving "
                                   f"org {org_id}") from exc
    org_ref = OrganizationRef.model_validate(response.json())
    await cache.set(cache_key, org_ref.model_dump_json(),
                    ex=_CACHE_TTL_SECONDS)
    return org_ref
