from datetime import datetime
from typing import Annotated
from uuid import UUID
from pydantic import (BaseModel, ConfigDict, Field, StringConstraints,
                      field_validator, model_validator)
from domains.auth.schema import Email
from shared.enums import OrganizationRole
from shared.schemas import BaseSchema, IDMixin, TimestampMixin


def _reject_owner(role: OrganizationRole) -> OrganizationRole:
    if role == OrganizationRole.OWNER:
        raise ValueError("the owner role cannot be assigned")
    return role


OrganizationName = Annotated[str,
                             StringConstraints(strip_whitespace=True,
                                               min_length=1, max_length=128)]


class OrganizationCreate(BaseModel):
    name: OrganizationName
    description: str | None = Field(default=None, max_length=1000)


class OrganizationUpdate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "PATCH: only fields present change. `description` can be "
                "cleared "
                "with null; `name` cannot be null.")
        })

    name: OrganizationName | None = None
    description: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _name_not_null(self) -> "OrganizationUpdate":
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name cannot be null")
        return self


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

    @field_validator("role")
    @classmethod
    def _no_owner(cls, value: OrganizationRole) -> OrganizationRole:
        return _reject_owner(value)


class InvitationCreate(BaseModel):
    email: Email
    role: OrganizationRole = OrganizationRole.MEMBER

    @field_validator("role")
    @classmethod
    def _no_owner(cls, value: OrganizationRole) -> OrganizationRole:
        return _reject_owner(value)


class InvitationRead(BaseSchema, IDMixin):
    organization_id: UUID
    email: str
    role: OrganizationRole
    expires_at: datetime
    accepted_at: datetime | None = None


class InvitationAccept(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


class InvitationAcceptResult(BaseModel):
    organization_id: UUID
    role: OrganizationRole
