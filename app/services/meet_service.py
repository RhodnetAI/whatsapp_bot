import datetime
import json
import logging
import pathlib
import random
import string

from app.core.config import settings

logger = logging.getLogger("whatsapp")


def _random_meet_code() -> str:
    """Generate a random Google Meet-style code (xxx-xxxx-xxx)."""
    def segment(n: int) -> str:
        return "".join(random.choices(string.ascii_lowercase, k=n))
    return f"{segment(3)}-{segment(4)}-{segment(3)}"


def _load_service_account() -> dict | None:
    """Load the service account JSON from the file path specified in settings.
    Returns the parsed dict on success, None if missing or invalid."""
    file_path = settings.google_service_account_file.strip()
    if not file_path:
        return None
    try:
        p = pathlib.Path(file_path)
        if not p.is_absolute():
            # Resolve relative to the backend directory (where .env lives)
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


def _try_google_calendar_meet(
    meeting_datetime: datetime.datetime,
    duration_minutes: int,
    title: str,
) -> str | None:
    """Attempt to create a Google Calendar event with a Meet link using a
    service account.  Returns the hangoutLink on success, None on any failure."""
    sa_info = _load_service_account()
    if sa_info is None:
        return None

    try:
        from google.oauth2 import service_account  # type: ignore
        from googleapiclient.discovery import build  # type: ignore

        scopes = ["https://www.googleapis.com/auth/calendar"]
        creds = service_account.Credentials.from_service_account_info(sa_info, scopes=scopes)

        service = build("calendar", "v3", credentials=creds, cache_discovery=False)

        end_dt = meeting_datetime + datetime.timedelta(minutes=duration_minutes)
        event = {
            "summary": title,
            "start": {"dateTime": meeting_datetime.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": "UTC"},
            "conferenceData": {
                "createRequest": {
                    "requestId": f"mtg-{meeting_datetime.strftime('%Y%m%d%H%M%S')}",
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            },
        }
        created = (
            service.events()
            .insert(calendarId="primary", body=event, conferenceDataVersion=1)
            .execute()
        )
        link = created.get("hangoutLink")
        if link:
            logger.info("Google Meet link created via Calendar API: %s", link)
            return link
    except Exception:
        logger.exception("Google Calendar API Meet link creation failed; falling back")

    return None


def generate_meet_link(
    meeting_datetime: datetime.datetime,
    duration_minutes: int = 30,
    title: str = "Business Consultation",
) -> str:
    """Return a Google Meet URL for the given meeting.

    Tries the Google Calendar API first. If credentials are missing or invalid,
    falls back to a randomly generated meet.google.com URL so the booking flow
    is never blocked by credential issues.
    """
    link = _try_google_calendar_meet(meeting_datetime, duration_minutes, title)
    if link:
        return link

    code = _random_meet_code()
    fallback = f"https://meet.google.com/{code}"
    logger.info("Using fallback Meet link: %s", fallback)
    return fallback
