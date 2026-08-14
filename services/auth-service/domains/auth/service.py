import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
import httpx
import security
import structlog
from config import settings
from sqlalchemy.ext.asyncio import AsyncSession
from domains.auth.repository import UserRepository
from domains.auth.schema import (MeRead, MeUpdate, TokenPair,
                                 TwoFactorSetupResponse, UserProfileRead,
                                 UserRegister, UserSettingsRead)
from shared.email import send_password_reset_email, send_verification_email
from shared.events import publish_cross_service
from shared.events_registry import EventName
from shared.exceptions import (ConflictError, InvalidCredentialsError,
                               NotFoundError, ValidationAppError)
from shared.redis import get_cache_client

logger = structlog.get_logger(__name__)

_OAUTH_PROVIDERS: dict[str, dict[str, str]] = {
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "userinfo_url": "https://openidconnect.googleapis.com/v1/userinfo",
        "scope": "openid email profile",
    },
    "github": {
        "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "userinfo_url": "https://api.github.com/user",
        "scope": "read:user user:email",
    }
}
_OAUTH_STATE_TTL_SECONDS = 300


class TwoFactorRequiredError(InvalidCredentialsError):
    code = "two_factor_required"


class EmailNotVerifiedError(InvalidCredentialsError):
    code = "email_not_verified"


class UnsupportedOAuthProviderError(ValidationAppError):
    code = "unsupported_oauth_provider"


def default_avatar_url(username: str) -> str:
    return f"https://api.dicebear.com/7.x/identicon/svg?seed={username}"


def _extract_oauth_identity(provider: str, userinfo: dict[str, Any]
                            ) -> tuple[str, str | None]:
    if provider == "google":
        return str(userinfo["sub"]), userinfo.get("email")
    if provider == "github":
        return str(userinfo["id"]), userinfo.get("email")
    raise UnsupportedOAuthProviderError(
        f"Unknown OAuth provider '{provider}'.")


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self.users = UserRepository(session)

    async def register(self, data: UserRegister) -> None:
        if await self.users.get_by_email(data.email) is not None:
            raise ConflictError("An account with this email already exists.")
        if await self.users.get_by_username(data.username) is not None:
            raise ConflictError("This username is already taken.")
        password_hash = security.hash_password(data.password)
        user = await self.users.create(email=data.email,
                                       username=data.username,
                                       password_hash=password_hash)
        await self.users.create_profile(user.id)
        await self.users.create_settings(user.id)
        token = security.create_email_verification_token(str(user.id),
                                                         data.email)
        link = f"{settings.FRONTEND_BASE_URL}/verify-email?token={token}"
        await send_verification_email(
            settings=settings, to=data.email, verification_link=link,
            expire_hours=settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS)
        logger.info("user_registered", user_id=str(user.id))

    async def verify_email(self, token: str) -> None:
        try:
            payload = security.decode_special_token(
                token, security.EMAIL_VERIFICATION_TOKEN_TYPE)
        except security.SpecialTokenError as exc:
            raise ValidationAppError(str(exc)) from exc
        user = await self.users.get_by_id(UUID(payload["sub"]))
        if user is None:
            raise NotFoundError("Account no longer exists.")
        if user.email != payload.get("email"):
            raise ValidationAppError(
                "This verification link is no longer valid.")
        user.is_verified = True
        await self._db.flush()

    async def request_password_reset(self, email: str) -> None:
        user = await self.users.get_by_email(email)
        if user is None:
            logger.info("password_reset_requested_unknown_email")
            return
        token = security.create_password_reset_token(str(user.id))
        link = f"{settings.FRONTEND_BASE_URL}/reset-password?token={token}"
        await send_password_reset_email(
            settings=settings, to=email, reset_link=link,
            expire_hours=settings.PASSWORD_RESET_TOKEN_EXPIRE_HOURS)

    async def confirm_password_reset(self, token: str, new_password: str
                                     ) -> None:
        try:
            payload = security.decode_special_token(
                token, security.PASSWORD_RESET_TOKEN_TYPE)
        except security.SpecialTokenError as exc:
            raise ValidationAppError(str(exc)) from exc
        user = await self.users.get_by_id(UUID(payload["sub"]))
        if user is None:
            raise NotFoundError("Account no longer exists.")
        user.password_hash = security.hash_password(new_password)
        await self._db.flush()

    async def _issue_token_pair(
            self, *, user_id: UUID, role: str, ip_address: str | None,
            device_info: str | None, mark_online: bool) -> TokenPair:
        access_token = security.create_access_token(
            str(user_id), extra_claims={"role": role})
        refresh_token = security.create_refresh_token(str(user_id))
        expires_at = datetime.now(UTC) + timedelta(
            days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
        await self.users.create_session(
            user_id=user_id, refresh_token=refresh_token,
            expires_at=expires_at, ip_address=ip_address,
            device_info=device_info)
        if mark_online:
            user = await self.users.get_by_id(user_id)
            if user is not None:
                user.last_seen_at = datetime.now(UTC)
                await self._db.flush()
            await publish_cross_service(
                settings, EventName.USER_PRESENCE_CHANGED,
                {"user_id": str(user_id), "is_online": True})
        return TokenPair(access_token=access_token,
                         refresh_token=refresh_token)

    async def login(self, *, email: str, password: str, totp_code: str | None,
                    ip_address: str | None = None,
                    device_info: str | None = None) -> TokenPair:
        user = await self.users.get_by_email(email)
        if user is None or not security.verify_password(password,
                                                        user.password_hash):
            raise InvalidCredentialsError()
        if not user.is_active:
            raise InvalidCredentialsError("This account has been deactivated.")
        if not user.is_verified:
            raise EmailNotVerifiedError(
                "Please verify your email before logging in.")
        two_factor = await self.users.get_two_factor_secret(user.id)
        if two_factor is not None and two_factor.enabled:
            if not totp_code:
                raise TwoFactorRequiredError()
            if not security.verify_totp_code(two_factor.secret, totp_code):
                raise InvalidCredentialsError("Invalid 2FA code.")
        return await self._issue_token_pair(
            user_id=user.id, role=user.system_role, ip_address=ip_address,
            device_info=device_info, mark_online=True)

    async def refresh(self, refresh_token: str) -> TokenPair:
        session = await self.users.get_session_by_refresh_token(refresh_token)
        if (session is None or session.revoked_at is not None
                or session.expires_at < datetime.now(UTC)):
            raise InvalidCredentialsError(
                "Refresh token is invalid or expired.")
        user = await self.users.get_by_id(session.user_id)
        if user is None or not user.is_active:
            raise InvalidCredentialsError()
        await self.users.revoke_session(session)
        return await self._issue_token_pair(
            user_id=user.id, role=user.system_role,
            ip_address=session.ip_address, device_info=session.device_info,
            mark_online=False)

    async def logout(self, refresh_token: str) -> None:
        session = await self.users.get_session_by_refresh_token(refresh_token)
        if session is not None and session.revoked_at is None:
            user_id = session.user_id
            await self.users.revoke_session(session)
            remaining = await self.users.count_active_sessions(user_id)
            if remaining == 0:
                await publish_cross_service(
                    settings, EventName.USER_PRESENCE_CHANGED,
                    {"user_id": str(user_id), "is_online": False})

    async def setup_two_factor(self, user_id: UUID, email: str
                               ) -> TwoFactorSetupResponse:
        secret = security.generate_totp_secret()
        await self.users.upsert_two_factor_secret(user_id, secret)
        uri = security.get_totp_provisioning_uri(secret, email)
        return TwoFactorSetupResponse(secret=secret, provisioning_uri=uri)

    async def verify_two_factor(self, user_id: UUID, code: str) -> None:
        row = await self.users.get_two_factor_secret(user_id)
        if row is None:
            raise NotFoundError("2FA has not been set up for this account.")
        if not security.verify_totp_code(row.secret, code):
            raise ValidationAppError("Invalid 2FA code.")
        row.enabled = True
        await self._db.flush()

    async def disable_two_factor(self, user_id: UUID, code: str) -> None:
        row = await self.users.get_two_factor_secret(user_id)
        if row is None or not row.enabled:
            raise NotFoundError("2FA is not currently enabled.")
        if not security.verify_totp_code(row.secret, code):
            raise ValidationAppError("Invalid 2FA code.")
        row.enabled = False
        await self._db.flush()

    async def oauth_authorize_url(self, provider: str) -> str:
        provider_cfg = _OAUTH_PROVIDERS.get(provider)
        if provider_cfg is None:
            raise UnsupportedOAuthProviderError(
                f"Unknown OAuth provider '{provider}'.")
        client_id = getattr(settings, f"{provider.upper()}_CLIENT_ID")
        state = secrets.token_urlsafe(24)
        await get_cache_client(settings).set(f"oauth:state:{state}", provider,
                                             ex=_OAUTH_STATE_TTL_SECONDS)
        redirect_uri = (f"{settings.OAUTH_REDIRECT_BASE_URL}/api/v1/auth/"
                        f"oauth/{provider}/callback")
        params = httpx.QueryParams({"client_id": client_id,
                                    "redirect_uri": redirect_uri,
                                    "response_type": "code",
                                    "scope": provider_cfg["scope"],
                                    "state": state})
        return f"{provider_cfg['authorize_url']}?{params}"

    async def _generate_unique_username(
            self, email: str | None, fallback_seed: str) -> str:
        base = (email.split("@")[0] if email else f"user_{fallback_seed}")[:24]
        candidate = base
        suffix = 0
        while await self.users.get_by_username(candidate) is not None:
            suffix += 1
            candidate = f"{base}{suffix}"
        return candidate

    async def oauth_callback(self, provider: str, code: str, state: str
                             ) -> TokenPair:
        provider_cfg = _OAUTH_PROVIDERS.get(provider)
        if provider_cfg is None:
            raise UnsupportedOAuthProviderError(
                f"Unknown OAuth provider '{provider}'.")
        cache = get_cache_client(settings)
        stored_provider = await cache.get(f"oauth:state:{state}")
        if stored_provider != provider:
            raise InvalidCredentialsError("Invalid or expired OAuth state.")
        await cache.delete(f"oauth:state:{state}")
        client_id = getattr(settings, f"{provider.upper()}_CLIENT_ID")
        client_secret = getattr(settings, f"{provider.upper()}_CLIENT_SECRET")
        redirect_uri = (f"{settings.OAUTH_REDIRECT_BASE_URL}/api/v1/auth/"
                        f"oauth/{provider}/callback")
        async with httpx.AsyncClient(timeout=10.0) as client:
            token_response = await client.post(
                provider_cfg["token_url"], data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code"},
                headers={"Accept": "application/json"})
            token_response.raise_for_status()
            provider_access_token = token_response.json()["access_token"]
            userinfo_response = await client.get(
                provider_cfg["userinfo_url"],
                headers={"Authorization": f"Bearer {provider_access_token}"})
            userinfo_response.raise_for_status()
            userinfo = userinfo_response.json()
        provider_user_id, email = _extract_oauth_identity(provider, userinfo)
        existing_link = await self.users.get_oauth_account(
            provider, provider_user_id)
        if existing_link is not None:
            user = await self.users.get_by_id(existing_link.user_id)
            if user is None:
                raise NotFoundError("Linked account no longer exists.")
        else:
            user = await self.users.get_by_email(email) if email else None
            if user is None:
                username = await self._generate_unique_username(
                    email, provider_user_id)
                user = await self.users.create(
                    email=email
                    or f"{provider}_{provider_user_id}@users.alphamind.local",
                    username=username, password_hash=security.hash_password(
                        secrets.token_urlsafe(32)))
                user.is_verified = bool(email)
                await self.users.create_profile(user.id)
                await self.users.create_settings(user.id)
            await self.users.create_oauth_account(
                user_id=user.id, provider=provider,
                provider_user_id=provider_user_id)
        return await self._issue_token_pair(
            user_id=user.id, role=user.system_role, ip_address=None,
            device_info=f"oauth:{provider}", mark_online=True)

    async def get_me(self, user_id: UUID) -> MeRead:
        user = await self.users.get_by_id(user_id)
        if user is None:
            raise NotFoundError("User not found.")
        profile = await self.users.get_profile(user_id)
        settings_row = await self.users.get_settings(user_id)
        return MeRead(id=user.id, email=user.email, username=user.username,
                      system_role=user.system_role, is_active=user.is_active,
                      is_verified=user.is_verified,
                      last_seen_at=user.last_seen_at,
                      created_at=user.created_at,
                      profile=(UserProfileRead(
                          first_name=profile.first_name,
                          last_name=profile.last_name,
                          avatar_url=profile.avatar_url or default_avatar_url(
                              user.username), bio=profile.bio,
                          timezone=profile.timezone, country=profile.country)
                          if profile else None),
                      settings=UserSettingsRead.model_validate(settings_row)
                      if settings_row else None)

    async def update_me(self, user_id: UUID, data: MeUpdate) -> MeRead:
        user = await self.users.get_by_id(user_id)
        if user is None:
            raise NotFoundError("User not found.")
        if data.username and data.username != user.username:
            if await self.users.get_by_username(data.username) is not None:
                raise ConflictError("This username is already taken.")
            user.username = data.username
        profile = await self.users.get_profile(user_id)
        if profile is not None:
            for field in ("first_name", "last_name", "bio", "timezone",
                          "country"):
                value = getattr(data, field)
                if value is not None:
                    setattr(profile, field, value)
        settings_row = await self.users.get_settings(user_id)
        if settings_row is not None:
            for field in ("preferred_currency", "theme", "default_model",
                          "notification_preferences"):
                value = getattr(data, field)
                if value is not None:
                    setattr(settings_row, field, value)
        await self._db.flush()
        return await self.get_me(user_id)

    async def set_avatar_url(self, user_id: UUID, avatar_url: str) -> None:
        profile = await self.users.get_profile(user_id)
        if profile is None:
            raise NotFoundError("Profile not found.")
        profile.avatar_url = avatar_url
        await self._db.flush()
