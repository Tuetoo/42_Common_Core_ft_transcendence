from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from domains.auth.schema import (USERNAME_PATTERN, Email, TimezoneName,
                                 UserProfileRead)
from shared.enums import SystemRole
from shared.schemas import BaseSchema, IDMixin


class AdminUserSort(StrEnum):
    CREATED_AT = "created_at"
    EMAIL = "email"
    USERNAME = "username"
    LAST_SEEN_AT = "last_seen_at"


SortOrder = Literal["asc", "desc"]


class AdminUserListItem(BaseSchema, IDMixin):
    email: str
    username: str
    system_role: str
    is_active: bool
    is_verified: bool
    is_online: bool = False
    avatar_url: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    created_at: datetime
    last_seen_at: datetime | None = None
    deleted_at: datetime | None = None


class AdminUserDetail(AdminUserListItem):
    updated_at: datetime
    permissions: list[str] = Field(default_factory=list)
    profile: UserProfileRead | None = None
    two_factor_enabled: bool = False
    oauth_providers: list[str] = Field(default_factory=list)
    organizations_count: int = 0


class AdminUserUpdate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "PATCH: only fields present in the body change. An explicit "
                "null "
                "clears a nullable profile field; null is rejected (422) for "
                "the "
                "account fields.")
        })

    username: str | None = Field(default=None, pattern=USERNAME_PATTERN)
    email: Email | None = None
    is_verified: bool | None = None
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    bio: str | None = Field(default=None, max_length=500)
    timezone: TimezoneName | None = None
    country: str | None = Field(default=None, max_length=100)
    role: SystemRole | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _no_null_for_required(self) -> "AdminUserUpdate":
        for field in ("username", "email", "is_verified", "role", "is_active"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class AdminStats(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "`core` / `ai` are null when that service could not be "
                "reached.")
        })

    auth: dict[str, Any]
    core: dict[str, Any] | None = None
    ai: dict[str, Any] | None = None
