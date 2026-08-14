from datetime import datetime, timedelta, UTC
from uuid import UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from config import settings
from domains.auth.model import User, UserProfile
from domains.auth.repository import UserRepository
from domains.auth.service import default_avatar_url
from domains.auth.schema import UserPublicRead
from domains.social.model import Connection
from domains.social.repository import ConnectionRepository
from domains.social.schema import ConnectionRead
from shared.enums.connection import ConnectionStatus
from shared.exceptions import (ConflictError, NotFoundError,
                               PermissionDeniedError, ValidationAppError)


class SocialService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self.connections = ConnectionRepository(session)
        self.users = UserRepository(session)

    def _row_to_read(self, connection: Connection, other_user: User | None,
                     profile: UserProfile | None, *, viewer_id: UUID
                     ) -> ConnectionRead:
        is_online = False
        avatar_url = None
        username = ""
        other_user_id = (
            connection.addressee_id if connection.requester_id == viewer_id
            else connection.requester_id)
        if other_user is not None:
            username = other_user.username
            avatar_url = profile.avatar_url if profile else None
            if other_user.last_seen_at is not None:
                threshold = timedelta(
                    minutes=settings.PRESENCE_ONLINE_THRESHOLD_MINUTES)
                is_online = (
                    datetime.now(UTC) - other_user.last_seen_at
                    ) < threshold
        return ConnectionRead(
            id=connection.id, requester_id=connection.requester_id,
            addressee_id=connection.addressee_id,
            status=ConnectionStatus(connection.status),
            created_at=connection.created_at,
            responded_at=connection.responded_at,
            other_user=UserPublicRead(
                id=other_user_id, username=username, avatar_url=avatar_url
                or default_avatar_url(username), is_online=is_online))

    async def _to_read(self, connection: Connection, *, viewer_id: UUID
                       ) -> ConnectionRead:
        other_user_id = (connection.addressee_id
                         if connection.requester_id == viewer_id
                         else connection.requester_id)
        other_user = await self.users.get_by_id(other_user_id)
        profile = (await self.users.get_profile(other_user_id)
                   if other_user is not None else None)
        return self._row_to_read(connection, other_user, profile,
                                 viewer_id=viewer_id)

    async def send_request(self, requester_id: UUID, addressee_username: str
                           ) -> ConnectionRead:
        addressee = await self.users.get_by_username(addressee_username)
        if addressee is None:
            raise NotFoundError("No user with that username.")
        if addressee.id == requester_id:
            raise ValidationAppError("You cannot connect with yourself.")
        existing = await self.connections.get_between(requester_id,
                                                      addressee.id)
        if existing is not None:
            raise ConflictError("A connection with this user already exists "
                                f"(status: {existing.status}).")
        try:
            async with self._db.begin_nested():
                connection = await self.connections.create(
                    requester_id=requester_id, addressee_id=addressee.id)
        except IntegrityError as exc:
            raise ConflictError("A connection with this user already exists."
                                ) from exc
        return await self._to_read(connection, viewer_id=requester_id)

    async def _require_connection(self, connection_id: UUID) -> Connection:
        connection = await self.connections.get_by_id(connection_id)
        if connection is None:
            raise NotFoundError("Connection not found.")
        return connection

    async def accept(self, connection_id: UUID, current_user_id: UUID
                     ) -> ConnectionRead:
        connection = await self._require_connection(connection_id)
        if connection.addressee_id != current_user_id:
            raise PermissionDeniedError(
                "Only the recipient of a connection request can accept it.")
        if connection.status != ConnectionStatus.PENDING:
            raise ConflictError(
                f"This request is already '{connection.status}'.")
        connection.status = ConnectionStatus.ACCEPTED
        connection.responded_at = datetime.now(UTC)
        await self._db.flush()
        return await self._to_read(connection, viewer_id=current_user_id)

    async def block(self, connection_id: UUID, current_user_id: UUID
                    ) -> ConnectionRead:
        connection = await self._require_connection(connection_id)
        if current_user_id not in (connection.requester_id,
                                   connection.addressee_id):
            raise PermissionDeniedError(
                "You are not a participant in this connection.")
        connection.status = ConnectionStatus.BLOCKED
        connection.responded_at = datetime.now(UTC)
        await self._db.flush()
        return await self._to_read(connection, viewer_id=current_user_id)

    async def remove(self, connection_id: UUID, current_user_id: UUID) -> None:
        connection = await self._require_connection(connection_id)
        if current_user_id not in (connection.requester_id,
                                   connection.addressee_id):
            raise PermissionDeniedError(
                "You are not a participant in this connection.")
        await self.connections.delete(connection)

    async def list_my_connections(
            self, user_id: UUID, status_filter: ConnectionStatus | None = None
            ) -> list[ConnectionRead]:
        rows = await self.connections.list_for_user_with_other_user(user_id)
        results = [self._row_to_read(
            connection, other_user, profile, viewer_id=user_id)
            for connection, other_user, profile in rows]
        if status_filter is not None:
            results = [r for r in results if r.status == status_filter]
        return results
