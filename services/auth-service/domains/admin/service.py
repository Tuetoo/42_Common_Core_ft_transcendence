import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
import httpx
import security
import structlog
from config import settings
from events_out import (PendingEvent, organization_event_payload,
                        user_event_payload)
from sqlalchemy.ext.asyncio import AsyncSession
from user_state_sync import commit_with_state, state_of
from domains.admin.repository import AdminRepository
from domains.admin.schema import (AdminStats, AdminUserDetail,
                                  AdminUserListItem, AdminUserSort,
                                  AdminUserUpdate, SortOrder)
from domains.auth.model import User, UserProfile
from domains.auth.repository import UserRepository
from domains.auth.schema import PROFILE_FIELDS
from domains.auth.service import (EmailDispatch, avatar_or_default,
                                  change_username, copy_changed_fields,
                                  flush_unique, is_user_online, profile_read,
                                  verification_link)
from domains.organization.model import Organization
from domains.organization.repository import OrganizationRepository
from shared.enums import OrganizationRole, SystemRole
from shared.events import EventName
from shared.exceptions import (ConflictError, NotFoundError,
                               PermissionDeniedError)
from shared.http_client import get_http_client
from shared.permissions import Permission, has_permission, sorted_permissions
from shared.schemas import PaginatedResponse, PaginationParams
from shared.security import CurrentUser

logger = structlog.get_logger(__name__)

_MODERATABLE_ROLES = frozenset({str(SystemRole.USER), str(SystemRole.GUEST)})
_STATS_TIMEOUT_SECONDS = 3.0
DELETED_EMAIL_DOMAIN = "deleted.alphamind.local"


def _stats_http_client() -> httpx.AsyncClient:
    return get_http_client(_STATS_TIMEOUT_SECONDS)


async def _fetch_service_stats(base_url: str,
                               service: str) -> dict[str, Any] | None:
    try:
        response = await _stats_http_client().get(f"{base_url}/internal/stats")
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            "admin_stats.service_unavailable", service=service, error=str(exc))
        return None
    if not isinstance(body, dict):
        logger.warning("admin_stats.unexpected_body", service=service)
        return None
    return body


@dataclass
class UnverifiedPurgeResult:
    candidates: int
    deleted: int = 0
    skipped: int = 0
    failed: int = 0

    @property
    def attempted(self) -> int:
        return self.deleted + self.failed


@dataclass
class LeftoverCleanupResult:
    candidates: int
    cleaned: int = 0
    failed: int = 0
    lookup_failed: bool = False


class AdminService:

    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self.admin = AdminRepository(session)
        self.users = UserRepository(session)
        self.orgs = OrganizationRepository(session)
        self.pending_events: list[PendingEvent] = []
        self.email_dispatch: EmailDispatch | None = None
        self.removed_avatar_url: str | None = None

    async def list_users(
            self, *, pagination: PaginationParams, q: str | None,
            role: SystemRole | None, is_active: bool | None,
            include_deleted: bool, sort: AdminUserSort,
            order: SortOrder) -> PaginatedResponse[AdminUserListItem]:
        rows, total = await self.admin.list_users(
            q=q, role=role, is_active=is_active,
            include_deleted=include_deleted, sort=sort, order=order,
            pagination=pagination)
        return PaginatedResponse[AdminUserListItem](
            items=[self._list_item(user, profile) for user, profile in rows],
            total=total, page=pagination.page, page_size=pagination.page_size)

    async def get_user(self, user_id: UUID) -> AdminUserDetail:
        user = await self.users.require(user_id)
        await self._db.flush()
        await self._db.refresh(user)
        profile = await self.users.get_profile(user_id)
        two_factor = await self.users.get_two_factor_secret(user_id)
        item = self._list_item(user, profile)
        return AdminUserDetail(
            **item.model_dump(), updated_at=user.updated_at,
            permissions=sorted_permissions(user.system_role), profile=(
                profile_read(profile, user.username)
                if profile is not None else None), two_factor_enabled=bool(
                    two_factor is not None and two_factor.enabled),
            oauth_providers=await self.admin.oauth_providers(user_id),
            organizations_count=await self.orgs.count_for_user(user_id))

    async def stats(self) -> AdminStats:
        return AdminStats(
            auth=await self.admin.auth_stats(), core=await
            _fetch_service_stats(settings.CORE_SERVICE_URL, "core"), ai=await
            _fetch_service_stats(settings.AI_SERVICE_URL, "ai"))

    async def update_user(
            self, actor: CurrentUser, user_id: UUID,
            data: AdminUserUpdate) -> AdminUserDetail:
        user = await self._require_live_user(user_id)
        previous_state = state_of(user)
        changes = data.model_dump(exclude_unset=True)
        is_self = actor.user_id == user.id
        changed: list[str] = []

        new_role = changes.get("role")
        role_changed = (
            new_role is not None and str(new_role) != user.system_role)
        new_active = changes.get("is_active")
        active_changed = (
            new_active is not None and new_active != user.is_active)
        if role_changed and is_self:
            raise PermissionDeniedError("You cannot change your own role.")
        if active_changed and new_active is False and is_self:
            raise PermissionDeniedError(
                "You cannot deactivate your own account.")
        if (role_changed and user.system_role == SystemRole.ADMIN) or (
                active_changed and new_active is False):
            await self._ensure_not_last_admin(user)

        await change_username(
            self.users, user, changes.get("username"), changed)

        new_email = changes.get("email")
        if new_email is not None and new_email != user.email.lower():
            other = await self.users.get_by_email(new_email)
            if other is not None and other.id != user.id:
                raise ConflictError(
                    "An account with this email already exists.")
            user.email = new_email
            changed.append("email")
            if "is_verified" not in changes:
                user.is_verified = False
                self.email_dispatch = EmailDispatch(
                    to=new_email, link=verification_link(user.id, new_email))
                changed.append("is_verified")
        new_verified = changes.get("is_verified")
        if new_verified is not None and new_verified != user.is_verified:
            user.is_verified = new_verified
            changed.append("is_verified")

        profile = await self.users.get_profile(user.id)
        if profile is not None:
            copy_changed_fields(profile, PROFILE_FIELDS, changes, changed)

        if role_changed:
            user.system_role = str(new_role)
        if active_changed:
            user.is_active = bool(new_active)
            if not user.is_active:
                await self.users.revoke_all_sessions(user.id)

        await flush_unique(
            self._db, "This username or email is already taken.")

        if role_changed or active_changed:
            await commit_with_state(self._db, user, previous_state)
        if role_changed:
            self._user_event(
                EventName.USER_ROLE_CHANGED, user, actor.user_id, ["role"])
        if active_changed:
            self._user_event((
                EventName.USER_REACTIVATED
                if user.is_active else EventName.USER_DEACTIVATED), user,
                             actor.user_id, ["is_active"])
        if changed:
            self._user_event(
                EventName.USER_UPDATED, user, actor.user_id, changed)
        return await self.get_user(user.id)

    async def delete_user(self, actor: CurrentUser, user_id: UUID) -> None:
        user = await self._require_live_user(user_id)
        if actor.user_id == user.id:
            raise PermissionDeniedError("You cannot delete your own account.")
        await self._delete(user, actor.user_id)

    async def purge_unverified_accounts(self) -> UnverifiedPurgeResult:
        cutoff = datetime.now(UTC) - timedelta(
            days=settings.UNVERIFIED_ACCOUNT_RETENTION_DAYS)
        candidates = await self.admin.unverified_account_ids(cutoff)
        result = UnverifiedPurgeResult(candidates=len(candidates))
        for user_id in candidates:
            events_before = len(self.pending_events)
            try:
                await self.admin.lock_active_admin_ids()
                user = await self.admin.lock_unverified_account(
                    user_id, cutoff)
                if user is None:
                    result.skipped += 1
                    continue
                await self._delete(user, user.id)
            except ConflictError as exc:
                await self._db.rollback()
                del self.pending_events[events_before:]
                result.skipped += 1
                logger.warning(
                    "unverified_account_purge_skipped", user_id=str(user_id),
                    reason=exc.message)
                continue
            except Exception as exc:
                await self._db.rollback()
                del self.pending_events[events_before:]
                result.failed += 1
                logger.error(
                    "unverified_account_purge_failed", user_id=str(user_id),
                    error=str(exc))
                continue
            result.deleted += 1
        logger.info(
            "unverified_accounts_purged", candidates=result.candidates,
            deleted=result.deleted, skipped=result.skipped,
            failed=result.failed,
            retention_days=settings.UNVERIFIED_ACCOUNT_RETENTION_DAYS)
        return result

    async def _delete(self, user: User, actor_id: UUID) -> None:
        await self._ensure_not_last_admin(user)
        previous_state = state_of(user)
        username = user.username
        await self._remove_organization_ties_and_sessions(
            user, actor_id, user.email if user.is_verified else None)

        user.deleted_at = datetime.now(UTC)
        user.is_active = False
        user.email = f"deleted-{user.id.hex}@{DELETED_EMAIL_DOMAIN}"
        user.username = f"deleted_{user.id.hex}"
        user.password_hash = security.hash_password(secrets.token_urlsafe(32))
        profile = await self.users.get_profile(user.id)
        if profile is not None:
            self.removed_avatar_url = profile.avatar_url
            for field in (*PROFILE_FIELDS, "avatar_url"):
                setattr(profile, field, None)
        await self._db.flush()
        await self.admin.delete_oauth_accounts(user.id)
        await self.admin.delete_two_factor_secret(user.id)
        await self.admin.delete_connections(user.id)
        await commit_with_state(self._db, user, previous_state)
        self._user_event(
            EventName.USER_DELETED, user, actor_id,
            ["deleted_at", "email", "is_active", "username"],
            username=username)

    async def _remove_organization_ties_and_sessions(
            self, user: User, actor_id: UUID, email: str | None) -> None:
        await self._revoke_sent_invitations(user)
        await self.orgs.delete_invitations_of_user(user.id, email)
        await self._transfer_owned_organizations(user, actor_id)
        await self.orgs.remove_memberships_of_user(user.id)
        await self.users.delete_sessions(user.id)

    async def clean_deleted_account_leftovers(self) -> LeftoverCleanupResult:
        try:
            candidates = await self.admin.deleted_ids_with_leftovers()
        except Exception as exc:
            await self._db.rollback()
            logger.error(
                "deleted_account_leftovers_lookup_failed", error=str(exc))
            return LeftoverCleanupResult(candidates=0, lookup_failed=True)
        result = LeftoverCleanupResult(candidates=len(candidates))
        for user_id in candidates:
            events_before = len(self.pending_events)
            try:
                user = await self.users.lock(user_id)
                if user is None or user.deleted_at is None:
                    continue
                await self._remove_organization_ties_and_sessions(
                    user, user.id, None)
                await self._db.commit()
            except Exception as exc:
                await self._db.rollback()
                del self.pending_events[events_before:]
                result.failed += 1
                logger.error(
                    "deleted_account_leftovers_failed", user_id=str(user_id),
                    error=str(exc))
                continue
            result.cleaned += 1
        logger.info(
            "deleted_account_leftovers_cleaned",
            candidates=result.candidates, cleaned=result.cleaned,
            failed=result.failed)
        return result

    async def _transfer_owned_organizations(
            self, user: User, actor_id: UUID) -> None:
        for org in await self.orgs.list_owned_by(user.id):
            successor = await self.orgs.successor_member(
                org.id, excluding=user.id)
            if successor is None:
                await self._delete_orphaned_organization(org, actor_id)
                continue
            org.owner_id = successor.user_id
            successor.role = OrganizationRole.OWNER
            await self._db.flush()
            member_ids = await self.orgs.list_member_ids(org.id)
            self.pending_events.append((
                EventName.ORGANIZATION_ROLE_CHANGED,
                organization_event_payload(
                    organization_id=org.id, organization_name=org.name,
                    actor_id=actor_id, user_id=successor.user_id,
                    role=str(OrganizationRole.OWNER), member_ids=member_ids)))
            logger.info(
                "organization_owner_transferred", organization_id=str(org.id),
                from_user=str(user.id), to_user=str(successor.user_id))

    async def _revoke_sent_invitations(self, user: User) -> None:
        now = datetime.now(UTC)
        for invitation in await self.orgs.list_open_invitations_sent_by(
                user.id, now):
            org = await self.orgs.get_by_id(invitation.organization_id)
            if org is None:
                continue
            invitee = await self.users.get_by_email(invitation.email)
            self.pending_events.append((
                EventName.ORGANIZATION_INVITATION_REVOKED,
                organization_event_payload(
                    organization_id=org.id, organization_name=org.name,
                    actor_id=user.id,
                    user_id=invitee.id if invitee is not None else None,
                    role=str(invitation.role),
                    member_ids=await self.orgs.list_member_ids(org.id))))

    async def _delete_orphaned_organization(
            self, org: Organization, actor_id: UUID) -> None:
        member_ids = await self.orgs.list_member_ids(org.id)
        self.pending_events.append((
            EventName.ORGANIZATION_DELETED,
            organization_event_payload(
                organization_id=org.id, organization_name=org.name,
                actor_id=actor_id, user_id=None, role=None,
                member_ids=member_ids)))
        await self.orgs.delete(org)
        logger.info(
            "organization_deleted_without_successor",
            organization_id=str(org.id))

    async def reset_two_factor(
            self, actor: CurrentUser, user_id: UUID) -> None:
        user = await self._require_live_user(user_id)
        if actor.user_id == user.id:
            raise PermissionDeniedError(
                "Use /api/v1/auth/2fa/disable with a code to turn off your "
                "own 2FA.")
        if await self.users.get_two_factor_secret(user.id) is None:
            return
        await self.admin.delete_two_factor_secret(user.id)
        await self._db.flush()
        self._user_event(
            EventName.USER_UPDATED, user, actor.user_id,
            ["two_factor_enabled"])
        logger.warning(
            "admin_two_factor_reset", user_id=str(user.id),
            actor_id=str(actor.user_id))

    async def set_active(
            self, actor: CurrentUser, user_id: UUID,
            active: bool) -> AdminUserDetail:
        user = await self._require_live_user(user_id)
        if not has_permission(actor.role, Permission.USER_MANAGE) and (
                user.system_role not in _MODERATABLE_ROLES):
            raise PermissionDeniedError(
                "Moderators can only deactivate or reactivate users and "
                "guests.")
        if actor.user_id == user.id:
            raise PermissionDeniedError(
                "You cannot deactivate or reactivate your own account.")
        if user.is_active == active:
            return await self.get_user(user.id)
        if not active:
            await self._ensure_not_last_admin(user)
        previous_state = state_of(user)
        user.is_active = active
        if not active:
            await self.users.revoke_all_sessions(user.id)
        await commit_with_state(self._db, user, previous_state)
        self._user_event((
            EventName.USER_REACTIVATED if active else
            EventName.USER_DEACTIVATED), user, actor.user_id, ["is_active"])
        return await self.get_user(user.id)

    async def _require_live_user(self, user_id: UUID) -> User:
        await self.admin.lock_active_admin_ids()
        user = await self.users.lock(user_id)
        if user is None:
            raise NotFoundError("User not found.")
        if user.deleted_at is not None:
            raise ConflictError("This user has been deleted.")
        return user

    async def _ensure_not_last_admin(self, user: User) -> None:
        if user.system_role != SystemRole.ADMIN:
            return
        admin_ids = await self.admin.lock_active_admin_ids()
        if user.id in admin_ids and len(admin_ids) <= 1:
            raise ConflictError(
                "This is the last active admin; promote another admin first.")

    def _user_event(
            self, name: EventName, user: User, actor_id: UUID,
            changed: list[str], *, username: str | None = None) -> None:
        self.pending_events.append((
            name,
            user_event_payload(
                user_id=user.id, actor_id=actor_id,
                username=username or user.username, role=user.system_role,
                is_active=user.is_active, changed=changed)))

    @staticmethod
    def _list_item(
            user: User, profile: UserProfile | None) -> AdminUserListItem:
        return AdminUserListItem(
            id=user.id, email=user.email, username=user.username,
            system_role=user.system_role, is_active=user.is_active,
            is_verified=user.is_verified, is_online=is_user_online(
                user.last_seen_at), avatar_url=avatar_or_default(
                    profile.avatar_url if profile else None, user.username),
            first_name=profile.first_name if profile else None,
            last_name=profile.last_name if profile else None,
            created_at=user.created_at, last_seen_at=user.last_seen_at,
            deleted_at=user.deleted_at)
