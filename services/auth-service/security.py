import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
import pyotp
import structlog
from config import settings
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from fastapi import Request
from fastapi.responses import JSONResponse
from jose import JWTError, jwt
from passlib.context import CryptContext
from shared.exceptions import RateLimitExceededError, ValidationAppError
from shared.redis import (fixed_window_hits, get_rate_limit_client,
                          rate_limited_response)
from shared.security import is_uuid_claim

logger = structlog.get_logger(__name__)

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

EMAIL_VERIFICATION_TOKEN_TYPE = "email_verification"
PASSWORD_RESET_TOKEN_TYPE = "password_reset"
ORGANIZATION_INVITATION_TOKEN_TYPE = "organization_invitation"
OAUTH_2FA_TOKEN_TYPE = "oauth_2fa"

PASSWORD_MAX_BYTES = 72


class SpecialTokenError(Exception):
    pass


def hash_password(plain_password: str) -> str:
    hashed: str = _pwd_context.hash(plain_password)
    return hashed


def verify_password(plain_password: str, hashed_password: str) -> bool:
    is_valid: bool = _pwd_context.verify(plain_password, hashed_password)
    return is_valid


_dummy_hash: str | None = None


def dummy_verify_password(plain_password: str) -> None:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(16))
    _pwd_context.verify(plain_password, _dummy_hash)


def password_fingerprint(password_hash: str, email: str) -> str:
    material = f"{password_hash}\n{email.lower()}"
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def hash_refresh_token(refresh_token: str) -> str:
    return hashlib.sha256(refresh_token.encode()).hexdigest()


def _create_token(
        subject: str, expires_delta: timedelta, token_type: str,
        extra_claims: dict[str, Any] | None = None) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
        "jti": str(uuid4())
    }
    if extra_claims:
        payload.update(extra_claims)
    token: str = jwt.encode(
        payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token


def create_access_token(
        subject: str, extra_claims: dict[str, Any] | None = None) -> str:
    return _create_token(
        subject, timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        "access", extra_claims)


def create_refresh_token(subject: str, auth_time: int | None) -> str:
    return _create_token(
        subject, timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS), "refresh",
        {"auth_time": auth_time} if auth_time is not None else None)


def refresh_token_auth_time(refresh_token: str) -> int | None:
    try:
        payload = jwt.decode(
            refresh_token, settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        return None
    value = payload.get("auth_time")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def is_recent_login(auth_time: int | None) -> bool:
    if auth_time is None:
        return False
    age = datetime.now(UTC).timestamp() - auth_time
    return age <= settings.TWO_FACTOR_RECENT_LOGIN_MINUTES * 60


def create_email_verification_token(user_id: str, email: str) -> str:
    return _create_token(
        subject=user_id, expires_delta=timedelta(
            hours=settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS),
        token_type=EMAIL_VERIFICATION_TOKEN_TYPE,
        extra_claims={"email": email})


def create_password_reset_token(
        user_id: str, password_hash: str, email: str) -> str:
    return _create_token(
        subject=user_id, expires_delta=timedelta(
            hours=settings.PASSWORD_RESET_TOKEN_EXPIRE_HOURS),
        token_type=PASSWORD_RESET_TOKEN_TYPE,
        extra_claims={"pwd": password_fingerprint(password_hash, email)})


def create_oauth_2fa_token(user_id: str, provider: str) -> str:
    return _create_token(
        subject=user_id, expires_delta=timedelta(
            minutes=settings.OAUTH_2FA_CHALLENGE_EXPIRE_MINUTES),
        token_type=OAUTH_2FA_TOKEN_TYPE, extra_claims={"provider": provider})


def create_organization_invitation_token(
        invitation_id: str, expire_days: int) -> str:
    return _create_token(
        subject=invitation_id, expires_delta=timedelta(days=expire_days),
        token_type=ORGANIZATION_INVITATION_TOKEN_TYPE)


def decode_special_token(token: str, expected_type: str) -> dict[str, Any]:
    try:
        payload: dict[str, Any] = jwt.decode(
            token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as exc:
        raise SpecialTokenError("invalid or expired token") from exc

    if payload.get("type") != expected_type:
        raise SpecialTokenError(
            f"expected token type '{expected_type}', "
            f"got '{payload.get('type')}'")
    if not is_uuid_claim(payload.get("sub")):
        raise SpecialTokenError("invalid token subject")
    return payload


def decode_or_invalid(token: str, expected_type: str) -> dict[str, Any]:
    try:
        return decode_special_token(token, expected_type)
    except SpecialTokenError as exc:
        raise ValidationAppError(str(exc)) from exc


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def get_totp_provisioning_uri(secret: str, account_email: str) -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(
        name=account_email, issuer_name=settings.TOTP_ISSUER)


def match_totp_step(secret: str, code: str) -> int | None:
    code = (code or "").strip()
    if not code.isdigit():
        return None
    totp = pyotp.totp.TOTP(secret)
    current = int(datetime.now(UTC).timestamp()) // totp.interval
    for step in (current - 1, current, current + 1):
        if hmac.compare_digest(totp.generate_otp(step), code):
            return step
    return None


PREFIX = "fernet:"


class UnreadableTotpSecretError(Exception):
    pass


def _derived_key() -> bytes:
    digest = hashlib.sha256(
        f"alphamind-totp:{settings.JWT_SECRET}".encode()).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet() -> MultiFernet:
    keys = [Fernet(_derived_key())]
    if settings.TOTP_ENCRYPTION_KEY:
        keys.insert(0, Fernet(settings.TOTP_ENCRYPTION_KEY.encode()))
    return MultiFernet(keys)


def encrypt_secret(secret: str) -> str:
    return PREFIX + _fernet().encrypt(secret.encode()).decode()


def decrypt_secret(stored: str) -> str:
    if not stored.startswith(PREFIX):
        raise UnreadableTotpSecretError
    try:
        return _fernet().decrypt(stored[len(PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise UnreadableTotpSecretError from exc


_RATE_LIMIT_PREFIX = "auth:rl"


class RateLimitedError(RateLimitExceededError):
    default_message = "Too many attempts. Please try again later."
    code = "rate_limited"

    def __init__(
            self, message: str | None = None, *, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


def _rate_key(scope: str, identity: str) -> str:
    digest = hashlib.sha256(identity.encode()).hexdigest()[:32]
    return f"{_RATE_LIMIT_PREFIX}:{scope}:{digest}"


async def hit(
        scope: str, identity: str | None, *, limit: int, window: int) -> None:
    if not settings.RATE_LIMIT_ENABLED or not identity:
        return
    hits = await fixed_window_hits(
        settings, [_rate_key(scope, identity)], window)
    if hits is None:
        return
    count, retry_after = hits[0]
    if count > limit:
        raise RateLimitedError(retry_after=retry_after)


async def two_factor_guard(user_id: str) -> None:
    if not settings.RATE_LIMIT_ENABLED:
        return
    try:
        ttl = await get_rate_limit_client(settings).ttl(
            _rate_key("2fa:lock", user_id))
    except Exception as exc:
        logger.warning("rate_limit_unavailable", scope="2fa", error=str(exc))
        return
    if int(ttl) > 0:
        raise RateLimitedError(
            "Too many invalid codes. Please try again later.",
            retry_after=int(ttl))


async def two_factor_failed(user_id: str) -> None:
    if not settings.RATE_LIMIT_ENABLED:
        return
    key = _rate_key("2fa:fail", user_id)
    hits = await fixed_window_hits(
        settings, [key], settings.RATE_LIMIT_2FA_WINDOW_SECONDS)
    if hits is None or hits[0][0] < settings.RATE_LIMIT_2FA_FAILURES:
        return
    try:
        client = get_rate_limit_client(settings)
        await client.set(
            _rate_key("2fa:lock", user_id), 1,
            ex=settings.RATE_LIMIT_2FA_LOCK_SECONDS)
        await client.delete(key)
    except Exception as exc:
        logger.warning("rate_limit_unavailable", scope="2fa", error=str(exc))


async def two_factor_succeeded(user_id: str) -> None:
    if not settings.RATE_LIMIT_ENABLED:
        return
    try:
        await get_rate_limit_client(settings).delete(
            _rate_key("2fa:fail", user_id))
    except Exception as exc:
        logger.warning("rate_limit_unavailable", scope="2fa", error=str(exc))


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def rate_limited_handler(
        request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RateLimitedError)
    return rate_limited_response(
        request, code=exc.code, message=exc.message,
        retry_after=exc.retry_after)
