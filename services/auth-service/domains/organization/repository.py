from datetime import datetime
from uuid import UUID
from typing import Any
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from domains.auth.model import User, UserProfile
from domains.organization.model import (
    CustomOrganizationRole, Organization, OrganizationInvitation,
    OrganizationMember)


class OrganizationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def get_by_id(self, org_id: UUID) -> Organization | None:
        result = await self._db.execute(select(Organization).where(
            Organization.id == org_id))
        return result.scalar_one_or_none()

    async def create(self, *, name: str, description: str | None,
                     owner_id: UUID) -> Organization:
        org = Organization(name=name, description=description,
                           owner_id=owner_id)
        self._db.add(org)
        await self._db.flush()
        return org

    async def delete(self, org: Organization) -> None:
        await self._db.delete(org)
        await self._db.flush()

    async def list_for_user(self, user_id: UUID) -> list[Organization]:
        result = await self._db.execute(
            select(Organization).join(
                OrganizationMember,
                OrganizationMember.organization_id == Organization.id).where(
                    OrganizationMember.user_id == user_id))
        return list(result.scalars().all())

    async def add_member(self, *, organization_id: UUID, user_id: UUID,
                         role: str) -> OrganizationMember:
        member = OrganizationMember(organization_id=organization_id,
                                    user_id=user_id, role=role)
        self._db.add(member)
        await self._db.flush()
        return member

    async def get_member(self, organization_id: UUID, user_id: UUID
                         ) -> OrganizationMember | None:
        result = await self._db.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == user_id))
        return result.scalar_one_or_none()

    async def list_members(self, organization_id: UUID
                           ) -> list[OrganizationMember]:
        result = await self._db.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == organization_id))
        return list(result.scalars().all())

    async def list_members_with_users(self, organization_id: UUID
                                      ) -> list[tuple[OrganizationMember, User,
                                                      UserProfile | None]]:
        result = await self._db.execute(
            select(OrganizationMember, User, UserProfile)
            .join(User, User.id == OrganizationMember.user_id)
            .outerjoin(UserProfile, UserProfile.user_id == User.id)
            .where(OrganizationMember.organization_id == organization_id))
        return [tuple(row) for row in result.all()]

    async def remove_member(self, member: OrganizationMember) -> None:
        await self._db.delete(member)
        await self._db.flush()

    async def create_invitation(
            self, *, organization_id: UUID, email: str, role: str,
            invited_by: UUID, expires_at: datetime) -> OrganizationInvitation:
        invitation = OrganizationInvitation(
            organization_id=organization_id, email=email, role=role,
            invited_by=invited_by, expires_at=expires_at)
        self._db.add(invitation)
        await self._db.flush()
        return invitation

    async def get_invitation_by_id(self, invitation_id: UUID
                                   ) -> OrganizationInvitation | None:
        result = await self._db.execute(
            select(OrganizationInvitation)
            .where(OrganizationInvitation.id == invitation_id))
        return result.scalar_one_or_none()

    async def create_role(self, *, organization_id: UUID, role_name: str,
                          permissions: dict[str, Any]
                          ) -> CustomOrganizationRole:
        role = CustomOrganizationRole(
            organization_id=organization_id, role_name=role_name,
            permissions=permissions)
        self._db.add(role)
        await self._db.flush()
        return role

    async def list_roles(self, organization_id: UUID
                         ) -> list[CustomOrganizationRole]:
        result = await self._db.execute(
            select(CustomOrganizationRole)
            .where(CustomOrganizationRole.organization_id == organization_id))
        return list(result.scalars().all())
