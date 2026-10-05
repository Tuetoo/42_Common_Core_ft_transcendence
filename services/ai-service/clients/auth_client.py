from uuid import UUID
from config import settings
from shared.http_client import get_http_client
from shared.org_membership_cache import organization_roles

_TIMEOUT = 3.0


async def list_user_organization_ids(user_id: str) -> list[UUID]:
    roles = await organization_roles(
        settings, get_http_client(_TIMEOUT), settings.AUTH_SERVICE_URL,
        user_id)
    return list(roles)
