from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4
import security
import structlog
from config import settings
from database import get_db
from deps import get_current_user, require_recent_login
from events_out import schedule_events
from fastapi import (APIRouter, BackgroundTasks, Depends, Query, Request,
                     UploadFile, status)
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from domains.auth.schema import (AvatarUploadRead, LogoutRequest, MeRead,
                                 MeUpdate, OAuthTwoFactorChallenge,
                                 OAuthTwoFactorVerify, PasswordResetConfirm,
                                 PasswordResetRequest, RefreshRequest,
                                 ResendVerificationRequest, TokenPair,
                                 TwoFactorDisableRequest,
                                 TwoFactorSetupResponse,
                                 TwoFactorVerifyRequest, UserLogin,
                                 UserRegister, VerifyEmailRequest)
from domains.auth.service import (OAUTH_NONCE_COOKIE, OAUTH_STATE_TTL_SECONDS,
                                  AuthService, delete_stored_avatar,
                                  extension_for, oauth_authorize_url,
                                  oauth_cookie_path,
                                  send_password_reset_email_safely,
                                  send_verification_email_safely,
                                  sniff_image_type)
from shared.exceptions import AppError, ValidationAppError
from shared.schemas import APIResponse
from shared.security import CurrentUser

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["auth"])


def _client_context(request: Request) -> dict[str, str | None]:
    return {
        "ip_address": security.client_ip(request),
        "device_info": request.headers.get("user-agent")
    }


@router.post(
    "/register", status_code=status.HTTP_201_CREATED,
    response_model=APIResponse[dict[str, str]])
async def register(
    data: UserRegister, request: Request, background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[dict[str, str]]:
    await security.hit(
        "register:ip", security.client_ip(request),
        limit=settings.RATE_LIMIT_REGISTER_PER_IP,
        window=settings.RATE_LIMIT_REGISTER_WINDOW_SECONDS)
    dispatch = await AuthService(db).register(data)
    background_tasks.add_task(send_verification_email_safely, dispatch)
    return APIResponse(
        data={
            "message": "Registered. Check your email to verify your account."
        })


@router.post("/verify-email", response_model=APIResponse[dict[str, str]])
async def verify_email(
    data: VerifyEmailRequest,
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[dict[str, str]]:
    await AuthService(db).verify_email(data.token)
    return APIResponse(data={"message": "Email verified."})


@router.post(
    "/verify-email/resend", response_model=APIResponse[dict[str, str]],
    description=(
        "Send a new email verification link. The response is the same 200 "
        "whether"
        " or not the email belongs to an account."))
async def resend_verification_email(
    data: ResendVerificationRequest, background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[dict[str, str]]:
    await security.hit(
        "resend:email", data.email,
        limit=settings.RATE_LIMIT_RESEND_VERIFICATION_PER_EMAIL,
        window=settings.RATE_LIMIT_RESEND_VERIFICATION_WINDOW_SECONDS)
    dispatch = await AuthService(db).resend_verification(data.email)
    if dispatch is not None:
        background_tasks.add_task(send_verification_email_safely, dispatch)
    return APIResponse(
        data={
            "message": "If an unverified account exists for this email, a new "
            "verification link has been sent."
        })


@router.post(
    "/password-reset/request", response_model=APIResponse[dict[str, str]])
async def request_password_reset(
    data: PasswordResetRequest, request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[dict[str, str]]:
    await security.hit(
        "reset:ip", security.client_ip(request),
        limit=settings.RATE_LIMIT_PASSWORD_RESET_PER_IP,
        window=settings.RATE_LIMIT_PASSWORD_RESET_WINDOW_SECONDS)
    await security.hit(
        "reset:email", data.email,
        limit=settings.RATE_LIMIT_PASSWORD_RESET_PER_EMAIL,
        window=settings.RATE_LIMIT_PASSWORD_RESET_WINDOW_SECONDS)
    dispatch = await AuthService(db).request_password_reset(data.email)
    if dispatch is not None:
        background_tasks.add_task(send_password_reset_email_safely, dispatch)
    return APIResponse(
        data={
            "message": "If an account exists for this email, a reset link has "
            "been sent."
        })


@router.post(
    "/password-reset/confirm", response_model=APIResponse[dict[str, str]])
async def confirm_password_reset(
    data: PasswordResetConfirm, background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[dict[str, str]]:
    service = AuthService(db)
    await service.confirm_password_reset(data.token, data.new_password)
    schedule_events(background_tasks, service.pending_events)
    return APIResponse(data={"message": "Password has been reset."})


@router.post("/login", response_model=APIResponse[TokenPair])
async def login(
    data: UserLogin, request: Request, background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[TokenPair]:
    await security.hit(
        "login:ip", security.client_ip(request),
        limit=settings.RATE_LIMIT_LOGIN_PER_IP,
        window=settings.RATE_LIMIT_LOGIN_WINDOW_SECONDS)
    await security.hit(
        "login:email", data.email, limit=settings.RATE_LIMIT_LOGIN_PER_EMAIL,
        window=settings.RATE_LIMIT_LOGIN_WINDOW_SECONDS)
    service = AuthService(db)
    tokens = await service.login(
        email=data.email, password=data.password, totp_code=data.totp_code,
        **_client_context(request))
    schedule_events(background_tasks, service.pending_events)
    return APIResponse(data=tokens)


@router.post("/refresh", response_model=APIResponse[TokenPair])
async def refresh(
    data: RefreshRequest, background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[TokenPair]:
    service = AuthService(db)
    tokens = await service.refresh(data.refresh_token)
    schedule_events(background_tasks, service.pending_events)
    return APIResponse(data=tokens)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    data: LogoutRequest, background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db, scope="function")
) -> None:
    service = AuthService(db)
    await service.logout(data.refresh_token)
    schedule_events(background_tasks, service.pending_events)


@router.get(
    "/oauth/{provider}/authorize", description=(
        "Start an OAuth sign-in: redirects to the provider and sets a "
        "short-lived HttpOnly cookie that binds the sign-in to this browser. "
        "Open it as a top-level navigation, not with fetch. Optional: next, "
        "a same-site path to open after the sign-in, and marker, a random "
        "value (16-128 of A-Z a-z 0-9 _ -) the caller keeps; both are "
        "returned in the completion fragment. Invalid values are dropped."))
async def oauth_authorize(
    provider: str, next_path: str | None = Query(None, alias="next"),
    marker: str | None = None
) -> RedirectResponse:
    start = await oauth_authorize_url(
        provider, next_path=next_path, marker=marker)
    response = RedirectResponse(
        start.url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    response.set_cookie(
        OAUTH_NONCE_COOKIE, start.nonce, max_age=OAUTH_STATE_TTL_SECONDS,
        path=oauth_cookie_path(), secure=True, httponly=True, samesite="lax")
    return response


def _oauth_complete_url(fields: dict[str, str | int]) -> str:
    return f"{settings.FRONTEND_BASE_URL}/oauth/complete#{urlencode(fields)}"


@router.get(
    "/oauth/{provider}/callback", status_code=status.HTTP_303_SEE_OTHER,
    response_model=None, description=(
        "Complete an OAuth sign-in. Requires the cookie set by the authorize "
        "step in the same browser. Redirects (303) to "
        "FRONTEND_BASE_URL/oauth/complete with the result in the URL "
        "fragment: access_token and refresh_token; or challenge_token and "
        "expires_in when the account has 2FA enabled (then call POST "
        "/api/v1/auth/oauth/2fa); or error=oauth_failed and message. The "
        "next and marker given at the authorize step are added once the "
        "browser binding has been checked."))
async def oauth_callback(
    provider: str, request: Request, code: str | None = None,
    state: str | None = None, error: str | None = None, *,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db, scope="function")
) -> RedirectResponse:
    service = AuthService(db)
    fields: dict[str, str | int]
    try:
        result = await service.oauth_callback(
            provider, code, state, error,
            nonce=request.cookies.get(OAUTH_NONCE_COOKIE),
            **_client_context(request))
    except AppError as exc:
        await db.rollback()
        logger.warning(
            "oauth_callback_failed", provider=provider, code=exc.code,
            message=exc.message)
        fields = {"error": "oauth_failed", "message": exc.message}
    else:
        if isinstance(result, OAuthTwoFactorChallenge):
            fields = {
                "challenge_token": result.challenge_token,
                "expires_in": result.expires_in
            }
        else:
            fields = {
                "access_token": result.access_token,
                "refresh_token": result.refresh_token
            }
        schedule_events(background_tasks, service.pending_events)
    flow = service.oauth_flow
    if flow is not None and flow.next_path is not None:
        fields["next"] = flow.next_path
    if flow is not None and flow.marker is not None:
        fields["marker"] = flow.marker
    response = RedirectResponse(
        _oauth_complete_url(fields), status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(
        OAUTH_NONCE_COOKIE, path=oauth_cookie_path(), secure=True,
        httponly=True, samesite="lax")
    return response


@router.post(
    "/oauth/2fa", response_model=APIResponse[TokenPair], description=(
        "Exchange an OAuth two-factor challenge token and a TOTP code for a "
        "token"
        " pair."))
async def oauth_two_factor(
    data: OAuthTwoFactorVerify, request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[TokenPair]:
    service = AuthService(db)
    tokens = await service.oauth_complete_two_factor(
        data.challenge_token, data.code, **_client_context(request))
    schedule_events(background_tasks, service.pending_events)
    return APIResponse(data=tokens)


@router.post("/2fa/setup", response_model=APIResponse[TwoFactorSetupResponse])
async def setup_two_factor(
        current_user: CurrentUser = Depends(require_recent_login),
        db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[TwoFactorSetupResponse]:
    result = await AuthService(db).setup_two_factor(current_user.user_id)
    return APIResponse(data=result)


@router.post("/2fa/verify", status_code=status.HTTP_204_NO_CONTENT)
async def verify_two_factor(
    data: TwoFactorVerifyRequest, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(require_recent_login),
    db: AsyncSession = Depends(get_db, scope="function")
) -> None:
    service = AuthService(db)
    await service.verify_two_factor(current_user.user_id, data.code)
    schedule_events(background_tasks, service.pending_events)


@router.post("/2fa/disable", status_code=status.HTTP_204_NO_CONTENT)
async def disable_two_factor(
    data: TwoFactorDisableRequest, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> None:
    service = AuthService(db)
    await service.disable_two_factor(current_user.user_id, data.code)
    schedule_events(background_tasks, service.pending_events)


@router.get("/me", response_model=APIResponse[MeRead])
async def get_me(
        current_user: CurrentUser = Depends(get_current_user),
        db: AsyncSession = Depends(get_db,
                                   scope="function")) -> APIResponse[MeRead]:
    result = await AuthService(db).get_me(current_user.user_id)
    return APIResponse(data=result)


@router.patch("/me", response_model=APIResponse[MeRead])
async def update_me(
    data: MeUpdate, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[MeRead]:
    service = AuthService(db)
    result = await service.update_me(current_user.user_id, data)
    schedule_events(background_tasks, service.pending_events)
    return APIResponse(data=result)


@router.post("/me/avatar", response_model=APIResponse[AvatarUploadRead])
async def upload_avatar(
    file: UploadFile, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[AvatarUploadRead]:
    if file.content_type not in settings.AVATAR_ALLOWED_CONTENT_TYPES:
        raise ValidationAppError(
            f"Unsupported image type '{file.content_type}'. Allowed: "
            f"{', '.join(settings.AVATAR_ALLOWED_CONTENT_TYPES)}.")

    max_bytes = settings.AVATAR_MAX_SIZE_MB * 1024 * 1024
    contents = await file.read(max_bytes + 1)
    if len(contents) > max_bytes:
        raise ValidationAppError(
            f"Image exceeds the {settings.AVATAR_MAX_SIZE_MB}MB size limit.")

    detected = sniff_image_type(contents)
    if detected is None or detected != file.content_type:
        raise ValidationAppError(
            "The file content is not a valid image of the declared type.")

    storage_dir = Path(settings.AVATAR_STORAGE_DIR)
    filename = f"{current_user.user_id}-{uuid4().hex}{extension_for(detected)}"
    (storage_dir / filename).write_bytes(contents)

    avatar_url = f"/static/avatars/{filename}"
    service = AuthService(db)
    try:
        previous = await service.set_avatar_url(
            current_user.user_id, avatar_url)
        await db.commit()
    except Exception:
        delete_stored_avatar(avatar_url, storage_dir)
        raise
    schedule_events(background_tasks, service.pending_events)
    if previous and previous != avatar_url:
        background_tasks.add_task(delete_stored_avatar, previous, storage_dir)
    return APIResponse(data=AvatarUploadRead(avatar_url=avatar_url))
