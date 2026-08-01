from abc import ABC, abstractmethod
import re
from email.message import EmailMessage
import aiosmtplib
import structlog
from shared.config import BaseServiceSettings

logger = structlog.get_logger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(html: str) -> str:
    return _TAG_RE.sub("", html).strip()


class EmailSendError(Exception):
    """Raised when an email could not be handed off to the SMTP server."""


class EmailSender(ABC):
    @abstractmethod
    async def send(self, *, to: str, subject: str, html_body: str,
                   text_body: str | None = None) -> None:
        """Send a single email.
        Raises:
            EmailSendError: if the message could not be sent."""


class SMTPEmailSender(EmailSender):
    def __init__(self, settings: BaseServiceSettings) -> None:
        self._settings = settings

    async def send(self, *, to: str, subject: str, html_body: str,
                   text_body: str | None = None) -> None:
        message = EmailMessage()
        message["From"] = self._settings.SMTP_FROM_EMAIL
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text_body or _strip_html(html_body))
        message.add_alternative(html_body, subtype="html")
        try:
            await aiosmtplib.send(
                message, hostname=self._settings.SMTP_HOST,
                port=self._settings.SMTP_PORT,
                username=self._settings.SMTP_USER or None,
                password=self._settings.SMTP_PASSWORD or None,
                start_tls=self._settings.SMTP_USE_TLS)
        except Exception as exc:
            logger.error("email_send_failed", to=to, subject=subject,
                         error=str(exc))
            raise EmailSendError(f"failed to send email to {to}") from exc
        logger.info("email_sent", to=to, subject=subject)


_sender: EmailSender | None = None


def get_email_sender(settings: BaseServiceSettings) -> EmailSender:
    global _sender
    if _sender is None:
        _sender = SMTPEmailSender(settings)
    return _sender


async def send_verification_email(*, settings: BaseServiceSettings, to: str,
                                  verification_link: str, expire_hours: int
                                  ) -> None:
    html = f"""
    <p>Welcome to AlphaMind!</p>
    <p>Please confirm your email address by clicking the link below:</p>
    <p><a href="{verification_link}">Verify my email</a></p>
    <p>This link expires in {expire_hours} hour(s). If you didn't create
    an AlphaMind account, you can safely ignore this email.</p>
    """.strip()
    sender = get_email_sender(settings)
    await sender.send(to=to, subject="Verify your AlphaMind account",
                      html_body=html)


async def send_password_reset_email(*, settings: BaseServiceSettings, to: str,
                                    reset_link: str, expire_hours: int
                                    ) -> None:
    html = f"""
    <p>We received a request to reset your AlphaMind password.</p>
    <p><a href="{reset_link}">Reset my password</a></p>
    <p>This link expires in {expire_hours} hour(s). If you didn't request this,
    you can safely ignore this email -- your password will not be changed.</p>
    """.strip()
    sender = get_email_sender(settings)
    await sender.send(to=to, subject="Reset your AlphaMind password",
                      html_body=html)


async def send_organization_invitation_email(*, settings: BaseServiceSettings,
                                             to: str, organization_name: str,
                                             invitation_link: str) -> None:
    html = f"""
    <p>You've been invited to join <strong>{organization_name}</strong>
    on AlphaMind.</p>
    <p><a href="{invitation_link}">Accept invitation</a></p>
    """.strip()
    sender = get_email_sender(settings)
    await sender.send(
        to=to,
        subject=f"You're invited to join {organization_name} on AlphaMind",
        html_body=html)
