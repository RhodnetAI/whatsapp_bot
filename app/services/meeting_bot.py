"""
Meeting booking flow service.

Manages a per-sender per-day state machine for scheduling meetings via WhatsApp.
Integrates with the Information Bot's scheduler section (scheduler_enabled flag)
and runs alongside — never instead of — the normal Knowledge AI reply path.

State machine (flow_step values):
  idle            Normal AI handles the conversation. Intent detection may
                  append a meeting suggestion to the AI reply.
  asked_yes_no    Waiting for the user to reply Yes or No.
  showing_slots   Available time windows have been sent; waiting for slot pick.
  asked_duration  Slot confirmed; waiting for meeting duration.
  asked_confirm   Summary shown; waiting for the word "Confirm".
  asked_name_email Confirmed; waiting for full name + email address.
  completed       Booking stored, emails sent, Meet link created. Normal AI resumes.
  declined        User said No today. Normal AI resumes; no re-suggestion today.
"""

import asyncio
import datetime
import html as _html
import json
import logging
import re
import smtplib
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import httpx

from app.core.config import settings
from app.db.supabase_client import first_row, supabase, supabase_admin

logger = logging.getLogger("whatsapp")

_TABLE_SESSION = "meeting_session_state"
_TABLE_BOOKINGS = "meeting_bookings"
_MEET_API_URL = "https://meet.googleapis.com/v2/spaces"
_MEET_SCOPES = ["https://www.googleapis.com/auth/meetings.space.created"]

_DAYS_LOWER = [
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
]
_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.IGNORECASE)


# ── helpers ───────────────────────────────────────────────────────────────────

def _db() -> Any:
    return supabase_admin if supabase_admin is not None else supabase


def _today() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


def _fmt_time(t: str) -> str:
    """Convert 24-h "HH:MM" to "H:MM AM/PM"."""
    try:
        h, m = map(int, t.split(":"))
        suffix = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12}:{m:02d} {suffix}"
    except Exception:
        return t


def _nice_date(date_str: str) -> str:
    """Convert "YYYY-MM-DD" to "Monday, June 9"."""
    try:
        d = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        return f"{d.strftime('%A, %B')} {d.day}"
    except Exception:
        return date_str


def _nice_date_long(date_str: str) -> str:
    """Convert "YYYY-MM-DD" to "Monday, June 9, 2026"."""
    try:
        d = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        return f"{d.strftime('%A, %B')} {d.day}, {d.year}"
    except Exception:
        return date_str


# ── session state ─────────────────────────────────────────────────────────────

async def get_session_state(sender: str) -> dict[str, Any]:
    """Fetch today's session row for sender, or return a default idle dict."""
    today = _today()
    try:
        result = (
            _db()
            .table(_TABLE_SESSION)
            .select("*")
            .eq("sender", sender)
            .eq("state_date", today)
            .limit(1)
            .execute()
        )
        row = first_row(result)
        if row:
            if not isinstance(row.get("partial_data"), dict):
                row["partial_data"] = {}
            return row
    except Exception:
        logger.exception("Failed to fetch meeting session for sender=%s", sender)

    return {
        "sender": sender,
        "state_date": today,
        "flow_step": "idle",
        "partial_data": {},
        "declined_today": False,
        "suggestion_made": False,
    }


async def upsert_session_state(
    sender: str,
    flow_step: str,
    partial_data: dict[str, Any],
    *,
    declined_today: bool = False,
    suggestion_made: bool = True,
) -> None:
    """Create or update today's session row for sender."""
    now = datetime.datetime.utcnow().isoformat()
    try:
        _db().table(_TABLE_SESSION).upsert(
            {
                "sender": sender,
                "state_date": _today(),
                "flow_step": flow_step,
                "partial_data": partial_data,
                "declined_today": declined_today,
                "suggestion_made": suggestion_made,
                "updated_at": now,
            },
            on_conflict="sender,state_date",
        ).execute()
    except Exception:
        logger.exception("Failed to upsert meeting session for sender=%s", sender)


# ── intent detection ──────────────────────────────────────────────────────────

async def detect_meeting_intent(conversation_data: list[dict[str, Any]]) -> bool:
    """
    Return True if the user looks like a serious potential customer who would
    benefit from a meeting. Requires at least 2 completed turns before firing.
    Uses Groq (llama-3.1-8b-instant) with a keyword heuristic as fallback.
    """
    completed = [
        e for e in conversation_data[:-1]
        if isinstance(e, dict) and e.get("response") and not e.get("manual")
    ]
    if len(completed) < 2:
        return False

    lines: list[str] = []
    for e in completed[-5:]:
        q = (e.get("query") or "").strip()
        r = (e.get("response") or "").strip()
        if q:
            lines.append(f"User: {q}")
        if r:
            lines.append(f"Bot: {r}")
    conv_text = "\n".join(lines)

    if settings.groq_api_key:
        try:
            from groq import Groq

            client = Groq(api_key=settings.groq_api_key)
            resp = await asyncio.to_thread(
                client.chat.completions.create,
                model="llama-3.1-8b-instant",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a lead qualification assistant. "
                            "Analyse the conversation and decide if the user is a serious "
                            "potential customer who would benefit from scheduling a meeting "
                            "(e.g. asking about pricing, demos, purchases, or wanting to "
                            "discuss services in depth). Reply with exactly YES or NO."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"Conversation:\n{conv_text}\n\nOffer a meeting? YES or NO:",
                    },
                ],
                max_tokens=5,
                temperature=0.0,
            )
            answer = (resp.choices[0].message.content or "").strip().upper()
            return answer.startswith("YES")
        except Exception:
            logger.exception("Groq intent detection failed; falling back to heuristic")

    # Keyword heuristic fallback
    keywords = [
        "price", "pricing", "cost", "demo", "trial", "consultation",
        "meeting", "call", "discuss", "purchase", "buy", "subscribe",
        "interested", "package", "plan", "quote", "proposal",
    ]
    text_lower = conv_text.lower()
    return sum(1 for kw in keywords if kw in text_lower) >= 2


# ── scheduler slot helpers ────────────────────────────────────────────────────

async def _get_scheduler_entries() -> list[dict[str, Any]]:
    try:
        result = (
            _db()
            .table("information_bot_scheduler")
            .select("*")
            .eq("is_special_time", False)
            .execute()
        )
        return result.data or []
    except Exception:
        logger.exception("Failed to fetch scheduler entries")
        return []


async def format_available_slots() -> str:
    """Return a WhatsApp-formatted list of available time windows for the next 7 days."""
    entries = await _get_scheduler_entries()
    day_map = {
        e.get("day_of_week", "").lower(): e
        for e in entries
        if e.get("day_of_week")
    }

    today = datetime.datetime.utcnow()
    lines: list[str] = []

    for i in range(7):
        target = today + datetime.timedelta(days=i)
        day_name = target.strftime("%A")
        date_label = f"{target.strftime('%B')} {target.day}"

        entry = day_map.get(day_name.lower())
        if entry:
            t_s = _fmt_time(entry.get("time_start", "09:00"))
            t_e = _fmt_time(entry.get("time_end", "17:00"))
            ex_s = entry.get("exclude_time_start", "")
            ex_e = entry.get("exclude_time_end", "")
            if ex_s and ex_e:
                window = f"{t_s}–{_fmt_time(ex_s)} and {_fmt_time(ex_e)}–{t_e}"
            else:
                window = f"{t_s}–{t_e}"
            lines.append(f"• *{day_name}, {date_label}:* {window}")
        else:
            lines.append(f"• *{day_name}, {date_label}:* Not available")

    return "\n".join(lines) if lines else "No time slots are currently configured. Please contact us directly."


def _parse_slot_from_text(
    text: str, scheduler_entries: list[dict[str, Any]]
) -> tuple[str, str] | None:
    """
    Extract the user's chosen date and time from free text and validate it
    against the scheduler entries.
    Returns ("YYYY-MM-DD", "HH:MM") or None if invalid / not found.
    """
    text_lower = text.lower().strip()
    today_utc = datetime.datetime.utcnow()

    target_date: datetime.datetime | None = None
    mentioned_day: str | None = None

    if "today" in text_lower:
        target_date = today_utc
        mentioned_day = today_utc.strftime("%A").lower()
    elif "tomorrow" in text_lower:
        target_date = today_utc + datetime.timedelta(days=1)
        mentioned_day = target_date.strftime("%A").lower()
    else:
        for day in _DAYS_LOWER:
            if day in text_lower:
                mentioned_day = day
                for i in range(7):
                    c = today_utc + datetime.timedelta(days=i)
                    if c.strftime("%A").lower() == day:
                        target_date = c
                        break
                break

    if not mentioned_day or target_date is None:
        return None

    day_map = {
        e.get("day_of_week", "").lower(): e
        for e in scheduler_entries
        if e.get("day_of_week")
    }
    entry = day_map.get(mentioned_day)
    if not entry:
        return None

    m = _TIME_RE.search(text_lower)
    if not m:
        return None

    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    meridiem = (m.group(3) or "").lower()

    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    elif not meridiem and 1 <= hour <= 6:
        # Assume PM for ambiguous small hours in a business scheduling context
        hour += 12

    if hour > 23 or minute > 59:
        return None

    try:
        ts_h, ts_m = map(int, entry.get("time_start", "09:00").split(":"))
        te_h, te_m = map(int, entry.get("time_end", "17:00").split(":"))
        req = hour * 60 + minute
        start = ts_h * 60 + ts_m
        end = te_h * 60 + te_m

        if not (start <= req < end):
            return None

        ex_s = entry.get("exclude_time_start", "")
        ex_e = entry.get("exclude_time_end", "")
        if ex_s and ex_e:
            exs_h, exs_m = map(int, ex_s.split(":"))
            exe_h, exe_m = map(int, ex_e.split(":"))
            if (exs_h * 60 + exs_m) <= req < (exe_h * 60 + exe_m):
                return None

        return target_date.strftime("%Y-%m-%d"), f"{hour:02d}:{minute:02d}"
    except Exception:
        return None


def _parse_duration_minutes(text: str) -> int | None:
    """Parse natural-language duration into minutes. Returns None if not found."""
    text_lower = text.lower()

    if re.search(r"\ban?\s+hours?\b", text_lower):
        return 60

    total = 0
    h_m = re.search(r"(\d+(?:\.\d+)?)\s*hours?", text_lower)
    m_m = re.search(r"(\d+)\s*min(?:utes?)?", text_lower)

    if h_m:
        total += int(float(h_m.group(1)) * 60)
    if m_m:
        total += int(m_m.group(1))

    if total > 0:
        return total

    bare = re.search(r"\b(\d+)\b", text_lower)
    if bare:
        n = int(bare.group(1))
        if n <= 4:
            return n * 60   # treat small integers as hours
        if n <= 480:
            return n        # treat larger integers as minutes

    return None


def _parse_name_email(text: str) -> tuple[str | None, str | None]:
    """Extract (name, email) from a free-text message. Returns (None, None) if email missing."""
    email_m = re.search(r"[\w.+\-]+@[\w\-]+\.[a-zA-Z]{2,}", text)
    if not email_m:
        return None, None

    email = email_m.group(0)
    name_raw = text[: email_m.start()] + text[email_m.end() :]
    name_raw = re.sub(r"[,|/\\]", " ", name_raw)
    name_raw = re.sub(r"\s+", " ", name_raw).strip()
    name = name_raw if len(name_raw) >= 2 else None
    return name, email


def build_conversation_summary(conversation_data: list[dict[str, Any]]) -> str:
    """Build a short plain-text summary of the last 5 user/bot turns."""
    entries = [
        e for e in conversation_data
        if isinstance(e, dict) and e.get("query") and not e.get("manual")
    ]
    lines: list[str] = []
    for e in entries[-5:]:
        q = (e.get("query") or "").strip()[:150]
        r = (e.get("response") or "").strip()[:150]
        if q:
            lines.append(f"User: {q}")
        if r:
            lines.append(f"Bot: {r}")
    return "\n".join(lines)


# ── Google Meet ───────────────────────────────────────────────────────────────

async def create_google_meet_link() -> str:
    """
    Create a Google Meet space via the Meet REST API v2 and return its URI.
    Returns "" on any error so booking can still complete without a link.
    """
    if not settings.google_service_account_json.strip():
        return ""

    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_info(
            json.loads(settings.google_service_account_json),
            scopes=_MEET_SCOPES,
        )
        await asyncio.to_thread(creds.refresh, Request())

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                _MEET_API_URL,
                headers={"Authorization": f"Bearer {creds.token}"},
                json={},
                timeout=10.0,
            )
            resp.raise_for_status()
            return resp.json().get("meetingUri", "")
    except Exception:
        logger.exception("Failed to create Google Meet link")
        return ""


# ── booking storage ───────────────────────────────────────────────────────────

async def store_booking(sender: str, partial: dict[str, Any]) -> None:
    try:
        _db().table(_TABLE_BOOKINGS).insert(
            {
                "id": str(uuid.uuid4()),
                "sender": sender,
                "user_name": partial.get("user_name", ""),
                "user_email": partial.get("user_email", ""),
                "meeting_date": partial.get("meeting_date", ""),
                "meeting_time": partial.get("meeting_time", ""),
                "duration_minutes": partial.get("duration_minutes", 60),
                "conversation_summary": partial.get("conversation_summary", ""),
                "meeting_link": partial.get("meeting_link", ""),
                "created_at": datetime.datetime.utcnow().isoformat(),
            }
        ).execute()
    except Exception:
        logger.exception("Failed to store meeting booking for sender=%s", sender)


# ── email sending ─────────────────────────────────────────────────────────────

def _send_smtp_email(to: str, subject: str, html_body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["From"] = settings.gmail_user
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
        srv.login(settings.gmail_user, settings.gmail_app_password)
        srv.sendmail(settings.gmail_user, to, msg.as_string())


async def send_confirmation_emails(partial: dict[str, Any]) -> None:
    if not settings.gmail_user or not settings.gmail_app_password:
        logger.warning("Gmail credentials not configured; skipping confirmation emails")
        return

    user_email = _html.escape(partial.get("user_email", ""))
    user_name = _html.escape(partial.get("user_name", ""))
    sender_phone = _html.escape(partial.get("sender", ""))
    meet_link = partial.get("meeting_link", "")
    summary = _html.escape(partial.get("conversation_summary", ""))

    nice_date = _nice_date_long(partial.get("meeting_date", ""))
    nice_time = _fmt_time(partial.get("meeting_time", ""))
    duration = partial.get("duration_minutes", 60)

    if meet_link:
        meet_cell = f'<a href="{_html.escape(meet_link)}">{_html.escape(meet_link)}</a>'
    else:
        meet_cell = "Will be sent before the meeting"

    row_style = 'style="padding:8px;border:1px solid #ddd;"'
    hdr_style = 'style="padding:8px;border:1px solid #ddd;font-weight:bold;background:#f9f9f9;"'

    user_html = f"""
<html><body style="font-family:Arial,sans-serif;color:#222;max-width:600px;margin:auto;">
<h2 style="color:#1a73e8;">Your Meeting is Confirmed!</h2>
<p>Hi {user_name},</p>
<p>Your meeting has been successfully scheduled. Here are the details:</p>
<table style="border-collapse:collapse;width:100%;">
  <tr><td {hdr_style}>Date</td><td {row_style}>{nice_date}</td></tr>
  <tr><td {hdr_style}>Time</td><td {row_style}>{nice_time}</td></tr>
  <tr><td {hdr_style}>Duration</td><td {row_style}>{duration} minutes</td></tr>
  <tr><td {hdr_style}>Google Meet Link</td><td {row_style}>{meet_cell}</td></tr>
</table>
<p style="margin-top:20px;">We look forward to speaking with you!</p>
</body></html>
"""

    admin_html = f"""
<html><body style="font-family:Arial,sans-serif;color:#222;max-width:600px;margin:auto;">
<h2 style="color:#1a73e8;">New Meeting Booking</h2>
<table style="border-collapse:collapse;width:100%;">
  <tr><td {hdr_style}>Name</td><td {row_style}>{user_name}</td></tr>
  <tr><td {hdr_style}>Email</td><td {row_style}>{user_email}</td></tr>
  <tr><td {hdr_style}>Phone (WhatsApp)</td><td {row_style}>{sender_phone}</td></tr>
  <tr><td {hdr_style}>Date</td><td {row_style}>{nice_date}</td></tr>
  <tr><td {hdr_style}>Time</td><td {row_style}>{nice_time}</td></tr>
  <tr><td {hdr_style}>Duration</td><td {row_style}>{duration} minutes</td></tr>
  <tr><td {hdr_style}>Google Meet Link</td><td {row_style}>{meet_cell}</td></tr>
</table>
<h3>Conversation Summary</h3>
<pre style="background:#f5f5f5;padding:12px;border-radius:4px;white-space:pre-wrap;">{summary}</pre>
</body></html>
"""

    try:
        await asyncio.to_thread(_send_smtp_email, partial.get("user_email", ""), "Your Meeting is Confirmed", user_html)
    except Exception:
        logger.exception("Failed to send user confirmation email to %s", partial.get("user_email"))

    if settings.admin_email:
        try:
            await asyncio.to_thread(
                _send_smtp_email,
                settings.admin_email,
                f"New Meeting Booked: {partial.get('user_name', '')}",
                admin_html,
            )
        except Exception:
            logger.exception("Failed to send admin notification email")


# ── main state machine ────────────────────────────────────────────────────────

async def process_meeting_message(
    sender: str,
    user_text: str,
    session_state: dict[str, Any],
    conversation_data: list[dict[str, Any]],
) -> str:
    """
    Drive the meeting booking state machine for one incoming message.
    Returns the reply string to send to the user.
    Only called when flow_step is an active booking step (not idle/completed/declined).
    """
    step = session_state.get("flow_step", "idle")
    partial: dict[str, Any] = dict(session_state.get("partial_data") or {})

    # ── asked_yes_no ──────────────────────────────────────────────────────────
    if step == "asked_yes_no":
        cleaned = user_text.strip().lower()

        if re.search(r"\b(yes|yeah|yep|yup|sure|ok|okay|y)\b", cleaned):
            slots_text = await format_available_slots()
            await upsert_session_state(
                sender, "showing_slots", partial, declined_today=False, suggestion_made=True
            )
            return (
                "Great! Here are our available time slots for the next 7 days:\n\n"
                + slots_text
                + "\n\nPlease tell me your preferred *date and time* "
                "(e.g., 'Monday at 10:00 AM')."
            )

        if re.search(r"\b(no|nope|nah|not now|maybe later|later|n)\b", cleaned):
            await upsert_session_state(
                sender, "declined", partial, declined_today=True, suggestion_made=True
            )
            return "No problem at all! Feel free to continue our conversation."

        # Unrelated message — acknowledge and re-ask
        return (
            "I'd be happy to help! But first, could you let me know — "
            "would you like to schedule a meeting with us? "
            "Please reply *Yes* or *No* to continue."
        )

    # ── showing_slots ─────────────────────────────────────────────────────────
    if step == "showing_slots":
        entries = await _get_scheduler_entries()
        result = _parse_slot_from_text(user_text, entries)

        if result:
            date_str, time_str = result
            partial["meeting_date"] = date_str
            partial["meeting_time"] = time_str
            await upsert_session_state(
                sender, "asked_duration", partial, declined_today=False, suggestion_made=True
            )
            return (
                f"Got it! *{_nice_date(date_str)} at {_fmt_time(time_str)}*.\n\n"
                "How long would you like the meeting? "
                "(e.g., '30 minutes', '1 hour', '45 min')"
            )

        slots_text = await format_available_slots()
        return (
            "I couldn't find that time in our available slots. "
            "Please choose from our available times:\n\n"
            + slots_text
            + "\n\nPlease tell me your preferred *date and time*."
        )

    # ── asked_duration ────────────────────────────────────────────────────────
    if step == "asked_duration":
        duration = _parse_duration_minutes(user_text)

        if duration and 15 <= duration <= 480:
            partial["duration_minutes"] = duration
            await upsert_session_state(
                sender, "asked_confirm", partial, declined_today=False, suggestion_made=True
            )
            return (
                "Here's your booking summary:\n\n"
                f"📅 *Date:* {_nice_date(partial.get('meeting_date', ''))}\n"
                f"⏰ *Time:* {_fmt_time(partial.get('meeting_time', ''))}\n"
                f"⏱ *Duration:* {duration} minutes\n\n"
                "Please reply *Confirm* to book this meeting."
            )

        return (
            "I didn't catch the duration. "
            "How long would you like the meeting? "
            "(e.g., '30 minutes', '1 hour', '45 min')"
        )

    # ── asked_confirm ─────────────────────────────────────────────────────────
    if step == "asked_confirm":
        if "confirm" in user_text.lower():
            await upsert_session_state(
                sender, "asked_name_email", partial, declined_today=False, suggestion_made=True
            )
            return (
                "Almost done! Please share your *full name* and *email address* "
                "to finalise the booking.\n\n"
                "_Example: John Smith, john@example.com_"
            )

        return (
            "Booking summary:\n\n"
            f"📅 *Date:* {_nice_date(partial.get('meeting_date', ''))}\n"
            f"⏰ *Time:* {_fmt_time(partial.get('meeting_time', ''))}\n"
            f"⏱ *Duration:* {partial.get('duration_minutes', '?')} minutes\n\n"
            "Reply *Confirm* to book, or let me know if you'd like to change anything."
        )

    # ── asked_name_email ──────────────────────────────────────────────────────
    if step == "asked_name_email":
        name, email = _parse_name_email(user_text)

        if name and email:
            partial["user_name"] = name
            partial["user_email"] = email
            partial["conversation_summary"] = build_conversation_summary(conversation_data)
            partial["sender"] = sender

            # Create Google Meet link (non-blocking on failure)
            meet_link = await create_google_meet_link()
            partial["meeting_link"] = meet_link

            # Persist booking and update session
            await store_booking(sender, partial)
            await upsert_session_state(
                sender, "completed", partial, declined_today=False, suggestion_made=True
            )

            # Send emails (errors are logged but do not fail the booking)
            try:
                await send_confirmation_emails(partial)
            except Exception:
                logger.exception("Email delivery failed for sender=%s", sender)

            meet_line = f"\n🔗 *Meet Link:* {meet_link}" if meet_link else ""
            return (
                "Your meeting has been booked! ✅\n\n"
                f"📅 *Date:* {_nice_date(partial.get('meeting_date', ''))}\n"
                f"⏰ *Time:* {_fmt_time(partial.get('meeting_time', ''))}\n"
                f"⏱ *Duration:* {partial.get('duration_minutes', '?')} minutes"
                f"{meet_line}\n\n"
                f"A confirmation email has been sent to {email}. "
                "We look forward to speaking with you!"
            )

        return (
            "Could you please share both your *full name* and *email address*?\n\n"
            "_Example: John Smith, john@example.com_"
        )

    # Fallback — should not reach here for active steps
    return ""
