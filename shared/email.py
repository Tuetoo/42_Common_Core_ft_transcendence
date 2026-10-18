import html
import re
from collections.abc import Awaitable, Callable
from email.message import EmailMessage
from typing import ParamSpec
import aiosmtplib
import structlog
from shared.config import BaseServiceSettings

logger = structlog.get_logger(__name__)

P = ParamSpec("P")

_TAG_RE = re.compile(r"<[^>]+>")


class EmailSendError(Exception):
    pass


async def send_email(
        settings: BaseServiceSettings, *, to: str, subject: str,
        html_body: str, text_body: str | None = None) -> None:
    message = EmailMessage()
    message["From"] = settings.SMTP_FROM_EMAIL
    message["To"] = to
    message["Subject"] = _clean_header(subject)
    message.set_content(text_body or _strip_html(html_body))
    message.add_alternative(html_body, subtype="html")

    try:
        await aiosmtplib.send(
            message, hostname=settings.SMTP_HOST, port=settings.SMTP_PORT,
            username=settings.SMTP_USER or None,
            password=settings.SMTP_PASSWORD or None,
            start_tls=settings.SMTP_USE_TLS,
            timeout=settings.SMTP_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.error(
            "email_send_failed", to=to, subject=subject, error=str(exc))
        raise EmailSendError(f"failed to send email to {to}") from exc

    logger.info("email_sent", to=to, subject=subject)


async def send_quietly(
        failure_event: str, send: Callable[P, Awaitable[None]], *args: P.args,
        **kwargs: P.kwargs) -> None:
    try:
        await send(*args, **kwargs)
    except Exception as exc:
        logger.warning(failure_event, error=str(exc))


_LINK_RE = re.compile(
    r'<a\s[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_BLOCK_END_RE = re.compile(r"</p\s*>|<br\s*/?>", re.IGNORECASE)


def _strip_html(body: str) -> str:
    text = _LINK_RE.sub(lambda m: f"{m.group(2)}: {m.group(1)}", body)
    text = _BLOCK_END_RE.sub("\n", text)
    text = html.unescape(_TAG_RE.sub("", text))
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line).strip()


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _clean_header(value: str) -> str:
    return re.sub(r"[\r\n]+", " ", value).strip()


async def send_verification_email(
        *, settings: BaseServiceSettings, to: str, verification_link: str,
        expire_hours: int) -> None:
    body = f"""
    <p>Welcome to AlphaMind!</p>
    <p>Please confirm your email address by clicking the link below:</p>
    <p><a href="{_esc(verification_link)}">Verify my email</a></p>
    <p>This link expires in {_esc(expire_hours)} hour(s). If you didn't create
    an AlphaMind account, you can safely ignore this email.</p>
    """
    await send_email(
        settings, to=to, subject="Verify your AlphaMind account",
        html_body=body)


async def send_password_reset_email(
        *, settings: BaseServiceSettings, to: str, reset_link: str,
        expire_hours: int) -> None:
    body = f"""
    <p>We received a request to reset your AlphaMind password.</p>
    <p><a href="{_esc(reset_link)}">Reset my password</a></p>
    <p>This link expires in {_esc(expire_hours)} hour(s). If you didn't request
    this, you can safely ignore this email -- your password will not be
    changed.</p>
    """
    await send_email(
        settings, to=to, subject="Reset your AlphaMind password",
        html_body=body)


async def send_organization_invitation_email(
        *, settings: BaseServiceSettings, to: str, organization_name: str,
        invitation_link: str) -> None:
    body = f"""
    <p>You've been invited to join <strong>{_esc(organization_name)}</strong>
    on AlphaMind.</p>
    <p><a href="{_esc(invitation_link)}">Accept invitation</a></p>
    """
    await send_email(
        settings, to=to, subject=_clean_header(
            f"You're invited to join {organization_name} on AlphaMind"),
        html_body=body)


async def send_notification_email(
        *, settings: BaseServiceSettings, to: str, title: str,
        content: str | None) -> None:
    body = f"<p>{_esc(title)}</p>" + (
        f"<p>{_esc(content)}</p>" if content else "")
    await send_email(
        settings, to=to, subject=_clean_header(f"AlphaMind: {title}"),
        html_body=body)
