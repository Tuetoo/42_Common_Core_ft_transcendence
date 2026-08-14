import uuid
from datetime import datetime
from typing import Any
from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from shared.database import Base
from shared.models import TimestampColumnsMixin, UUIDPKMixin


class Organization(Base, UUIDPKMixin, TimestampColumnsMixin):
    __tablename__ = "organizations"
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)


class OrganizationMember(Base, UUIDPKMixin):
    __tablename__ = "organization_members"
    __table_args__ = (UniqueConstraint("organization_id", "user_id",
                                       name="uq_org_member_org_user"), )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id",
                                         ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False,
        index=True)
    role: Mapped[str] = mapped_column(String, nullable=False, default="member")
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", nullable=False)


class CustomOrganizationRole(Base, UUIDPKMixin):
    __tablename__ = "organization_roles"
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(
            "organizations.id", ondelete="CASCADE"), nullable=False,
        index=True)
    role_name: Mapped[str] = mapped_column(String, nullable=False)
    permissions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False,
                                                        default=dict)


class OrganizationInvitation(Base, UUIDPKMixin):
    __tablename__ = "organization_invitations"
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id",
                                         ondelete="CASCADE"), nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False, default="member")
    invited_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    