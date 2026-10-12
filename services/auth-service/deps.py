from config import settings
from fastapi import Depends
from security import is_recent_login
from domains.auth.service import RecentLoginRequiredError
from shared.permissions import Permission
from shared.security import (CurrentUser, make_get_current_user,
                             require_permission)

get_current_user = make_get_current_user(settings)


async def require_recent_login(
        user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not is_recent_login(user.auth_time):
        raise RecentLoginRequiredError()
    return user


require_organization_create = require_permission(
    Permission.ORGANIZATION_CREATE)(get_current_user)
require_connection_write = require_permission(
    Permission.CONNECTION_WRITE)(get_current_user)
require_user_manage = require_permission(
    Permission.USER_MANAGE)(get_current_user)
require_user_deactivate = require_permission(
    Permission.USER_DEACTIVATE)(get_current_user)
require_site_stats = require_permission(
    Permission.SITE_STATS)(get_current_user)
