from datetime import datetime, UTC
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from domains.auth.model import (OAuthAccount, TwoFactorSecret, User,
                                UserProfile, UserSession, UserSettings)


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def get_by_id(self, user_id: UUID) -> User | None:
        result = await self._db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        result = await self._db.execute(select(User).where(User.email == email
                                                           ))
        return result.scalar_one_or_none()

    async def get_by_username(self, username: str) -> User | None:
        result = await self._db.execute(select(User).where(
            User.username == username))
        return result.scalar_one_or_none()

    async def create(self, *, email: str, username: str, password_hash: str
                     ) -> User:
        user = User(email=email, username=username,
                    password_hash=password_hash)
        self._db.add(user)
        await self._db.flush()
        return user

    async def create_profile(self, user_id: UUID) -> UserProfile:
        profile = UserProfile(user_id=user_id)
        self._db.add(profile)
        await self._db.flush()
        return profile

    async def create_settings(self, user_id: UUID) -> UserSettings:
        settings_row = UserSettings(user_id=user_id)
        self._db.add(settings_row)
        await self._db.flush()
        return settings_row

    async def get_profile(self, user_id: UUID) -> UserProfile | None:
        result = await self._db.execute(select(UserProfile).where(
            UserProfile.user_id == user_id))
        return result.scalar_one_or_none()

    async def get_settings(self, user_id: UUID) -> UserSettings | None:
        result = await self._db.execute(select(UserSettings).where(
            UserSettings.user_id == user_id))
        return result.scalar_one_or_none()

    async def get_two_factor_secret(self, user_id: UUID
                                    ) -> TwoFactorSecret | None:
        result = await self._db.execute(select(
            TwoFactorSecret).where(TwoFactorSecret.user_id == user_id))
        return result.scalar_one_or_none()

    async def upsert_two_factor_secret(self, user_id: UUID, secret: str
                                       ) -> TwoFactorSecret:
        stmt = pg_insert(TwoFactorSecret).values(
            user_id=user_id, secret=secret, enabled=False)
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id"],
            set_=dict(secret=secret, enabled=False))
        await self._db.execute(stmt)
        await self._db.flush()
        result = await self._db.execute(select(TwoFactorSecret).where(
            TwoFactorSecret.user_id == user_id))
        return result.scalar_one()

    async def get_oauth_account(self, provider: str, provider_user_id: str
                                ) -> OAuthAccount | None:
        result = await self._db.execute(select(OAuthAccount).where(
            OAuthAccount.provider == provider,
            OAuthAccount.provider_user_id == provider_user_id))
        return result.scalar_one_or_none()

    async def create_oauth_account(self, *, user_id: UUID, provider: str,
                                   provider_user_id: str) -> OAuthAccount:
        row = OAuthAccount(user_id=user_id, provider=provider,
                           provider_user_id=provider_user_id)
        self._db.add(row)
        await self._db.flush()
        return row

    async def create_session(
            self, *, user_id: UUID, refresh_token: str, expires_at: datetime,
            ip_address: str | None = None, device_info: str | None = None
            ) -> UserSession:
        row = UserSession(user_id=user_id, refresh_token=refresh_token,
                          expires_at=expires_at, ip_address=ip_address,
                          device_info=device_info)
        self._db.add(row)
        await self._db.flush()
        return row

    async def get_session_by_refresh_token(self, refresh_token: str
                                           ) -> UserSession | None:
        result = await self._db.execute(select(UserSession).where(
            UserSession.refresh_token == refresh_token))
        return result.scalar_one_or_none()

    async def revoke_session(self, session: UserSession) -> None:
        session.revoked_at = datetime.now(UTC)
        await self._db.flush()

    async def count_active_sessions(self, user_id: UUID) -> int:
        result = await self._db.execute(select(UserSession).where(
            UserSession.user_id == user_id, UserSession.revoked_at.is_(None),
            UserSession.expires_at > datetime.now(UTC)))
        return len(result.scalars().all())
