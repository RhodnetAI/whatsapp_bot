import datetime
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings

logger = logging.getLogger("whatsapp")


def _build_email_body(
    user_name: str,
    user_email: str,
    meeting_datetime: datetime.datetime,
    duration_minutes: int,
    meet_link: str,
    summary: str,
    recipient_is_admin: bool,
) -> str:
    formatted_dt = meeting_datetime.strftime("%A, %B %d, %Y at %I:%M %p")
    intro = (
        f"A meeting has been booked by {user_name}."
        if recipient_is_admin
        else f"Hi {user_name}, your meeting has been confirmed!"
    )
    return f"""{intro}

Meeting Details
───────────────────────────────
Name:      {user_name}
Email:     {user_email}
Date/Time: {formatted_dt}
Duration:  {duration_minutes} minutes
Meet Link: {meet_link}

Context
───────────────────────────────
{summary or "No summary available."}

───────────────────────────────
This is an automated confirmation from your WhatsApp bot.
"""


def send_meeting_confirmation(
    user_email: str,
    user_name: str,
    meeting_datetime: datetime.datetime,
    duration_minutes: int,
    meet_link: str,
    summary: str,
) -> None:
    """Send confirmation email to both the user and the admin via Gmail SMTP."""
    if not settings.gmail_user or not settings.gmail_app_password:
        logger.warning("Gmail credentials not configured; skipping email")
        return

    subject = f"Meeting Confirmed – {meeting_datetime.strftime('%b %d, %Y %I:%M %p')}"
    recipients = {user_email: False}
    if settings.admin_email and settings.admin_email != user_email:
        recipients[settings.admin_email] = True

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(settings.gmail_user, settings.gmail_app_password)
            for address, is_admin in recipients.items():
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"] = settings.gmail_user
                msg["To"] = address
                body = _build_email_body(
                    user_name=user_name,
                    user_email=user_email,
                    meeting_datetime=meeting_datetime,
                    duration_minutes=duration_minutes,
                    meet_link=meet_link,
                    summary=summary,
                    recipient_is_admin=is_admin,
                )
                msg.attach(MIMEText(body, "plain"))
                smtp.sendmail(settings.gmail_user, address, msg.as_string())
                logger.info("Confirmation email sent to %s", address)
    except Exception:
        logger.exception("Failed to send meeting confirmation email")
