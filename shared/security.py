from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from shared.config import BaseServiceSettings

bearer_scheme = HTTPBearer(auto_error=False)


class CurrentUser:
    def __init__(self, user_id: str, role: str, org_id: str | None = None):
        self.user_id = user_id
        self.role = role
        self.org_id = org_id


def decode_token(token: str, settings: BaseServiceSettings) -> dict:
    try:
        return jwt.decode(token, settings.JWT_SECRET,
                          algorithms=[settings.JWT_ALGORITHM])
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid or expired token") from exc


def make_get_current_user(settings: BaseServiceSettings):
    async def get_current_user(
            credentials: HTTPAuthorizationCredentials | None = Depends(
                bearer_scheme)) -> CurrentUser:
        if credentials is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Missing bearer token")
        payload = decode_token(credentials.credentials, settings)
        if payload.get("type") != "access":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Wrong token type")
        return CurrentUser(user_id=payload["sub"],
                           role=payload.get("role", "user"),
                           org_id=payload.get("org_id"))
    return get_current_user


def require_roles(*allowed_roles: str):
    def _factory(get_current_user_dep):
        async def _checker(user: CurrentUser = Depends(get_current_user_dep)
                           ) -> CurrentUser:
            if user.role not in allowed_roles:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                    detail="Insufficient permissions")
            return user
        return _checker
    return _factory
