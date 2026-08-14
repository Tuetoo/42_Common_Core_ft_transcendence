from datetime import datetime
from typing import Any
from uuid import UUID
from pydantic import BaseModel, EmailStr, Field
from shared.enums.role import OrganizationRole
from shared.schemas.base import BaseSchema, IDMixin, TimestampMixin


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None


class OrganizationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None


class OrganizationRead(BaseSchema, IDMixin, TimestampMixin):
    name: str
    description: str | None = None
    owner_id: UUID


class OrganizationMemberRead(BaseSchema, IDMixin):
    user_id: UUID
    username: str
    avatar_url: str | None = None
    role: OrganizationRole
    joined_at: datetime


class MemberRoleUpdate(BaseModel):
    role: OrganizationRole


class InvitationCreate(BaseModel):
    email: EmailStr
    role: OrganizationRole = OrganizationRole.MEMBER


class InvitationRead(BaseSchema, IDMixin):
    organization_id: UUID
    email: str
    role: OrganizationRole
    expires_at: datetime
    accepted_at: datetime | None = None


class InvitationAcceptResult(BaseModel):
    organization_id: UUID
    role: OrganizationRole


class OrganizationRoleCreate(BaseModel):
    role_name: str = Field(min_length=1, max_length=64)
    permissions: dict[str, Any] = Field(default_factory=dict)


class OrganizationRoleRead(BaseSchema, IDMixin):
    organization_id: UUID
    role_name: str
    permissions: dict[str, Any]
