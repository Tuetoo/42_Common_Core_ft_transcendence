from enum import StrEnum
from shared.enums import SystemRole


class Permission(StrEnum):
    PORTFOLIO_WRITE = "portfolio:write"
    ANALYSIS_CREATE = "analysis:create"
    DOCUMENT_UPLOAD = "document:upload"
    NOTE_WRITE = "note:write"
    COMMENT_WRITE = "comment:write"
    ORGANIZATION_CREATE = "organization:create"
    API_KEY_MANAGE = "api_key:manage"
    RAG_ASK = "rag:ask"
    CONTENT_MODERATE = "content:moderate"
    USER_DEACTIVATE = "user:deactivate"
    USER_MANAGE = "user:manage"
    SITE_STATS = "site:stats"


_WRITE: frozenset[Permission] = frozenset({
    Permission.PORTFOLIO_WRITE, Permission.ANALYSIS_CREATE,
    Permission.DOCUMENT_UPLOAD, Permission.NOTE_WRITE,
    Permission.COMMENT_WRITE, Permission.ORGANIZATION_CREATE,
    Permission.API_KEY_MANAGE, Permission.RAG_ASK
})

ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    SystemRole.GUEST: frozenset(),
    SystemRole.USER: _WRITE,
    SystemRole.MODERATOR: _WRITE
    | {Permission.CONTENT_MODERATE, Permission.USER_DEACTIVATE},
    SystemRole.ADMIN: _WRITE
    | {
        Permission.CONTENT_MODERATE, Permission.USER_DEACTIVATE,
        Permission.USER_MANAGE, Permission.SITE_STATS
    }
}


def permissions_for(role: str) -> frozenset[Permission]:
    return ROLE_PERMISSIONS.get(role, frozenset())


def has_permission(role: str, permission: Permission) -> bool:
    return permission in permissions_for(role)


def sorted_permissions(role: str) -> list[str]:
    return sorted(str(p) for p in permissions_for(role))
