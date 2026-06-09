import datetime
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings

logger = logging.getLogger("whatsapp")


def build_booking_body(
    user_name: str,
    user_email: str,
    meeting_datetime: datetime.datetime,
    duration_minutes: int,
    meet_link: str,
    summary: str,
    calendar_event_link: str = "",
    recipient_is_admin: bool = False,
) -> str:
    """Build the plain-text booking confirmation body.

    Used for both email and the WhatsApp follow-up message.
    """
    formatted_dt = meeting_datetime.strftime("%A, %B %d, %Y at %I:%M %p UTC")
    intro = (
        f"A meeting has been booked by {user_name}."
        if recipient_is_admin
        else f"Hi {user_name}, your meeting has been confirmed!"
    )
    lines = [
        intro,
        "",
        "Meeting Details",
        "───────────────────────────────",
        f"Name:      {user_name}",
        f"Email:     {user_email}",
        f"Date/Time: {formatted_dt}",
        f"Duration:  {duration_minutes} minutes",
        f"Meet Link: {meet_link}",
    ]
    if calendar_event_link:
        lines.append(f"Calendar:  {calendar_event_link}")
    lines += [
        "",
        "Context",
        "───────────────────────────────",
        summary or "No summary available.",
        "",
        "───────────────────────────────",
        "This is an automated confirmation from your WhatsApp bot.",
    ]
    return "\n".join(lines)


def send_meeting_confirmation(
    user_email: str,
    user_name: str,
    meeting_datetime: datetime.datetime,
    duration_minutes: int,
    meet_link: str,
    summary: str,
    calendar_event_link: str = "",
) -> None:
    """Send confirmation email to the user and the admin via Gmail SMTP."""
    if not settings.gmail_user or not settings.gmail_app_password:
        logger.warning("Gmail credentials not configured; skipping email")
        return

    logger.info(
        "Preparing confirmation email for user=%s meet=%s", user_email, meet_link
    )

    subject = f"Meeting Confirmed – {meeting_datetime.strftime('%b %d, %Y %I:%M %p UTC')}"
    recipients: dict[str, bool] = {user_email: False}
    if settings.admin_email and settings.admin_email != user_email:
        recipients[settings.admin_email] = True

    try:
        logger.info("Connecting to Gmail SMTP (smtp.gmail.com:465) as %s", settings.gmail_user)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(settings.gmail_user, settings.gmail_app_password)
            logger.info("SMTP login successful")
            for address, is_admin in recipients.items():
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"] = settings.gmail_user
                msg["To"] = address
                body = build_booking_body(
                    user_name=user_name,
                    user_email=user_email,
                    meeting_datetime=meeting_datetime,
                    duration_minutes=duration_minutes,
                    meet_link=meet_link,
                    summary=summary,
                    calendar_event_link=calendar_event_link,
                    recipient_is_admin=is_admin,
                )
                msg.attach(MIMEText(body, "plain"))
                smtp.sendmail(settings.gmail_user, address, msg.as_string())
                logger.info("Confirmation email sent to %s", address)
    except smtplib.SMTPAuthenticationError:
        logger.exception(
            "SMTP authentication failed for %s — check GMAIL_APP_PASSWORD", settings.gmail_user
        )
    except Exception:
        logger.exception("Failed to send meeting confirmation email")
