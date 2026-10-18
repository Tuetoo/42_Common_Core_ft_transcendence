from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID
import security
from config import settings
from events_out import (MEMBERSHIP_EVENTS, PendingEvent,
                        organization_event_payload)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from domains.auth.repository import UserRepository
from domains.auth.service import avatar_or_default
from domains.organization.model import Organization, OrganizationMember
from domains.organization.repository import OrganizationRepository
from domains.organization.schema import (InvitationAcceptResult,
                                         InvitationCreate, InvitationRead,
                                         MemberRoleUpdate, OrganizationCreate,
                                         OrganizationMemberRead,
                                         OrganizationRead, OrganizationUpdate)
from shared.enums import OrganizationRole
from shared.events import EventName
from shared.exceptions import (ConflictError, NotFoundError,
                               PermissionDeniedError, ValidationAppError)
from shared.user_state import invalidate_membership_cache

_MANAGER_ROLES = {OrganizationRole.OWNER, OrganizationRole.ADMIN}


@dataclass(frozen=True)
class InvitationEmail:
    to: str
    organization_name: str
    link: str


class OrganizationService:

    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self.orgs = OrganizationRepository(session)
        self.users = UserRepository(session)
        self.pending_events: list[PendingEvent] = []
        self.pending_invitation_emails: list[InvitationEmail] = []

    async def _org_event(
            self, name: EventName, org: Organization, actor_id: UUID, *,
            user_id: UUID | None = None, role: str | None = None,
            member_ids: list[UUID] | None = None) -> None:
        if member_ids is None:
            await self._db.flush()
            member_ids = await self.orgs.list_member_ids(org.id)
        self.pending_events.append((
            name,
            organization_event_payload(
                organization_id=org.id, organization_name=org.name,
                actor_id=actor_id, user_id=user_id, role=role,
                member_ids=member_ids)))
        if name in MEMBERSHIP_EVENTS:
            await invalidate_membership_cache(
                settings, [
                    user_id, *(
                        member_ids
                        if name == EventName.ORGANIZATION_DELETED else [])
                ])

    async def create(
            self, owner_id: UUID,
            data: OrganizationCreate) -> OrganizationRead:
        org = await self.orgs.create(
            name=data.name, description=data.description, owner_id=owner_id)
        await self.orgs.add_member(
            organization_id=org.id, user_id=owner_id,
            role=OrganizationRole.OWNER)
        await self._org_event(
            EventName.ORGANIZATION_CREATED, org, owner_id, user_id=owner_id,
            role=str(OrganizationRole.OWNER), member_ids=[owner_id])
        return OrganizationRead.model_validate(org)

    async def get(self, org_id: UUID, requester_id: UUID) -> OrganizationRead:
        org = await self._require_org(org_id)
        await self._require_member(org_id, requester_id)
        return OrganizationRead.model_validate(org)

    async def list_mine(self, user_id: UUID) -> list[OrganizationRead]:
        orgs = await self.orgs.list_for_user(user_id)
        return [OrganizationRead.model_validate(org) for org in orgs]

    async def update(
            self, org_id: UUID, requester_id: UUID,
            data: OrganizationUpdate) -> OrganizationRead:
        org = await self._require_org(org_id)
        await self._require_role(org_id, requester_id, _MANAGER_ROLES)
        changes = data.model_dump(exclude_unset=True)
        if "name" in changes:
            org.name = changes["name"]
        if "description" in changes:
            org.description = changes["description"]
        await self._db.flush()
        await self._db.refresh(org)
        if changes:
            await self._org_event(
                EventName.ORGANIZATION_UPDATED, org, requester_id)
        return OrganizationRead.model_validate(org)

    async def delete(self, org_id: UUID, requester_id: UUID) -> None:
        org = await self._require_org(org_id)
        await self._require_member(org_id, requester_id)
        if org.owner_id != requester_id:
            raise PermissionDeniedError(
                "Only the organization owner can delete it.")
        member_ids = await self.orgs.list_member_ids(org_id)
        await self._org_event(
            EventName.ORGANIZATION_DELETED, org, requester_id,
            member_ids=member_ids)
        await self.orgs.delete(org)

    async def list_members(self, org_id: UUID,
                           requester_id: UUID) -> list[OrganizationMemberRead]:
        await self._require_org(org_id)
        await self._require_member(org_id, requester_id)
        rows = await self.orgs.list_members_with_users(org_id)

        return [
            OrganizationMemberRead(
                id=member.id, user_id=member.user_id, username=user.username,
                avatar_url=avatar_or_default(
                    profile.avatar_url if profile else None, user.username),
                role=OrganizationRole(member.role), joined_at=member.joined_at)
            for member, user, profile in rows
        ]

    async def remove_member(
            self, org_id: UUID, requester_id: UUID,
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
        if (member.role == OrganizationRole.ADMIN
                and requester_id != org.owner_id):
            raise PermissionDeniedError(
                "Only the organization owner can remove an admin.")
        previous_role = member.role
        await self.orgs.remove_member(member)
        await self.orgs.delete_open_invitations_by(org_id, target_user_id)
        await self._org_event(
            EventName.ORGANIZATION_MEMBER_REMOVED, org, requester_id,
            user_id=target_user_id, role=previous_role)

    async def leave(self, org_id: UUID, user_id: UUID) -> None:
        org = await self._require_org(org_id)
        member = await self._require_member(org_id, user_id)
        if org.owner_id == user_id:
            raise ValidationAppError(
                "The owner cannot leave the organization. Delete it instead.")
        previous_role = member.role
        await self.orgs.remove_member(member)
        await self.orgs.delete_open_invitations_by(org_id, user_id)
        await self._org_event(
            EventName.ORGANIZATION_MEMBER_LEFT, org, user_id, user_id=user_id,
            role=previous_role)

    async def update_member_role(
            self, org_id: UUID, requester_id: UUID, target_user_id: UUID,
            data: MemberRoleUpdate) -> OrganizationMemberRead:
        org = await self._require_org(org_id)
        await self._require_role(org_id, requester_id, _MANAGER_ROLES)
        if target_user_id == org.owner_id:
            raise ValidationAppError(
                "The organization owner's role cannot be changed.")
        member = await self.orgs.get_member(org_id, target_user_id)
        if member is None:
            raise NotFoundError(
                "This user is not a member of the organization.")
        if (member.role == OrganizationRole.ADMIN or data.role
                == OrganizationRole.ADMIN) and requester_id != org.owner_id:
            raise PermissionDeniedError(
                "Only the organization owner can change admin roles.")
        previous_role = member.role
        member.role = data.role
        await self._db.flush()
        if OrganizationRole(member.role) not in _MANAGER_ROLES:
            await self.orgs.delete_open_invitations_by(org_id, target_user_id)
        if previous_role != member.role:
            await self._org_event(
                EventName.ORGANIZATION_ROLE_CHANGED, org, requester_id,
                user_id=target_user_id, role=str(data.role))

        user = await self.users.get_by_id(target_user_id)
        profile = (
            await self.users.get_profile(target_user_id) if user else None)
        return OrganizationMemberRead(
            id=member.id, user_id=member.user_id,
            username=user.username if user else "",
            avatar_url=avatar_or_default(
                profile.avatar_url if profile else None,
                user.username if user else ""),
            role=OrganizationRole(member.role), joined_at=member.joined_at)

    async def invite(
            self, org_id: UUID, requester_id: UUID,
            data: InvitationCreate) -> InvitationRead:
        org = await self._require_org(org_id)
        await self._require_role(org_id, requester_id, _MANAGER_ROLES)
        if (data.role == OrganizationRole.ADMIN
                and requester_id != org.owner_id):
            raise PermissionDeniedError(
                "Only the organization owner can invite admins.")
        await security.hit(
            "invite:org", str(org_id),
            limit=settings.RATE_LIMIT_INVITATIONS_PER_ORG,
            window=settings.RATE_LIMIT_INVITATIONS_WINDOW_SECONDS)

        existing_user = await self.users.get_by_email(data.email)
        if (existing_user is not None and await self.orgs.get_member(
                org_id, existing_user.id) is not None):
            raise ConflictError(
                "This user is already a member of the organization.")

        now = datetime.now(UTC)
        if requester_id != org.owner_id and any(
                row.email.lower() == data.email.lower()
                and row.role == OrganizationRole.ADMIN
                for row in await self.orgs.list_open_invitations(org_id, now)):
            raise PermissionDeniedError(
                "An open admin invitation exists for this email; only the "
                "organization owner can replace it.")
        await self.orgs.delete_replaced_invitations(org_id, data.email, now)
        expires_at = now + timedelta(
            days=settings.ORGANIZATION_INVITATION_EXPIRE_DAYS)
        invitation = await self.orgs.create_invitation(
            organization_id=org_id, email=data.email, role=data.role,
            invited_by=requester_id, expires_at=expires_at)
        token = security.create_organization_invitation_token(
            str(invitation.id), settings.ORGANIZATION_INVITATION_EXPIRE_DAYS)
        link = f"{settings.FRONTEND_BASE_URL}/invitations/accept?token={token}"
        self.pending_invitation_emails.append(
            InvitationEmail(
                to=data.email, organization_name=org.name, link=link))
        await self._org_event(
            EventName.ORGANIZATION_INVITATION_CREATED, org, requester_id,
            user_id=existing_user.id if existing_user is not None else None,
            role=str(data.role))
        return InvitationRead.model_validate(invitation)

    async def list_invitations(self, org_id: UUID,
                               requester_id: UUID) -> list[InvitationRead]:
        await self._require_org(org_id)
        await self._require_role(org_id, requester_id, _MANAGER_ROLES)
        rows = await self.orgs.list_open_invitations(org_id, datetime.now(UTC))
        return [InvitationRead.model_validate(row) for row in rows]

    async def revoke_invitation(
            self, org_id: UUID, requester_id: UUID,
            invitation_id: UUID) -> None:
        org = await self._require_org(org_id)
        await self._require_role(org_id, requester_id, _MANAGER_ROLES)
        invitation = await self.orgs.lock_invitation(invitation_id)
        if (invitation is None or invitation.organization_id != org_id
                or invitation.accepted_at is not None):
            raise NotFoundError("Invitation not found.")
        if (invitation.role == OrganizationRole.ADMIN
                and requester_id != org.owner_id):
            raise PermissionDeniedError(
                "Only the organization owner can revoke admin invitations.")
        role = str(invitation.role)
        invitee = await self.users.get_by_email(invitation.email)
        await self.orgs.delete_invitation(invitation)
        await self._org_event(
            EventName.ORGANIZATION_INVITATION_REVOKED, org, requester_id,
            user_id=invitee.id if invitee is not None else None, role=role)

    async def accept_invitation(
            self, token: str, current_user_id: UUID) -> InvitationAcceptResult:
        payload = security.decode_or_invalid(
            token, security.ORGANIZATION_INVITATION_TOKEN_TYPE)

        invitation = await self.orgs.lock_invitation(UUID(payload["sub"]))
        if invitation is None:
            raise NotFoundError("Invitation no longer exists.")
        if invitation.accepted_at is not None:
            raise ConflictError("This invitation has already been accepted.")
        if invitation.expires_at < datetime.now(UTC):
            raise ValidationAppError("This invitation has expired.")

        current_user = await self.users.get_by_id(current_user_id)
        if (current_user is None
                or current_user.email.lower() != invitation.email.lower()):
            raise PermissionDeniedError(
                "This invitation was sent to a different email address. Log "
                "in with that account to accept it.")

        if await self.orgs.get_member(invitation.organization_id,
                                      current_user_id):
            raise ConflictError(
                "You are already a member of this organization.")

        inviter = await self.orgs.get_member(
            invitation.organization_id, invitation.invited_by)
        if inviter is None or OrganizationRole(
                inviter.role) not in _MANAGER_ROLES:
            raise NotFoundError("Invitation no longer exists.")

        try:
            async with self._db.begin_nested():
                await self.orgs.add_member(
                    organization_id=invitation.organization_id,
                    user_id=current_user_id, role=invitation.role)
                invitation.accepted_at = datetime.now(UTC)
                await self._db.flush()
        except IntegrityError as exc:
            raise ConflictError(
                "You are already a member of this organization.") from exc

        org = await self.orgs.get_by_id(invitation.organization_id)
        if org is not None:
            await self._org_event(
                EventName.ORGANIZATION_MEMBER_JOINED, org, current_user_id,
                user_id=current_user_id, role=str(invitation.role))
        return InvitationAcceptResult(
            organization_id=invitation.organization_id,
            role=OrganizationRole(invitation.role))

    async def _require_org(self, org_id: UUID) -> Organization:
        org = await self.orgs.get_by_id(org_id)
        if org is None:
            raise NotFoundError("Organization not found.")
        return org

    async def _require_member(
            self, org_id: UUID, user_id: UUID) -> OrganizationMember:
        member = await self.orgs.get_member(org_id, user_id)
        if member is None:
            raise NotFoundError("Organization not found.")
        return member

    async def _require_role(
            self, org_id: UUID, user_id: UUID,
            allowed: set[OrganizationRole]) -> OrganizationMember:
        member = await self._require_member(org_id, user_id)
        if OrganizationRole(member.role) not in allowed:
            raise PermissionDeniedError(
                "You do not have permission to perform this action.")
        return member
