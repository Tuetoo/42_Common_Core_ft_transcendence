from datetime import datetime, timedelta, UTC
from uuid import UUID
import structlog
from sqlalchemy.ext.asyncio import AsyncSession
import security
from config import settings
from domains.auth.repository import UserRepository
from domains.organization.model import Organization, OrganizationMember
from domains.organization.repository import OrganizationRepository
from domains.organization.schema import (
    InvitationAcceptResult, InvitationCreate, InvitationRead, MemberRoleUpdate,
    OrganizationCreate, OrganizationMemberRead, OrganizationRead,
    OrganizationRoleCreate, OrganizationRoleRead, OrganizationUpdate)
from shared.email import send_organization_invitation_email
from shared.enums.role import OrganizationRole
from shared.events import publish_cross_service
from shared.events_registry import EventName
from shared.exceptions import (ConflictError, NotFoundError,
                               PermissionDeniedError, ValidationAppError)

logger = structlog.get_logger(__name__)

_MANAGER_ROLES = {OrganizationRole.OWNER, OrganizationRole.ADMIN}


class OrganizationService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self.orgs = OrganizationRepository(session)
        self.users = UserRepository(session)

    async def create(self, owner_id: UUID, data: OrganizationCreate
                     ) -> OrganizationRead:
        org = await self.orgs.create(
            name=data.name, description=data.description, owner_id=owner_id)
        await self.orgs.add_member(organization_id=org.id, user_id=owner_id,
                                   role=OrganizationRole.OWNER)
        return OrganizationRead.model_validate(org)

    async def _require_org(self, org_id: UUID) -> Organization:
        org = await self.orgs.get_by_id(org_id)
        if org is None:
            raise NotFoundError("Organization not found.")
        return org

    async def _require_member(self, org_id: UUID, user_id: UUID
                              ) -> OrganizationMember:
        member = await self.orgs.get_member(org_id, user_id)
        if member is None:
            raise NotFoundError("Organization not found.")
        return member

    async def _require_role(self, org_id: UUID, user_id: UUID,
                            allowed: set[OrganizationRole]
                            ) -> OrganizationMember:
        member = await self._require_member(org_id, user_id)
        if OrganizationRole(member.role) not in allowed:
            raise PermissionDeniedError(
                "You do not have permission to perform this action.")
        return member

    async def get(self, org_id: UUID, requester_id: UUID) -> OrganizationRead:
        org = await self._require_org(org_id)
        await self._require_member(org_id, requester_id)
        return OrganizationRead.model_validate(org)

    async def list_mine(self, user_id: UUID) -> list[OrganizationRead]:
        orgs = await self.orgs.list_for_user(user_id)
        return [OrganizationRead.model_validate(org) for org in orgs]

    async def update(self, org_id: UUID, requester_id: UUID,
                     data: OrganizationUpdate) -> OrganizationRead:
        org = await self._require_org(org_id)
        await self._require_role(org_id, requester_id, _MANAGER_ROLES)
        if data.name is not None:
            org.name = data.name
        if data.description is not None:
            org.description = data.description
        await self._db.flush()
        await self._db.refresh(org)
        return OrganizationRead.model_validate(org)

    async def delete(self, org_id: UUID, requester_id: UUID) -> None:
        org = await self._require_org(org_id)
        if org.owner_id != requester_id:
            raise PermissionDeniedError(
                "Only the organization owner can delete it.")
        await self.orgs.delete(org)

    async def list_members(self, org_id: UUID, requester_id: UUID
                           ) -> list[OrganizationMemberRead]:
        await self._require_org(org_id)
        await self._require_member(org_id, requester_id)
        rows = await self.orgs.list_members_with_users(org_id)
        return [OrganizationMemberRead(
            id=member.id, user_id=member.user_id, username=user.username,
            avatar_url=profile.avatar_url if profile else None,
            role=OrganizationRole(member.role), joined_at=member.joined_at)
            for member, user, profile in rows]

    async def remove_member(self, org_id: UUID, requester_id: UUID,
                            target_user_id: UUID) -> None:
        org = await self._require_org(org_id)
        await self._require_role(org_id, requester_id, _MANAGER_ROLES)
        if target_user_id == org.owner_id:
            raise ValidationAppError(
                "The organization owner cannot be removed.")
        member = await self.orgs.get_member(org_id, target_user_id)
        if member is None:
            raise NotFoundError(
                "This user is not a member of the organization.")
        await self.orgs.remove_member(member)

    async def update_member_role(self, org_id: UUID, requester_id: UUID,
                                 target_user_id: UUID, data: MemberRoleUpdate
                                 ) -> OrganizationMemberRead:
        org = await self._require_org(org_id)
        await self._require_role(org_id, requester_id, _MANAGER_ROLES)
        if target_user_id == org.owner_id:
            raise ValidationAppError(
                "The organization owner's role cannot be changed.")
        member = await self.orgs.get_member(org_id, target_user_id)
        if member is None:
            raise NotFoundError(
                "This user is not a member of the organization.")
        member.role = data.role
        await self._db.flush()
        user = await self.users.get_by_id(target_user_id)
        profile = await self.users.get_profile(target_user_id
                                               ) if user else None
        return OrganizationMemberRead(
            id=member.id, user_id=member.user_id,
            username=user.username if user else "",
            avatar_url=profile.avatar_url if profile else None,
            role=OrganizationRole(member.role), joined_at=member.joined_at)

    async def invite(self, org_id: UUID, requester_id: UUID,
                     data: InvitationCreate) -> InvitationRead:
        org = await self._require_org(org_id)
        await self._require_role(org_id, requester_id, _MANAGER_ROLES)
        existing_user = await self.users.get_by_email(data.email)
        if (existing_user is not None
            and await self.orgs.get_member(org_id, existing_user.id
                                           ) is not None):
            raise ConflictError(
                "This user is already a member of the organization.")
        expires_at = datetime.now(UTC) + timedelta(
            days=settings.ORGANIZATION_INVITATION_EXPIRE_DAYS)
        invitation = await self.orgs.create_invitation(
            organization_id=org_id, email=data.email, role=data.role,
            invited_by=requester_id, expires_at=expires_at)
        token = security.create_organization_invitation_token(
            str(invitation.id), settings.ORGANIZATION_INVITATION_EXPIRE_DAYS)
        link = f"{settings.FRONTEND_BASE_URL}/invitations/accept?token={token}"
        await send_organization_invitation_email(
            settings=settings, to=data.email, organization_name=org.name,
            invitation_link=link)
        return InvitationRead.model_validate(invitation)

    async def accept_invitation(self, token: str, current_user_id: UUID
                                ) -> InvitationAcceptResult:
        try:
            payload = security.decode_special_token(
                token, security.ORGANIZATION_INVITATION_TOKEN_TYPE)
        except security.SpecialTokenError as exc:
            raise ValidationAppError(str(exc)) from exc
        invitation = await self.orgs.get_invitation_by_id(UUID(payload["sub"]))
        if invitation is None:
            raise NotFoundError("Invitation no longer exists.")
        if invitation.accepted_at is not None:
            raise ConflictError("This invitation has already been accepted.")
        if invitation.expires_at < datetime.now(UTC):
            raise ValidationAppError("This invitation has expired.")
        current_user = await self.users.get_by_id(current_user_id)
        if current_user is None or current_user.email != invitation.email:
            raise PermissionDeniedError(
                "This invitation was sent to a different email address. "
                "Log in with that account to accept it.")
        if await self.orgs.get_member(invitation.organization_id,
                                      current_user_id):
            raise ConflictError(
                "You are already a member of this organization.")
        await self.orgs.add_member(organization_id=invitation.organization_id,
                                   user_id=current_user_id,
                                   role=invitation.role)
        invitation.accepted_at = datetime.now(UTC)
        await self._db.flush()
        await publish_cross_service(
            settings, EventName.ORGANIZATION_MEMBER_JOINED,
            {"organization_id": str(invitation.organization_id),
             "user_id": str(current_user_id)})
        return InvitationAcceptResult(
            organization_id=invitation.organization_id,
            role=OrganizationRole(invitation.role))

    async def create_role(
            self, org_id: UUID, requester_id: UUID,
            data: OrganizationRoleCreate) -> OrganizationRoleRead:
        await self._require_org(org_id)
        await self._require_role(org_id, requester_id, _MANAGER_ROLES)
        role = await self.orgs.create_role(
            organization_id=org_id, role_name=data.role_name,
            permissions=data.permissions)
        return OrganizationRoleRead.model_validate(role)

    async def list_roles(self, org_id: UUID, requester_id: UUID
                         ) -> list[OrganizationRoleRead]:
        await self._require_org(org_id)
        await self._require_member(org_id, requester_id)
        roles = await self.orgs.list_roles(org_id)
        return [OrganizationRoleRead.model_validate(role) for role in roles]
