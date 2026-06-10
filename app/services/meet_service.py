import datetime
import json
import logging
import random
import string

from app.core.config import settings

logger = logging.getLogger("whatsapp")


def _random_meet_code() -> str:
    def segment(n: int) -> str:
        return "".join(random.choices(string.ascii_lowercase, k=n))
    return f"{segment(3)}-{segment(4)}-{segment(3)}"


def _load_service_account() -> dict | None:
    raw = settings.google_service_account_json.strip()
    if not raw:
        return None
    try:
        sa_info = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Failed to parse GOOGLE_SERVICE_ACCOUNT_JSON")
        return None
    if sa_info.get("type") != "service_account":
        logger.warning("GOOGLE_SERVICE_ACCOUNT_JSON does not describe a service_account")
        return None
    return sa_info


def create_meeting_event(
    meeting_datetime: datetime.datetime,
    duration_minutes: int = 30,
    title: str = "Business Consultation",
    attendees: list[str] | None = None,
    description: str = "",
) -> tuple[str, str, str]:
    """Create a Google Calendar event and return a Meet link.

    A Meet link is always generated locally (service accounts cannot create
    real Google Meet conferences without domain-wide delegation), and is
    embedded in the calendar event's description and location.

    Returns (meet_link, calendar_event_id, calendar_event_link).
    If the Calendar API call fails, calendar_event_id and calendar_event_link
    are empty strings but the Meet link is still returned.
    """
    code = _random_meet_code()
    meet_link = f"https://meet.google.com/{code}"

    sa_info = _load_service_account()
    if sa_info is not None:
        result = _try_google_calendar_event(
            sa_info, meeting_datetime, duration_minutes, title, attendees or [], description, meet_link
        )
        if result is not None:
            event_id, html_link = result
            logger.info(
                "Calendar event created successfully id=%s meet=%s calendar=%s",
                event_id, meet_link, html_link,
            )
            return meet_link, event_id, html_link

    logger.warning("Calendar event not created; using Meet link without calendar entry: %s", meet_link)
    return meet_link, "", ""


def _try_google_calendar_event(
    sa_info: dict,
    meeting_datetime: datetime.datetime,
    duration_minutes: int,
    title: str,
    attendees: list[str],
    description: str,
    meet_link: str,
) -> tuple[str, str] | None:
    """Create a Calendar event with attendees and the given Meet link.

    Returns (event_id, htmlLink) on success, None on any failure.
    """
    try:
        from google.oauth2 import service_account  # type: ignore
        from googleapiclient.discovery import build  # type: ignore

        scopes = ["https://www.googleapis.com/auth/calendar"]
        creds = service_account.Credentials.from_service_account_info(sa_info, scopes=scopes)
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)

        end_dt = meeting_datetime + datetime.timedelta(minutes=duration_minutes)
        tz = settings.google_calendar_timezone or "UTC"
        full_description = f"{description}\n\nGoogle Meet: {meet_link}" if description else f"Google Meet: {meet_link}"
        event_body: dict = {
            "summary": title,
            "description": full_description,
            "location": meet_link,
            "start": {"dateTime": meeting_datetime.isoformat(), "timeZone": tz},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": tz},
        }
        if attendees:
            event_body["attendees"] = [{"email": a} for a in attendees if a]

        logger.info(
            "Creating Calendar event title=%r attendees=%s calendar_id=%s",
            title, attendees, settings.google_calendar_id or "primary",
        )
        calendar_id = settings.google_calendar_id or "primary"

        from googleapiclient.errors import HttpError  # type: ignore

        try:
            created = (
                service.events()
                .insert(
                    calendarId=calendar_id,
                    body=event_body,
                    sendUpdates="all" if attendees else "none",
                )
                .execute()
            )
        except HttpError as exc:
            # Bare service accounts (without domain-wide delegation) cannot invite
            # attendees. Retry without attendees so the event is still created.
            if attendees and exc.resp.status == 403 and "forbiddenForServiceAccounts" in str(exc):
                logger.warning(
                    "Calendar API rejected attendees (no domain-wide delegation); "
                    "retrying without attendees. Confirmation emails will still be sent."
                )
                event_body.pop("attendees", None)
                created = (
                    service.events()
                    .insert(calendarId=calendar_id, body=event_body, sendUpdates="none")
                    .execute()
                )
            else:
                raise

        event_id = created.get("id", "")
        html_link = created.get("htmlLink", "")
        return event_id, html_link
    except Exception:
        logger.exception("Google Calendar API event creation failed; continuing without calendar entry")

    return None


def generate_meet_link(
    meeting_datetime: datetime.datetime,
    duration_minutes: int = 30,
    title: str = "Business Consultation",
) -> str:
    """Backward-compatible wrapper — returns only the Meet URL."""
    meet_link, _, _ = create_meeting_event(meeting_datetime, duration_minutes, title)
    return meet_link
