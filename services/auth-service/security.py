import uuid
from datetime import datetime, timedelta, UTC
import pyotp
from typing import Any
from jose import jwt, JWTError
from passlib.context import CryptContext
from config import settings

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

EMAIL_VERIFICATION_TOKEN_TYPE = "email_verification"
PASSWORD_RESET_TOKEN_TYPE = "password_reset"
ORGANIZATION_INVITATION_TOKEN_TYPE = "organization_invitation"


class SpecialTokenError(Exception):
    """Raised by decode_special_token on invalid/expired/mismatched token."""


def hash_password(plain_password: str) -> str:
    hashed: str = _pwd_context.hash(plain_password)
    return hashed


def verify_password(plain_password: str, hashed_password: str) -> bool:
    is_valid: bool = _pwd_context.verify(plain_password, hashed_password)
    return is_valid


def _create_token(subject: str, expires_delta: timedelta, token_type: str,
                  extra_claims: dict[str, Any] | None = None) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
        "jti": str(uuid.uuid4()),
    }
    if extra_claims:
        payload.update(extra_claims)
    token: str = jwt.encode(payload, settings.JWT_SECRET,
                            algorithm=settings.JWT_ALGORITHM)
    return token


def create_access_token(subject: str,
                        extra_claims: dict[str, Any] | None = None) -> str:
    return _create_token(subject, timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES), "access", extra_claims)


def create_refresh_token(subject: str) -> str:
    return _create_token(subject, timedelta(
        days=settings.REFRESH_TOKEN_EXPIRE_DAYS), "refresh")


def create_email_verification_token(user_id: str, email: str) -> str:
    return _create_token(
        subject=user_id,
        expires_delta=timedelta(
            hours=settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS),
        token_type=EMAIL_VERIFICATION_TOKEN_TYPE,
        extra_claims={"email": email})


def create_password_reset_token(user_id: str) -> str:
    return _create_token(
        subject=user_id,
        expires_delta=timedelta(
            hours=settings.PASSWORD_RESET_TOKEN_EXPIRE_HOURS),
        token_type=PASSWORD_RESET_TOKEN_TYPE)


def create_organization_invitation_token(invitation_id: str, expire_days: int
                                         ) -> str:
    return _create_token(subject=invitation_id, expires_delta=timedelta(
        days=expire_days), token_type=ORGANIZATION_INVITATION_TOKEN_TYPE)


def decode_special_token(token: str, expected_type: str) -> dict[str, Any]:
    try:
        payload: dict[str, Any] = jwt.decode(
            token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as exc:
        raise SpecialTokenError("invalid or expired token") from exc
    if payload.get("type") != expected_type:
        raise SpecialTokenError(f"expected token type '{expected_type}', "
                                f"got '{payload.get('type')}'")
    return payload


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def get_totp_provisioning_uri(secret: str, account_email: str) -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(
        name=account_email, issuer_name=settings.TOTP_ISSUER)


def verify_totp_code(secret: str, code: str) -> bool:
    return pyotp.totp.TOTP(secret).verify(code, valid_window=1)
