from uuid import UUID
from sqlalchemy import case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from domains.auth.model import User, UserProfile
from domains.social.model import Connection
from shared.enums import ConnectionStatus


class ConnectionRepository:

    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def get_by_id(
            self, connection_id: UUID, *,
            for_update: bool = False) -> Connection | None:
        stmt = select(Connection).where(Connection.id == connection_id)
        if for_update:
            stmt = stmt.with_for_update().execution_options(
                populate_existing=True)
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_between(
            self, user_a: UUID, user_b: UUID) -> Connection | None:
        result = await self._db.execute(
            select(Connection).where(
                or_((Connection.requester_id == user_a)
                    & (Connection.addressee_id == user_b),
                    (Connection.requester_id == user_b)
                    & (Connection.addressee_id == user_a))))
        return result.scalar_one_or_none()

    async def list_friend_ids(self, user_id: UUID) -> list[UUID]:
        other_user_id = case(
            (Connection.requester_id == user_id, Connection.addressee_id),
            else_=Connection.requester_id)
        result = await self._db.execute(
            select(other_user_id).where(
                or_(
                    Connection.requester_id == user_id,
                    Connection.addressee_id == user_id),
                Connection.status == ConnectionStatus.ACCEPTED))
        return list(result.scalars().all())

    async def create(
            self, *, requester_id: UUID, addressee_id: UUID) -> Connection:
        connection = Connection(
            requester_id=requester_id, addressee_id=addressee_id,
            status="pending")
        self._db.add(connection)
        await self._db.flush()
        return connection

    async def list_for_user_with_other_user(
            self, user_id: UUID
    ) -> list[tuple[Connection, User, UserProfile | None]]:
        other_user_id = case(
            (Connection.requester_id == user_id, Connection.addressee_id),
            else_=Connection.requester_id)
        result = await self._db.execute(
            select(Connection, User,
                   UserProfile).join(User, User.id == other_user_id).outerjoin(
                       UserProfile, UserProfile.user_id == User.id).where(
                           or_(
                               Connection.requester_id == user_id,
                               Connection.addressee_id == user_id),
                           ~((Connection.status == ConnectionStatus.BLOCKED)
                             & Connection.blocked_by.is_not(None)
                             & (Connection.blocked_by != user_id))))
        return [tuple(row) for row in result.all()]

    async def delete(self, connection: Connection) -> None:
        await self._db.delete(connection)
        await self._db.flush()
