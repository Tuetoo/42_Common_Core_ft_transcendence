import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
import pyotp
from config import settings
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from jose import JWTError, jwt
from passlib.context import CryptContext
from shared.security import is_uuid_claim

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


def password_fingerprint(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode()).hexdigest()[:32]


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


def create_password_reset_token(user_id: str, password_hash: str) -> str:
    return _create_token(
        subject=user_id, expires_delta=timedelta(
            hours=settings.PASSWORD_RESET_TOKEN_EXPIRE_HOURS),
        token_type=PASSWORD_RESET_TOKEN_TYPE,
        extra_claims={"pwd": password_fingerprint(password_hash)})


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


def is_encrypted(stored: str) -> bool:
    return stored.startswith(PREFIX)


def encrypt_secret(secret: str) -> str:
    return PREFIX + _fernet().encrypt(secret.encode()).decode()


def decrypt_secret(stored: str) -> str:
    if not is_encrypted(stored):
        return stored
    try:
        return _fernet().decrypt(stored[len(PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise UnreadableTotpSecretError from exc
