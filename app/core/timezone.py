"""Central timezone helpers.

Policy (see also the frontend's Asia/Kolkata formatting):
  * The database always stores **true UTC** instants (timestamptz).
  * All user-facing output (WhatsApp, email, UI) is shown in **IST**.
  * The Information Bot scheduler's bare ``"HH:MM"`` values are **IST
    wall-clock** business hours and must be converted to UTC before they are
    persisted as a ``meeting_datetime``.

IST has no daylight saving and is a fixed UTC+05:30 offset, so a fixed-offset
``timezone`` is exact and avoids any dependency on the system tz database
(``zoneinfo``/``tzdata``), which is not reliably present on Windows.
"""

import datetime

UTC = datetime.timezone.utc
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="IST")
IST_LABEL = "IST"

# Name passed to the Google Calendar API (it interprets the label itself).
IST_TZ_NAME = "Asia/Kolkata"


def now_utc() -> datetime.datetime:
    """Timezone-aware current time in UTC."""
    return datetime.datetime.now(UTC)


def now_ist() -> datetime.datetime:
    """Timezone-aware current time in IST."""
    return datetime.datetime.now(IST)


def ist_today() -> datetime.date:
    """The current calendar date in IST (not UTC)."""
    return now_ist().date()


def to_ist(dt: datetime.datetime) -> datetime.datetime:
    """Return ``dt`` expressed in IST.

    A naive datetime is assumed to already be UTC (the storage convention).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)


def to_utc(dt: datetime.datetime) -> datetime.datetime:
    """Return ``dt`` expressed in UTC. Naive datetimes are assumed IST."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(UTC)


def ist_walltime_to_utc(date_str: str, hhmm: str) -> datetime.datetime:
    """Interpret ``date_str`` ("YYYY-MM-DD") + ``hhmm`` ("HH:MM") as an IST
    wall-clock moment and return the equivalent timezone-aware **UTC** datetime
    (suitable for storing in ``meeting_datetime``)."""
    y, mo, d = (int(p) for p in date_str.split("-"))
    h, m = (int(p) for p in hhmm.split(":"))
    ist_dt = datetime.datetime(y, mo, d, h, m, tzinfo=IST)
    return ist_dt.astimezone(UTC)


def format_ist_time(dt: datetime.datetime, with_label: bool = True) -> str:
    """e.g. ``"09:00 AM IST"`` — for emails / WhatsApp / UI."""
    text = to_ist(dt).strftime("%I:%M %p")
    return f"{text} {IST_LABEL}" if with_label else text


def format_ist_date(dt: datetime.datetime) -> str:
    """e.g. ``"Monday, June 23, 2026"`` (IST calendar date)."""
    return to_ist(dt).strftime("%A, %B %d, %Y")


def format_ist_datetime(dt: datetime.datetime, with_label: bool = True) -> str:
    """e.g. ``"Jun 23, 2026 09:00 AM IST"`` — used in email subjects."""
    ist = to_ist(dt)
    base = ist.strftime("%b %d, %Y %I:%M %p")
    return f"{base} {IST_LABEL}" if with_label else base
