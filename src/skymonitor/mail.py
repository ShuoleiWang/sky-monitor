"""E-mail through SMTP, as the Chinese mail providers expect it (SSL on 465 or STARTTLS on 587)."""

from __future__ import annotations

import smtplib
import ssl
from collections.abc import Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid


class EmailError(Exception):
    pass


@dataclass(frozen=True)
class EmailSettings:
    enabled: bool = False
    host: str = ""
    port: int = 465
    # "ssl", "starttls" or "none".
    security: str = "ssl"
    username: str = ""
    password: str = ""
    sender: str = ""
    recipients: tuple[str, ...] = ()
    subject_prefix: str = "[Sky Monitor]"
    # Mail when the roof may open; mail again when that is no longer so; repeat the first every so often.
    on_clear: bool = True
    on_clear_lost: bool = True
    repeat_minutes: float = 30.0
    timeout_seconds: float = 30.0


@dataclass(frozen=True)
class Attachment:
    filename: str
    data: bytes
    # "image/jpeg" and the like.
    media_type: str = "application/octet-stream"


def compose(settings: EmailSettings, subject: str, text: str, attachments: Sequence[Attachment] = ()) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = f"{settings.subject_prefix} {subject}".strip()
    message["From"] = settings.sender or settings.username
    message["To"] = ", ".join(settings.recipients)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    message.set_content(text)
    for attachment in attachments:
        main_type, _, sub_type = attachment.media_type.partition("/")
        message.add_attachment(
            attachment.data,
            maintype=main_type or "application",
            subtype=sub_type or "octet-stream",
            filename=attachment.filename,
        )
    return message


def send_email(settings: EmailSettings, subject: str, text: str, attachments: Sequence[Attachment] = ()) -> None:
    """Deliver one mail, or raise EmailError with a reason that carries no password."""

    if not settings.host or not settings.recipients:
        raise EmailError("the mail server or the recipients are not set")
    message = compose(settings, subject, text, attachments)
    context = ssl.create_default_context()
    try:
        if settings.security == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                settings.host, settings.port, timeout=settings.timeout_seconds, context=context
            )
        else:
            client = smtplib.SMTP(settings.host, settings.port, timeout=settings.timeout_seconds)
        with client:
            client.ehlo()
            if settings.security == "starttls":
                client.starttls(context=context)
                client.ehlo()
            if settings.username:
                client.login(settings.username, settings.password)
            client.send_message(message)
    except smtplib.SMTPAuthenticationError as error:
        raise EmailError("the mail server refused the user name or password") from error
    except (smtplib.SMTPException, OSError) as error:
        raise EmailError(f"{type(error).__name__}: {error}") from error
