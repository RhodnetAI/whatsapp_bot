import datetime
import json
import logging
import pathlib
import random
import string

from app.core.config import settings

logger = logging.getLogger("whatsapp")


def _random_meet_code() -> str:
    def segment(n: int) -> str:
        return "".join(random.choices(string.ascii_lowercase, k=n))
    return f"{segment(3)}-{segment(4)}-{segment(3)}"


def _load_service_account() -> dict | None:
    file_path = settings.google_service_account_file.strip()
    if not file_path:
        return None
    try:
        p = pathlib.Path(file_path)
        if not p.is_absolute():
            p = pathlib.Path(__file__).parent.parent.parent / p
        sa_info = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("Service account file not found: %s", file_path)
        return None
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to read/parse service account file: %s", file_path)
        return None
    if sa_info.get("type") != "service_account":
        logger.warning("File %s does not describe a service_account; falling back", file_path)
        return None
    return sa_info


def create_meeting_event(
    meeting_datetime: datetime.datetime,
    duration_minutes: int = 30,
    title: str = "Business Consultation",
    attendees: list[str] | None = None,
    description: str = "",
) -> tuple[str, str, str]:
    """Create a Google Calendar event with a Meet link.

    Returns (meet_link, calendar_event_id, calendar_event_link).
    Falls back to a randomly generated Meet URL if the Calendar API fails;
    in that case calendar_event_id and calendar_event_link are empty strings.
    """
    sa_info = _load_service_account()
    if sa_info is not None:
        result = _try_google_calendar_event(
            sa_info, meeting_datetime, duration_minutes, title, attendees or [], description
        )
        if result is not None:
            return result

    code = _random_meet_code()
    fallback = f"https://meet.google.com/{code}"
    logger.info("Using fallback Meet link: %s", fallback)
    return fallback, "", ""


def _try_google_calendar_event(
    sa_info: dict,
    meeting_datetime: datetime.datetime,
    duration_minutes: int,
    title: str,
    attendees: list[str],
    description: str,
) -> tuple[str, str, str] | None:
    """Create a Calendar event with Meet + attendees.

    Returns (hangoutLink, event_id, htmlLink) on success, None on any failure.
    """
    try:
        from google.oauth2 import service_account  # type: ignore
        from googleapiclient.discovery import build  # type: ignore

        scopes = ["https://www.googleapis.com/auth/calendar"]
        creds = service_account.Credentials.from_service_account_info(sa_info, scopes=scopes)
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)

        end_dt = meeting_datetime + datetime.timedelta(minutes=duration_minutes)
        tz = settings.google_calendar_timezone or "UTC"
        event_body: dict = {
            "summary": title,
            "description": description,
            "start": {"dateTime": meeting_datetime.isoformat(), "timeZone": tz},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": tz},
            "conferenceData": {
                "createRequest": {
                    "requestId": f"mtg-{meeting_datetime.strftime('%Y%m%d%H%M%S')}",
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            },
        }
        if attendees:
            event_body["attendees"] = [{"email": a} for a in attendees if a]

        logger.info(
            "Creating Calendar event title=%r attendees=%s", title, attendees
        )
        calendar_id = settings.google_calendar_id or "primary"
        created = (
            service.events()
            .insert(
                calendarId=calendar_id,
                body=event_body,
                conferenceDataVersion=1,
                sendUpdates="all" if attendees else "none",
            )
            .execute()
        )
        meet_link = created.get("hangoutLink", "")
        event_id = created.get("id", "")
        html_link = created.get("htmlLink", "")

        if meet_link:
            logger.info(
                "Calendar event created id=%s meet=%s calendar=%s",
                event_id,
                meet_link,
                html_link,
            )
            return meet_link, event_id, html_link

        logger.warning("Calendar event created but hangoutLink missing; id=%s", event_id)
    except Exception:
        logger.exception("Google Calendar API event creation failed; falling back")

    return None


def generate_meet_link(
    meeting_datetime: datetime.datetime,
    duration_minutes: int = 30,
    title: str = "Business Consultation",
) -> str:
    """Backward-compatible wrapper — returns only the Meet URL."""
    meet_link, _, _ = create_meeting_event(meeting_datetime, duration_minutes, title)
    return meet_link
