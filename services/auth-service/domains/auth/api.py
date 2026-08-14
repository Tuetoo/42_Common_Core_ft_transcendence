import mimetypes
from pathlib import Path
from uuid import uuid4
from fastapi import APIRouter, Depends, Request, UploadFile, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from config import settings
from database import get_db
from deps import get_current_user
from domains.auth.schema import (AvatarUploadRead, LogoutRequest, MeRead,
                                 MeUpdate, PasswordResetConfirm,
                                 PasswordResetRequest, RefreshRequest,
                                 TokenPair, TwoFactorDisableRequest,
                                 TwoFactorSetupResponse,
                                 TwoFactorVerifyRequest, UserLogin,
                                 UserRegister, VerifyEmailRequest)
from domains.auth.service import AuthService
from shared.exceptions import ValidationAppError
from shared.schemas.base import APIResponse
from shared.security import CurrentUser

router = APIRouter(tags=["auth"])


def _client_context(request: Request) -> dict[str, str | None]:
    return {
        "ip_address": request.client.host if request.client else None,
        "device_info": request.headers.get("user-agent"),
    }


@router.post("/register", status_code=status.HTTP_201_CREATED,
             response_model=APIResponse[dict[str, str]])
async def register(data: UserRegister, db: AsyncSession = Depends(get_db)
                   ) -> APIResponse[dict[str, str]]:
    await AuthService(db).register(data)
    return APIResponse(data={
            "message": "Registered. Check your email to verify your account."})


@router.post("/verify-email", response_model=APIResponse[dict[str, str]])
async def verify_email(
    data: VerifyEmailRequest, db: AsyncSession = Depends(get_db)
) -> APIResponse[dict[str, str]]:
    await AuthService(db).verify_email(data.token)
    return APIResponse(data={"message": "Email verified."})


@router.post("/password-reset/request",
             response_model=APIResponse[dict[str, str]])
async def request_password_reset(
    data: PasswordResetRequest, db: AsyncSession = Depends(get_db)
) -> APIResponse[dict[str, str]]:
    await AuthService(db).request_password_reset(data.email)
    return APIResponse(data={"message": "If an account exists for this email,"
                             " a reset link has been sent."})


@router.post("/password-reset/confirm",
             response_model=APIResponse[dict[str, str]])
async def confirm_password_reset(
    data: PasswordResetConfirm, db: AsyncSession = Depends(get_db)
) -> APIResponse[dict[str, str]]:
    await AuthService(db).confirm_password_reset(data.token, data.new_password)
    return APIResponse(data={"message": "Password has been reset."})


@router.post("/login", response_model=APIResponse[TokenPair])
async def login(data: UserLogin, request: Request,
                db: AsyncSession = Depends(get_db)) -> APIResponse[TokenPair]:
    tokens = await AuthService(db).login(
        email=data.email, password=data.password, totp_code=data.totp_code,
        **_client_context(request))
    return APIResponse(data=tokens)


@router.post("/refresh", response_model=APIResponse[TokenPair])
async def refresh(data: RefreshRequest, db: AsyncSession = Depends(get_db)
                  ) -> APIResponse[TokenPair]:
    tokens = await AuthService(db).refresh(data.refresh_token)
    return APIResponse(data=tokens)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(data: LogoutRequest, db: AsyncSession = Depends(get_db)
                 ) -> None:
    await AuthService(db).logout(data.refresh_token)


@router.get("/oauth/{provider}/authorize")
async def oauth_authorize(provider: str, db: AsyncSession = Depends(get_db)
                          ) -> RedirectResponse:
    url = await AuthService(db).oauth_authorize_url(provider)
    return RedirectResponse(
        url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.get("/oauth/{provider}/callback",
            response_model=APIResponse[TokenPair])
async def oauth_callback(
    provider: str, code: str, state: str, db: AsyncSession = Depends(get_db)
) -> APIResponse[TokenPair]:
    tokens = await AuthService(db).oauth_callback(provider, code, state)
    return APIResponse(data=tokens)


@router.post("/2fa/setup", response_model=APIResponse[TwoFactorSetupResponse])
async def setup_two_factor(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[TwoFactorSetupResponse]:
    service = AuthService(db)
    me = await service.get_me(current_user.user_id)
    result = await service.setup_two_factor(current_user.user_id, me.email)
    return APIResponse(data=result)


@router.post("/2fa/verify", status_code=status.HTTP_204_NO_CONTENT)
async def verify_two_factor(
    data: TwoFactorVerifyRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> None:
    await AuthService(db).verify_two_factor(current_user.user_id, data.code)


@router.post("/2fa/disable", status_code=status.HTTP_204_NO_CONTENT)
async def disable_two_factor(
    data: TwoFactorDisableRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> None:
    await AuthService(db).disable_two_factor(current_user.user_id, data.code)


@router.get("/me", response_model=APIResponse[MeRead])
async def get_me(current_user: CurrentUser = Depends(get_current_user),
                 db: AsyncSession = Depends(get_db)) -> APIResponse[MeRead]:
    result = await AuthService(db).get_me(current_user.user_id)
    return APIResponse(data=result)


@router.patch("/me", response_model=APIResponse[MeRead])
async def update_me(data: MeUpdate,
                    current_user: CurrentUser = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db)) -> APIResponse[MeRead]:
    result = await AuthService(db).update_me(current_user.user_id, data)
    return APIResponse(data=result)


@router.post("/me/avatar", response_model=APIResponse[AvatarUploadRead])
async def upload_avatar(file: UploadFile,
                        current_user: CurrentUser = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)
                        ) -> APIResponse[AvatarUploadRead]:
    if file.content_type not in settings.AVATAR_ALLOWED_CONTENT_TYPES:
        raise ValidationAppError(
            f"Unsupported image type '{file.content_type}'. "
            f"Allowed: {', '.join(settings.AVATAR_ALLOWED_CONTENT_TYPES)}")
    max_bytes = settings.AVATAR_MAX_SIZE_MB * 1024 * 1024
    contents = await file.read()
    if len(contents) > max_bytes:
        raise ValidationAppError(
            f"Image exceeds the {settings.AVATAR_MAX_SIZE_MB}MB size limit.")
    extension = mimetypes.guess_extension(file.content_type) or ".bin"
    storage_dir = Path(settings.AVATAR_STORAGE_DIR)
    storage_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{current_user.user_id}-{uuid4().hex}{extension}"
    (storage_dir / filename).write_bytes(contents)
    avatar_url = f"/static/avatars/{filename}"
    await AuthService(db).set_avatar_url(current_user.user_id, avatar_url)
    return APIResponse(data=AvatarUploadRead(avatar_url=avatar_url))
