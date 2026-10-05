from datetime import datetime
from uuid import UUID
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from domains.auth.model import User, UserProfile
from domains.organization.model import (Organization, OrganizationInvitation,
                                        OrganizationMember)


class OrganizationRepository:

    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def get_by_id(self, org_id: UUID) -> Organization | None:
        result = await self._db.execute(
            select(Organization).where(Organization.id == org_id))
        return result.scalar_one_or_none()

    async def create(
            self, *, name: str, description: str | None,
            owner_id: UUID) -> Organization:
        org = Organization(
            name=name, description=description, owner_id=owner_id)
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

    async def list_for_user_with_roles(
            self, user_id: UUID) -> list[tuple[Organization, str]]:
        result = await self._db.execute(
            select(Organization, OrganizationMember.role).join(
                OrganizationMember,
                OrganizationMember.organization_id == Organization.id).where(
                    OrganizationMember.user_id == user_id))
        return [(org, str(role)) for org, role in result.all()]

    async def add_member(
            self, *, organization_id: UUID, user_id: UUID,
            role: str) -> OrganizationMember:
        member = OrganizationMember(
            organization_id=organization_id, user_id=user_id, role=role)
        self._db.add(member)
        await self._db.flush()
        return member

    async def get_member(
            self, organization_id: UUID,
            user_id: UUID) -> OrganizationMember | None:
        result = await self._db.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == user_id))
        return result.scalar_one_or_none()

    async def list_members_with_users(
        self, organization_id: UUID
    ) -> list[tuple[OrganizationMember, User, UserProfile | None]]:
        result = await self._db.execute(
            select(OrganizationMember, User, UserProfile).join(
                User, User.id == OrganizationMember.user_id).outerjoin(
                    UserProfile, UserProfile.user_id == User.id).where(
                        OrganizationMember.organization_id == organization_id))
        return [tuple(row) for row in result.all()]

    async def list_member_ids(self, organization_id: UUID) -> list[UUID]:
        result = await self._db.execute(
            select(OrganizationMember.user_id).where(
                OrganizationMember.organization_id == organization_id))
        return list(result.scalars().all())

    async def list_owned_by(self, user_id: UUID) -> list[Organization]:
        result = await self._db.execute(
            select(Organization).where(Organization.owner_id == user_id))
        return list(result.scalars().all())

    async def successor_member(
            self, organization_id: UUID, *,
            excluding: UUID) -> OrganizationMember | None:
        result = await self._db.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id
                != excluding).order_by((OrganizationMember.role != "admin"),
                                       OrganizationMember.joined_at,
                                       OrganizationMember.id).limit(1))
        return result.scalar_one_or_none()

    async def remove_memberships_of_user(
            self, user_id: UUID, *, keep_owner: bool = True) -> int:
        stmt = delete(OrganizationMember).where(
            OrganizationMember.user_id == user_id)
        if keep_owner:
            stmt = stmt.where(OrganizationMember.role != "owner")
        result = await self._db.execute(
            stmt.execution_options(synchronize_session="fetch"))
        return int(getattr(result, "rowcount", 0) or 0)

    async def count_for_user(self, user_id: UUID) -> int:
        result = await self._db.execute(
            select(func.count()).select_from(OrganizationMember).where(
                OrganizationMember.user_id == user_id))
        return int(result.scalar_one())

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

    async def get_invitation_by_id(
            self, invitation_id: UUID) -> OrganizationInvitation | None:
        result = await self._db.execute(
            select(OrganizationInvitation).where(
                OrganizationInvitation.id == invitation_id))
        return result.scalar_one_or_none()

    async def list_open_invitations(
            self, organization_id: UUID,
            now: datetime) -> list[OrganizationInvitation]:
        result = await self._db.execute(
            select(OrganizationInvitation).where(
                OrganizationInvitation.organization_id == organization_id,
                OrganizationInvitation.accepted_at.is_(None),
                OrganizationInvitation.expires_at > now).order_by(
                    OrganizationInvitation.expires_at.desc(),
                    OrganizationInvitation.id))
        return list(result.scalars().all())

    async def delete_replaced_invitations(
            self, organization_id: UUID, email: str, now: datetime) -> int:
        result = await self._db.execute(
            delete(OrganizationInvitation).where(
                OrganizationInvitation.organization_id == organization_id,
                OrganizationInvitation.accepted_at.is_(None),
                or_(
                    func.lower(OrganizationInvitation.email) == email.lower(),
                    OrganizationInvitation.expires_at
                    <= now)).execution_options(synchronize_session="fetch"))
        return int(getattr(result, "rowcount", 0) or 0)

    async def delete_invitation(
            self, invitation: OrganizationInvitation) -> None:
        await self._db.delete(invitation)
        await self._db.flush()
