import base64
import datetime
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

from app.core.config import settings

logger = logging.getLogger("whatsapp")

GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


def build_booking_body(
    user_name: str,
    user_email: str,
    meeting_datetime: datetime.datetime,
    duration_minutes: int,
    meet_link: str,
    purpose: str,
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
        "Purpose",
        "───────────────────────────────",
        purpose or "Not specified.",
        "",
        "───────────────────────────────",
        "This is an automated confirmation from your WhatsApp bot.",
    ]
    return "\n".join(lines)


def _get_access_token() -> str | None:
    """Exchange the stored OAuth refresh token for a short-lived Gmail API access token."""
    if not (settings.gmail_client_id and settings.gmail_client_secret and settings.gmail_refresh_token):
        logger.warning("Gmail API OAuth credentials not configured; skipping email")
        return None
    try:
        resp = requests.post(
            GMAIL_TOKEN_URL,
            data={
                "client_id": settings.gmail_client_id,
                "client_secret": settings.gmail_client_secret,
                "refresh_token": settings.gmail_refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if not token:
            logger.error("Gmail token refresh response missing access_token")
            return None
        return token
    except Exception:
        logger.exception("Failed to refresh Gmail API access token")
        return None


def _send_via_gmail_api(access_token: str, to_address: str, subject: str, body: str) -> bool:
    """Send a single email via the Gmail REST API. Returns True on success."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.gmail_user
    msg["To"] = to_address
    msg.attach(MIMEText(body, "plain"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    try:
        resp = requests.post(
            GMAIL_SEND_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            json={"raw": raw},
            timeout=20,
        )
        if resp.status_code >= 400:
            logger.error(
                "Gmail API send failed for %s: %s %s", to_address, resp.status_code, resp.text
            )
            return False
        logger.info("Confirmation email sent to %s via Gmail API", to_address)
        return True
    except Exception:
        logger.exception("Gmail API request failed for recipient=%s", to_address)
        return False


def send_meeting_confirmation(
    user_email: str,
    user_name: str,
    meeting_datetime: datetime.datetime,
    duration_minutes: int,
    meet_link: str,
    purpose: str,
    calendar_event_link: str = "",
) -> None:
    """Send a confirmation email to the user and the admin via the Gmail API."""
    if not settings.gmail_user:
        logger.warning("GMAIL_USER not configured; skipping confirmation email")
        return

    access_token = _get_access_token()
    if access_token is None:
        logger.error("Could not obtain Gmail API access token; confirmation email not sent")
        return

    subject = f"Meeting Confirmed – {meeting_datetime.strftime('%b %d, %Y %I:%M %p UTC')}"
    recipients: dict[str, bool] = {user_email: False}
    if settings.admin_email and settings.admin_email != user_email:
        recipients[settings.admin_email] = True

    for address, is_admin in recipients.items():
        body = build_booking_body(
            user_name=user_name,
            user_email=user_email,
            meeting_datetime=meeting_datetime,
            duration_minutes=duration_minutes,
            meet_link=meet_link,
            purpose=purpose,
            calendar_event_link=calendar_event_link,
            recipient_is_admin=is_admin,
        )
        if _send_via_gmail_api(access_token, address, subject, body):
            logger.info("Confirmation email sent successfully to %s", address)
        else:
            logger.error("Confirmation email failed to send to %s", address)
