from datetime import datetime
from typing import Any
from uuid import UUID
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from domains.admin.schema import AdminUserSort, SortOrder
from domains.auth.model import OAuthAccount, TwoFactorSecret, User, UserProfile
from domains.social.model import Connection
from shared.database import count_query
from shared.enums import SystemRole
from shared.schemas import PaginationParams

_SORT_COLUMNS: dict[AdminUserSort, Any] = {
    AdminUserSort.CREATED_AT: User.created_at,
    AdminUserSort.EMAIL: User.email,
    AdminUserSort.USERNAME: User.username,
    AdminUserSort.LAST_SEEN_AT: User.last_seen_at
}


def _like_pattern(text: str) -> str:
    escaped = (
        text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))
    return f"%{escaped}%"


class AdminRepository:

    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def list_users(
        self, *, q: str | None, role: SystemRole | None,
        is_active: bool | None, include_deleted: bool, sort: AdminUserSort,
        order: SortOrder, pagination: PaginationParams
    ) -> tuple[list[tuple[User, UserProfile | None]], int]:
        conditions: list[Any] = []
        if q:
            pattern = _like_pattern(q.strip())
            conditions.append(
                or_(
                    User.email.ilike(pattern, escape="\\"),
                    User.username.ilike(pattern, escape="\\")))
        if role is not None:
            conditions.append(User.system_role == str(role))
        if is_active is not None:
            conditions.append(User.is_active.is_(is_active))
        if not include_deleted:
            conditions.append(User.deleted_at.is_(None))

        total = await count_query(self._db, select(User.id).where(*conditions))

        column = _SORT_COLUMNS[sort]
        ordered = column.asc() if order == "asc" else column.desc()
        stmt = (
            select(User, UserProfile).outerjoin(
                UserProfile,
                UserProfile.user_id == User.id).where(*conditions).order_by(
                    ordered.nulls_last(), User.id.asc()).offset(
                        pagination.offset).limit(pagination.limit))
        result = await self._db.execute(stmt)
        rows = [(user, profile) for user, profile in result.all()]
        return rows, total

    async def lock_active_admin_ids(self) -> list[UUID]:
        result = await self._db.execute(
            select(User.id).where(
                User.system_role == str(SystemRole.ADMIN),
                User.is_active.is_(True),
                User.deleted_at.is_(None)).order_by(User.id).with_for_update())
        return list(result.scalars().all())

    @staticmethod
    def _unverified_before(created_before: datetime) -> list[Any]:
        linked = select(OAuthAccount.id).where(OAuthAccount.user_id == User.id)
        # last_seen_at IS NULL means the account never held a token, so it
        # cannot have uploaded an avatar.
        return [
            User.is_verified.is_(False),
            User.deleted_at.is_(None),
            User.last_seen_at.is_(None), User.created_at < created_before,
            ~linked.exists()
        ]

    async def unverified_account_ids(self,
                                     created_before: datetime) -> list[UUID]:
        result = await self._db.execute(
            select(User.id).where(
                *self._unverified_before(created_before)).order_by(
                    User.created_at, User.id))
        return list(result.scalars().all())

    async def lock_unverified_account(
            self, user_id: UUID, created_before: datetime) -> User | None:
        result = await self._db.execute(
            select(User).where(
                User.id == user_id,
                *self._unverified_before(created_before)).with_for_update())
        return result.scalar_one_or_none()

    async def oauth_providers(self, user_id: UUID) -> list[str]:
        result = await self._db.execute(
            select(
                OAuthAccount.provider).where(OAuthAccount.user_id == user_id))
        return sorted(set(result.scalars().all()))

    async def delete_oauth_accounts(self, user_id: UUID) -> None:
        await self._db.execute(
            delete(OAuthAccount).where(
                OAuthAccount.user_id == user_id).execution_options(
                    synchronize_session="fetch"))

    async def delete_two_factor_secret(self, user_id: UUID) -> None:
        await self._db.execute(
            delete(TwoFactorSecret).where(
                TwoFactorSecret.user_id == user_id).execution_options(
                    synchronize_session="fetch"))

    async def delete_connections(self, user_id: UUID) -> None:
        await self._db.execute(
            delete(Connection).where(
                or_(
                    Connection.requester_id == user_id,
                    Connection.addressee_id == user_id)).execution_options(
                        synchronize_session="fetch"))

    async def auth_stats(self) -> dict[str, Any]:
        live = User.deleted_at.is_(None)
        counts = (
            await self._db.execute(
                select(
                    func.count().filter(live),
                    func.count().filter(live, User.is_active.is_(True)),
                    func.count().filter(
                        User.deleted_at.is_not(None))).select_from(User)
            )).one()
        total, active, deleted = (int(value) for value in counts)
        by_role: dict[str, int] = {str(role): 0 for role in SystemRole}
        rows = await self._db.execute(
            select(User.system_role,
                   func.count()).where(live).group_by(User.system_role))
        for role_name, count in rows.all():
            by_role[str(role_name)] = int(count)
        return {
            "users_total": total,
            "users_active": active,
            "users_deleted": deleted,
            "users_by_role": by_role
        }
