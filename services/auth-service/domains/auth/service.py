import base64
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID
import httpx
import security
import structlog
from config import settings
from events_out import PendingEvent, user_event_payload
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from user_state_sync import mirror_user_safely
from domains.auth.model import (SessionRevokeReason, TwoFactorSecret, User,
                                UserProfile, UserSession)
from domains.auth.repository import UserRepository
from domains.auth.schema import (PROFILE_FIELDS, MeRead, MeUpdate,
                                 OAuthTwoFactorChallenge, TokenPair,
                                 TwoFactorSetupResponse, UserProfileRead,
                                 UserRegister, UserSettingsRead,
                                 normalize_email)
from shared.email import (send_password_reset_email, send_quietly,
                          send_verification_email)
from shared.enums import OAuthProvider
from shared.events import EventName
from shared.exceptions import (AppError, ConflictError,
                               InvalidCredentialsError, NotFoundError,
                               PermissionDeniedError, UpstreamServiceError,
                               ValidationAppError)
from shared.permissions import sorted_permissions
from shared.redis import get_cache_client

logger = structlog.get_logger(__name__)

REFRESH_REUSE_GRACE = timedelta(seconds=30)

_OAUTH_PROVIDERS: dict[OAuthProvider, dict[str, str]] = {
    OAuthProvider.GOOGLE: {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "userinfo_url": "https://openidconnect.googleapis.com/v1/userinfo",
        "scope": "openid email profile"
    },
    OAuthProvider.GITHUB: {
        "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "userinfo_url": "https://api.github.com/user",
        "emails_url": "https://api.github.com/user/emails",
        "scope": "read:user user:email"
    }
}


def _oauth_provider_config(provider: str) -> dict[str, str]:
    try:
        return _OAUTH_PROVIDERS[OAuthProvider(provider)]
    except (ValueError, KeyError):
        raise UnsupportedOAuthProviderError(
            f"Unknown OAuth provider '{provider}'.") from None


OAUTH_STATE_TTL_SECONDS = 300
OAUTH_NONCE_COOKIE = "alphamind_oauth_nonce"
TOTP_UNREADABLE_MESSAGE = (
    "Your 2FA settings cannot be read. Ask an administrator to reset your "
    "2FA, then set it up again.")
_USERNAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


class TwoFactorRequiredError(InvalidCredentialsError):
    default_message = (
        "Login succeeded on password but the account requires a TOTP code.")
    code = "two_factor_required"


class EmailNotVerifiedError(InvalidCredentialsError):
    default_message = (
        "Login succeeded on password but the account's email hasn't been "
        "verified yet.")
    code = "email_not_verified"


class RefreshTokenSupersededError(InvalidCredentialsError):
    default_message = (
        "This refresh token was just replaced by a newer one.")
    code = "refresh_token_superseded"


class RecentLoginRequiredError(PermissionDeniedError):
    default_message = (
        "This action needs a recent login. Log in again (password or "
        "OAuth) and retry within the next few minutes.")
    code = "recent_login_required"


class UnsupportedOAuthProviderError(ValidationAppError):
    default_message = (
        "The requested OAuth provider is not one AlphaMind supports.")
    code = "unsupported_oauth_provider"


class OAuthFailedError(InvalidCredentialsError):
    default_message = "The OAuth sign-in was denied or rejected."
    code = "oauth_failed"


class OAuthProviderError(UpstreamServiceError):
    default_message = "The OAuth provider failed or answered unexpectedly."
    code = "oauth_provider_error"


class OAuthAccountLinkError(ConflictError):
    default_message = (
        "Refusing to attach an OAuth login to an existing local account "
        "whose ownership is not proven by both sides.")
    code = "oauth_link_refused"


@dataclass(frozen=True)
class EmailDispatch:
    to: str
    link: str


@dataclass(frozen=True)
class OAuthStart:
    url: str
    nonce: str


@dataclass(frozen=True)
class OAuthFlow:
    provider: str
    nonce_hash: str
    code_verifier: str

    def to_json(self) -> str:
        return json.dumps({
            "provider": self.provider,
            "nonce_hash": self.nonce_hash,
            "code_verifier": self.code_verifier
        })

    @classmethod
    def from_json(cls, raw: str | bytes | None) -> "OAuthFlow | None":
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        provider = body.get("provider")
        nonce_hash = body.get("nonce_hash")
        code_verifier = body.get("code_verifier")
        if not all(isinstance(value, str) and value
                   for value in (provider, nonce_hash, code_verifier)):
            return None
        return cls(
            provider=str(provider), nonce_hash=str(nonce_hash),
            code_verifier=str(code_verifier))


@dataclass(frozen=True)
class OAuthIdentity:
    provider_user_id: str
    email: str | None
    email_verified: bool


def _now_timestamp() -> int:
    return int(datetime.now(UTC).timestamp())


def default_avatar_url(username: str) -> str:
    return (
        "https://api.dicebear.com/7.x/identicon/svg?"
        f"seed={quote(username, safe='')}")


def avatar_or_default(avatar_url: str | None, username: str) -> str:
    return avatar_url or default_avatar_url(username)


def profile_read(profile: UserProfile, username: str) -> UserProfileRead:
    return UserProfileRead(
        first_name=profile.first_name, last_name=profile.last_name,
        avatar_url=avatar_or_default(profile.avatar_url, username),
        bio=profile.bio, timezone=profile.timezone, country=profile.country)


def verification_link(user_id: UUID, email: str) -> str:
    token = security.create_email_verification_token(str(user_id), email)
    return f"{settings.FRONTEND_BASE_URL}/verify-email?token={token}"


def is_user_online(last_seen_at: datetime | None) -> bool:
    if last_seen_at is None:
        return False
    threshold = timedelta(minutes=settings.PRESENCE_ONLINE_THRESHOLD_MINUTES)
    return (datetime.now(UTC) - last_seen_at) < threshold


def presence_event(user_id: UUID, is_online: bool) -> PendingEvent:
    return (
        EventName.USER_PRESENCE_CHANGED, {
            "user_id": str(user_id),
            "is_online": is_online
        })


async def send_verification_email_safely(dispatch: EmailDispatch) -> None:
    await send_quietly(
        "verification_email_failed", send_verification_email,
        settings=settings, to=dispatch.to, verification_link=dispatch.link,
        expire_hours=settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS)


async def send_password_reset_email_safely(dispatch: EmailDispatch) -> None:
    await send_quietly(
        "password_reset_email_failed", send_password_reset_email,
        settings=settings, to=dispatch.to, reset_link=dispatch.link,
        expire_hours=settings.PASSWORD_RESET_TOKEN_EXPIRE_HOURS)


def _oauth_client_id(provider: str) -> str:
    return str(getattr(settings, f"{provider.upper()}_CLIENT_ID"))


def _oauth_redirect_uri(provider: str) -> str:
    return (
        f"{settings.OAUTH_REDIRECT_BASE_URL}{settings.API_V1_PREFIX}"
        f"/auth/oauth/{provider}/callback")


def _oauth_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10.0)


def oauth_cookie_path() -> str:
    return f"{settings.API_V1_PREFIX}/auth/oauth"


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def pkce_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def oauth_authorize_url(provider: str) -> OAuthStart:
    provider_cfg = _oauth_provider_config(provider)

    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(32)
    flow = OAuthFlow(
        provider=provider, nonce_hash=_sha256_hex(nonce),
        code_verifier=secrets.token_urlsafe(48))
    await get_cache_client(settings).set(
        f"oauth:state:{state}", flow.to_json(), ex=OAUTH_STATE_TTL_SECONDS)
    params = httpx.QueryParams({
        "client_id": _oauth_client_id(provider),
        "redirect_uri": _oauth_redirect_uri(provider),
        "response_type": "code",
        "scope": provider_cfg["scope"],
        "state": state,
        "code_challenge": pkce_challenge(flow.code_verifier),
        "code_challenge_method": "S256"
    })
    return OAuthStart(
        url=f"{provider_cfg['authorize_url']}?{params}", nonce=nonce)


SETTINGS_FIELDS = (
    "preferred_currency", "theme", "default_model", "notification_preferences")


async def change_username(
        users: UserRepository, user: User, new_username: str | None,
        changed: list[str]) -> None:
    if new_username is None or new_username == user.username:
        return
    other = await users.get_by_username(new_username)
    if other is not None and other.id != user.id:
        raise ConflictError("This username is already taken.")
    user.username = new_username
    changed.append("username")


def copy_changed_fields(
        target: object, fields: tuple[str, ...], changes: dict[str, Any],
        changed: list[str]) -> None:
    for field in fields:
        if field in changes and getattr(target, field) != changes[field]:
            setattr(target, field, changes[field])
            changed.append(field)


async def flush_unique(db: AsyncSession, message: str) -> None:
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise ConflictError(message) from exc


class AuthService:

    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self.users = UserRepository(session)
        self.pending_events: list[PendingEvent] = []

    def _user_updated(self, user: User, changed: list[str]) -> None:
        if changed:
            self.pending_events.append((
                EventName.USER_UPDATED,
                user_event_payload(
                    user_id=user.id, actor_id=user.id,
                    username=user.username, role=user.system_role,
                    is_active=user.is_active, changed=changed)))

    async def register(self, data: UserRegister) -> EmailDispatch:
        email = normalize_email(data.email)
        if await self.users.get_by_email(email) is not None:
            raise ConflictError("An account with this email already exists.")
        if await self.users.get_by_username(data.username) is not None:
            raise ConflictError("This username is already taken.")

        password_hash = security.hash_password(data.password)
        try:
            async with self._db.begin_nested():
                user = await self.users.create(
                    email=email, username=data.username,
                    password_hash=password_hash)
                await self.users.create_profile(user.id)
                await self.users.create_settings(user.id)
        except IntegrityError as exc:
            raise ConflictError(
                "An account with this email or username already exists."
            ) from exc
        await mirror_user_safely(user)

        link = verification_link(user.id, email)
        logger.info("user_registered", user_id=str(user.id))
        return EmailDispatch(to=email, link=link)

    async def verify_email(self, token: str) -> None:
        payload = security.decode_or_invalid(
            token, security.EMAIL_VERIFICATION_TOKEN_TYPE)

        user = await self.users.require(
            UUID(payload["sub"]), "Account no longer exists.")
        if user.email.lower() != str(payload.get("email", "")).lower():
            raise ValidationAppError(
                "This verification link is no longer valid.")
        user.is_verified = True
        await self._db.flush()

    async def resend_verification(self, email: str) -> EmailDispatch | None:
        user = await self.users.get_by_email(email)
        if user is None or user.is_verified or not user.is_active:
            return None
        return EmailDispatch(
            to=user.email, link=verification_link(user.id, user.email))

    async def request_password_reset(self, email: str) -> EmailDispatch | None:
        user = await self.users.get_by_email(email)
        if user is not None and user.deleted_at is not None:
            user = None
        if user is None:
            logger.info("password_reset_requested_unknown_email")
            return None
        token = security.create_password_reset_token(
            str(user.id), user.password_hash)
        return EmailDispatch(
            to=user.email,
            link=f"{settings.FRONTEND_BASE_URL}/reset-password?token={token}")

    async def confirm_password_reset(
            self, token: str, new_password: str) -> None:
        payload = security.decode_or_invalid(
            token, security.PASSWORD_RESET_TOKEN_TYPE)

        user = await self.users.require(
            UUID(payload["sub"]), "Account no longer exists.")
        if user.deleted_at is not None:
            raise ValidationAppError("This reset link is no longer valid.")
        if payload.get("pwd") != security.password_fingerprint(
                user.password_hash):
            raise ValidationAppError(
                "This reset link has already been used "
                "or is no longer valid.")
        user.password_hash = security.hash_password(new_password)
        await self._db.flush()
        await self.users.revoke_all_sessions(user.id)
        self._user_updated(user, ["password"])

    async def login(
            self, *, email: str, password: str, totp_code: str | None,
            ip_address: str | None = None,
            device_info: str | None = None) -> TokenPair:
        user = await self.users.get_by_email(email)
        if user is None:
            security.dummy_verify_password(password)
            raise InvalidCredentialsError()
        if not security.verify_password(password, user.password_hash):
            raise InvalidCredentialsError()
        if user.deleted_at is not None:
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
            await self._check_totp(
                user.id, two_factor, totp_code,
                InvalidCredentialsError("Invalid 2FA code."))

        return await self._issue_token_pair(
            user=user, ip_address=ip_address, device_info=device_info,
            auth_time=_now_timestamp())

    async def refresh(self, refresh_token: str) -> TokenPair:
        session_row = await self.users.claim_session_for_rotation(
            refresh_token)
        if session_row is None:
            known = await self.users.get_session_by_refresh_token(
                refresh_token)
            if (known is not None and known.revoked_at is not None
                    and known.revoked_reason == SessionRevokeReason.ROTATED):
                await self._rotated_token_reused(known, known.revoked_at)
            raise InvalidCredentialsError(
                "Refresh token is invalid or expired.")

        user = await self.users.get_by_id(session_row.user_id)
        if user is None or not user.is_active or user.deleted_at is not None:
            raise InvalidCredentialsError()

        return await self._issue_token_pair(
            user=user, ip_address=session_row.ip_address,
            device_info=session_row.device_info,
            auth_time=security.refresh_token_auth_time(refresh_token),
            rotated_at=session_row.revoked_at)

    async def _rotated_token_reused(
            self, known: UserSession, rotated_at: datetime) -> None:
        if datetime.now(UTC) - rotated_at < REFRESH_REUSE_GRACE:
            logger.warning(
                "refresh_token_superseded", user_id=str(known.user_id),
                session_id=str(known.id))
            raise RefreshTokenSupersededError()
        revoked = await self.users.revoke_rotation_successors(
            known, reason=SessionRevokeReason.REUSE)
        await self._db.commit()
        logger.warning(
            "refresh_token_reuse_detected", user_id=str(known.user_id),
            revoked=revoked)

    async def logout(self, refresh_token: str) -> None:
        session_row = await self.users.get_session_by_refresh_token(
            refresh_token)
        if session_row is not None and session_row.revoked_at is None:
            user_id = session_row.user_id
            await self.users.revoke_session(session_row)
            used_since = datetime.now(UTC) - timedelta(
                minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
            if await self.users.count_live_sessions(user_id,
                                                    used_since=used_since):
                return
            user = await self.users.get_by_id(user_id)
            if user is not None:
                await self._mark_offline(user)

    async def set_presence(self, user_id: UUID, online: bool) -> None:
        user = await self.users.require(user_id, f"User {user_id} not found.")
        if online:
            await self._mark_online(user)
        else:
            await self._mark_offline(user)

    async def _mark_offline(self, user: User) -> None:
        user.last_seen_at = datetime.now(UTC) - timedelta(
            minutes=settings.PRESENCE_ONLINE_THRESHOLD_MINUTES, seconds=1)
        await self._db.flush()
        self.pending_events.append(presence_event(user.id, False))

    async def _mark_online(self, user: User) -> None:
        was_online = is_user_online(user.last_seen_at)
        user.last_seen_at = datetime.now(UTC)
        await self._db.flush()
        if not was_online:
            self.pending_events.append(presence_event(user.id, True))

    async def _issue_token_pair(
            self, *, user: User, ip_address: str | None,
            device_info: str | None, auth_time: int | None,
            rotated_at: datetime | None = None) -> TokenPair:
        claims: dict[str, Any] = {"role": user.system_role}
        if auth_time is not None:
            claims["auth_time"] = auth_time
        access_token = security.create_access_token(
            str(user.id), extra_claims=claims)
        refresh_token = security.create_refresh_token(str(user.id), auth_time)
        expires_at = datetime.now(UTC) + timedelta(
            days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
        await self.users.create_session(
            user_id=user.id, refresh_token=refresh_token,
            expires_at=expires_at, ip_address=ip_address,
            device_info=device_info, created_at=rotated_at)
        await self._mark_online(user)
        return TokenPair(
            access_token=access_token, refresh_token=refresh_token)

    async def _check_totp(
            self, user_id: UUID, row: TwoFactorSecret, code: str,
            error: AppError) -> None:
        await security.two_factor_guard(str(user_id))
        try:
            secret = security.decrypt_secret(row.secret)
        except security.UnreadableTotpSecretError:
            logger.error("totp_secret_unreadable", user_id=str(user_id))
            raise ConflictError(TOTP_UNREADABLE_MESSAGE) from None
        step = security.match_totp_step(secret, code)
        if step is None or not await self.users.consume_totp_step(row.id,
                                                                  step):
            await security.two_factor_failed(str(user_id))
            raise error
        await security.two_factor_succeeded(str(user_id))
        if not security.is_encrypted(row.secret):
            row.secret = security.encrypt_secret(secret)
            await self._db.flush()

    async def setup_two_factor(self, user_id: UUID) -> TwoFactorSetupResponse:
        existing = await self.users.get_two_factor_secret(user_id)
        if existing is not None and existing.enabled:
            raise ConflictError(
                "2FA is already enabled. Disable it with a valid code "
                "before setting it up again.")
        user = await self.users.require(user_id)
        secret = security.generate_totp_secret()
        row = await self.users.upsert_two_factor_secret(user_id, secret)
        if row.enabled:
            raise ConflictError("2FA is already enabled.")
        uri = security.get_totp_provisioning_uri(secret, user.email)
        return TwoFactorSetupResponse(secret=secret, provisioning_uri=uri)

    async def verify_two_factor(self, user_id: UUID, code: str) -> None:
        row = await self.users.get_two_factor_secret(user_id)
        if row is None:
            raise NotFoundError("2FA has not been set up for this account.")
        await self._check_totp(
            user_id, row, code, ValidationAppError("Invalid 2FA code."))
        if row.enabled:
            return
        row.enabled = True
        await self._db.flush()
        await self._two_factor_changed(user_id)

    async def disable_two_factor(self, user_id: UUID, code: str) -> None:
        row = await self.users.get_two_factor_secret(user_id)
        if row is None or not row.enabled:
            raise NotFoundError("2FA is not currently enabled.")
        await self._check_totp(
            user_id, row, code, ValidationAppError("Invalid 2FA code."))
        row.enabled = False
        await self._db.flush()
        await self._two_factor_changed(user_id)

    async def _two_factor_changed(self, user_id: UUID) -> None:
        user = await self.users.get_by_id(user_id)
        if user is not None:
            self._user_updated(user, ["two_factor_enabled"])

    async def oauth_callback(
            self, provider: str, code: str | None, state: str | None,
            error: str | None = None, *, nonce: str | None = None,
            ip_address: str | None = None, device_info: str | None = None
    ) -> TokenPair | OAuthTwoFactorChallenge:
        provider_cfg = _oauth_provider_config(provider)

        if error:
            raise OAuthFailedError(
                f"OAuth sign-in was not completed ({error[:64]}).")
        if not code or not state:
            raise OAuthFailedError("Missing OAuth code or state.")

        flow = OAuthFlow.from_json(
            await get_cache_client(settings).getdel(f"oauth:state:{state}"))
        if flow is None or flow.provider != provider:
            raise OAuthFailedError("Invalid or expired OAuth state.")
        if not nonce or not hmac.compare_digest(flow.nonce_hash,
                                                _sha256_hex(nonce)):
            logger.warning("oauth_browser_binding_failed", provider=provider)
            raise OAuthFailedError(
                "This sign-in was not started in this browser. Start the "
                "sign-in again.")

        identity = await self._fetch_oauth_identity(
            provider, provider_cfg, code, flow.code_verifier)
        user = await self._resolve_oauth_user(provider, identity)

        if not user.is_active or user.deleted_at is not None:
            raise InvalidCredentialsError("This account has been deactivated.")
        two_factor = await self.users.get_two_factor_secret(user.id)
        if two_factor is not None and two_factor.enabled:
            return OAuthTwoFactorChallenge(
                challenge_token=security.create_oauth_2fa_token(
                    str(user.id), provider),
                expires_in=settings.OAUTH_2FA_CHALLENGE_EXPIRE_MINUTES * 60)

        return await self._issue_token_pair(
            user=user, ip_address=ip_address, device_info=device_info
            or f"oauth:{provider}", auth_time=_now_timestamp())

    async def oauth_complete_two_factor(
            self, challenge_token: str, code: str, *,
            ip_address: str | None = None,
            device_info: str | None = None) -> TokenPair:
        try:
            payload = security.decode_special_token(
                challenge_token, security.OAUTH_2FA_TOKEN_TYPE)
        except security.SpecialTokenError as exc:
            raise OAuthFailedError(
                "Invalid or expired 2FA challenge.") from exc

        user = await self.users.get_by_id(UUID(payload["sub"]))
        if user is None or not user.is_active or user.deleted_at is not None:
            raise InvalidCredentialsError()
        two_factor = await self.users.get_two_factor_secret(user.id)
        if two_factor is None or not two_factor.enabled:
            raise OAuthFailedError("This 2FA challenge is no longer valid.")
        await self._check_totp(
            user.id, two_factor, code,
            InvalidCredentialsError("Invalid 2FA code."))

        ttl = max(1, int(payload["exp"]) - int(datetime.now(UTC).timestamp()))
        first_use = await get_cache_client(settings).set(
            f"oauth:2fa:used:{payload['jti']}", "1", ex=ttl, nx=True)
        if not first_use:
            raise OAuthFailedError("This 2FA challenge was already used.")

        return await self._issue_token_pair(
            user=user, ip_address=ip_address, device_info=device_info
            or f"oauth:{payload.get('provider', '')}",
            auth_time=_now_timestamp())

    async def _fetch_oauth_identity(
            self, provider: str, provider_cfg: dict[str, str], code: str,
            code_verifier: str) -> OAuthIdentity:
        client_secret = getattr(settings, f"{provider.upper()}_CLIENT_SECRET")
        try:
            async with _oauth_http_client() as client:
                token_response = await client.post(
                    provider_cfg["token_url"], data={
                        "client_id": _oauth_client_id(provider),
                        "client_secret": client_secret,
                        "code": code,
                        "redirect_uri": _oauth_redirect_uri(provider),
                        "grant_type": "authorization_code",
                        "code_verifier": code_verifier
                    }, headers={"Accept": "application/json"})
                if token_response.status_code >= 500:
                    raise OAuthProviderError(
                        "The OAuth provider is unavailable.")
                token_body = _json_object(token_response)
                if token_response.status_code >= 400 or "error" in token_body:
                    logger.info(
                        "oauth_token_rejected", provider=provider,
                        status=token_response.status_code,
                        error=str(token_body.get("error", ""))[:64])
                    raise OAuthFailedError(
                        "The OAuth provider rejected the sign-in.")
                provider_access_token = token_body.get("access_token")
                if not (isinstance(provider_access_token, str)
                        and provider_access_token):
                    raise OAuthFailedError(
                        "The OAuth provider rejected the sign-in.")

                auth_headers = {
                    "Authorization": f"Bearer {provider_access_token}",
                    "Accept": "application/json"
                }
                userinfo_response = await client.get(
                    provider_cfg["userinfo_url"], headers=auth_headers)
                if userinfo_response.status_code >= 400:
                    raise OAuthProviderError(
                        "Could not read the OAuth profile.")
                userinfo = _json_object(userinfo_response)

                github_emails: list[Any] | None = None
                if provider == OAuthProvider.GITHUB:
                    emails_response = await client.get(
                        provider_cfg["emails_url"], headers=auth_headers)
                    if emails_response.status_code < 400:
                        try:
                            parsed = emails_response.json()
                        except ValueError:
                            parsed = None
                        github_emails = (
                            parsed if isinstance(parsed, list) else None)
                    else:
                        logger.warning(
                            "oauth_github_emails_unavailable",
                            status=emails_response.status_code)
        except httpx.HTTPError as exc:
            logger.warning(
                "oauth_provider_http_error", provider=provider, error=str(exc))
            raise OAuthProviderError(
                "The OAuth provider is unavailable.") from exc

        return _extract_oauth_identity(provider, userinfo, github_emails)

    async def _resolve_oauth_user(
            self, provider: str, identity: OAuthIdentity) -> User:
        existing_link = await self.users.get_oauth_account(
            provider, identity.provider_user_id)
        if existing_link is not None:
            return await self.users.require(
                existing_link.user_id, "Linked account no longer exists.")

        email = normalize_email(identity.email) if identity.email else None
        local = await self.users.get_by_email(email) if email else None
        if local is not None:
            if not (identity.email_verified and local.is_verified):
                raise OAuthAccountLinkError(
                    "An account with this email already exists. Log in with "
                    "your password first; the OAuth login was not linked.")
            user = local
        else:
            user = None
        created = user is None

        try:
            async with self._db.begin_nested():
                if user is None:
                    account_email = (
                        email if email and identity.email_verified else
                        f"{provider}_{identity.provider_user_id}"
                        "@users.alphamind.local")
                    username = await self._generate_unique_username(
                        email, identity.provider_user_id)
                    user = await self.users.create(
                        email=account_email, username=username,
                        password_hash=security.hash_password(
                            secrets.token_urlsafe(32)))
                    user.is_verified = bool(email and identity.email_verified)
                    await self.users.create_profile(user.id)
                    await self.users.create_settings(user.id)
                await self.users.create_oauth_account(
                    user_id=user.id, provider=provider,
                    provider_user_id=identity.provider_user_id)
        except IntegrityError as exc:
            raise ConflictError(
                "This sign-in is already being processed. Please retry."
            ) from exc
        if created:
            await mirror_user_safely(user)
        return user

    async def _generate_unique_username(
            self, email: str | None, fallback_seed: str) -> str:
        base = _USERNAME_UNSAFE.sub("_",
                                    email.split("@")[0] if email else "")[:24]
        if len(base) < 3:
            base = _USERNAME_UNSAFE.sub("_", f"user_{fallback_seed}")[:24]
        candidate = base
        suffix = 0
        while await self.users.get_by_username(candidate) is not None:
            suffix += 1
            candidate = f"{base}{suffix}"
        return candidate

    async def get_me(self, user_id: UUID) -> MeRead:
        user = await self.users.require(user_id)
        profile = await self.users.get_profile(user_id)
        settings_row = await self.users.get_settings(user_id)
        return MeRead(
            id=user.id, email=user.email, username=user.username,
            system_role=user.system_role,
            permissions=sorted_permissions(user.system_role),
            is_active=user.is_active, is_verified=user.is_verified,
            last_seen_at=user.last_seen_at, created_at=user.created_at,
            profile=profile_read(profile, user.username) if profile else None,
            settings=(
                UserSettingsRead.model_validate(settings_row)
                if settings_row else None))

    async def update_me(self, user_id: UUID, data: MeUpdate) -> MeRead:
        user = await self.users.require(user_id)

        changes = data.model_dump(exclude_unset=True)
        changed: list[str] = []
        await change_username(
            self.users, user, changes.get("username"), changed)
        profile = await self.users.get_profile(user_id)
        if profile is not None:
            copy_changed_fields(profile, PROFILE_FIELDS, changes, changed)
        settings_row = await self.users.get_settings(user_id)
        if settings_row is not None:
            copy_changed_fields(
                settings_row, SETTINGS_FIELDS, changes, changed)
        await flush_unique(self._db, "This username is already taken.")
        self._user_updated(user, changed)
        return await self.get_me(user_id)

    async def set_avatar_url(
            self, user_id: UUID, avatar_url: str) -> str | None:
        profile = await self.users.get_profile(user_id)
        if profile is None:
            raise NotFoundError("Profile not found.")
        previous = profile.avatar_url
        profile.avatar_url = avatar_url
        await self._db.flush()
        user = await self.users.get_by_id(user_id)
        if user is not None:
            self._user_updated(user, ["avatar_url"])
        return previous


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise OAuthProviderError(
            "Unexpected response from the OAuth provider.") from exc
    if not isinstance(body, dict):
        raise OAuthProviderError(
            "Unexpected response from the OAuth provider.")
    return body


def _extract_oauth_identity(
        provider: str, userinfo: dict[str, Any],
        github_emails: list[Any] | None = None) -> OAuthIdentity:
    if provider == OAuthProvider.GOOGLE:
        sub = userinfo.get("sub")
        if not sub:
            raise OAuthProviderError(
                "Unexpected response from the OAuth provider.")
        verified = userinfo.get("email_verified")
        return OAuthIdentity(
            provider_user_id=str(sub), email=userinfo.get("email") or None,
            email_verified=verified is True or str(verified).lower() == "true")
    if provider == OAuthProvider.GITHUB:
        github_id = userinfo.get("id")
        if github_id is None:
            raise OAuthProviderError(
                "Unexpected response from the OAuth provider.")
        for entry in github_emails or []:
            if (isinstance(entry, dict) and entry.get("primary") is True
                    and entry.get("verified") is True and entry.get("email")):
                return OAuthIdentity(str(github_id), str(entry["email"]), True)
        return OAuthIdentity(
            str(github_id),
            userinfo.get("email") or None, False)
    raise UnsupportedOAuthProviderError(
        f"Unknown OAuth provider '{provider}'.")


AVATAR_URL_PREFIX = "/static/avatars/"

_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp"
}


def sniff_image_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def extension_for(content_type: str) -> str:
    return _EXTENSIONS.get(content_type, ".bin")


def delete_stored_avatar(avatar_url: str | None, storage_dir: Path) -> None:
    if not avatar_url or not avatar_url.startswith(AVATAR_URL_PREFIX):
        return
    name = Path(avatar_url).name
    if not name or name in {".", ".."}:
        return
    try:
        (storage_dir / name).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("avatar_delete_failed", file=name, error=str(exc))
