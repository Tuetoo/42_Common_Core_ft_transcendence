from enum import StrEnum


class SystemRole(StrEnum):
    ADMIN = "admin"
    USER = "user"


class OrganizationRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"
