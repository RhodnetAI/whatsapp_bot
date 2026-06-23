"""
Meeting booking state machine.

State is stored in conversation_data[-1]["meeting_state"] using the same
pattern as flow_ai.py stores flow_state — no separate table, no extra DB
reads per turn.

Steps
------
idle          – scheduler enabled but no active booking interaction
asked_yes_no  – bot asked "Would you like to schedule?" and is waiting
showing_slots – bot displayed available slots, waiting for selection
asked_name    – slot chosen, waiting for user's name
asked_email   – name collected, waiting for email address
asked_purpose – email collected, waiting for meeting purpose
verification  – all details collected, showing summary and asking to confirm
completed     – booking saved and confirmed
declined      – user said No (suppressed for the rest of the day)
"""

import datetime
import logging
import re
from typing import Any, cast

from app.db.supabase_client import first_row, supabase, supabase_admin

logger = logging.getLogger("whatsapp")

# Booking steps that are inside the strict booking flow (no normal AI reply)
BOOKING_FLOW_STEPS = {"showing_slots", "asked_name", "asked_email", "asked_purpose", "verification"}

# Steps where the scheduler is inactive for the session
SUPPRESSED_STEPS = {"declined", "completed"}

_NO_RE = re.compile(r"^\s*no\s*$", re.IGNORECASE)
_YES_RE = re.compile(r"^\s*yes\s*$", re.IGNORECASE)

_NO_FOOTER = "\n\nType *NO* at any time to stop the booking and return to normal chat."


def _db():
    return supabase_admin if supabase_admin is not None else supabase


# ── State helpers ─────────────────────────────────────────────────────────────

def _empty_state() -> dict[str, Any]:
    return {
        "step": "idle",
        "suggestion_count": 0,
        "declined_date": None,
        "partial": {
            "slot_date": None,
            "slot_start": None,
            "slot_end": None,
            "user_name": None,
            "user_email": None,
            "purpose": None,
        },
        "shown_slots": [],
    }


def get_meeting_state(conversation_data: list[dict[str, Any]]) -> dict[str, Any]:
    if not conversation_data:
        return _empty_state()
    last = conversation_data[-1]
    if not isinstance(last, dict):
        return _empty_state()
    raw = last.get("meeting_state")
    if not isinstance(raw, dict):
        return _empty_state()
    # Merge with empty state so missing keys always have defaults
    state = _empty_state()
    state.update(raw)
    if not isinstance(state.get("partial"), dict):
        state["partial"] = _empty_state()["partial"]
    if not isinstance(state.get("shown_slots"), list):
        state["shown_slots"] = []
    return state


def set_meeting_state(conversation_data: list[dict[str, Any]], state: dict[str, Any]) -> None:
    if conversation_data:
        conversation_data[-1]["meeting_state"] = state


def fresh_meeting_state() -> dict[str, Any]:
    """A clean slate, used to reset the booking suggestion at the start of a new
    day-session (see `is_first_message_of_session` in bot_chat.py — same one
    calendar UTC day definition, derived from whatsapp_conversations)."""
    return _empty_state()


def is_declined_today(state: dict[str, Any]) -> bool:
    declined_date = state.get("declined_date")
    if not declined_date:
        return False
    try:
        d = datetime.date.fromisoformat(str(declined_date))
        return d == datetime.datetime.utcnow().date()
    except ValueError:
        return False


def is_suppressed_for_session(state: dict[str, Any]) -> bool:
    """True when we should never show the meeting suggestion again this session."""
    return state.get("step") in SUPPRESSED_STEPS or is_declined_today(state)


# ── Slot helpers ──────────────────────────────────────────────────────────────

def _time_to_minutes(t: str) -> int:
    """Convert "HH:MM" to minutes since midnight."""
    try:
        h, m = t.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def _minutes_to_time(mins: int) -> str:
    return f"{mins // 60:02d}:{mins % 60:02d}"


def _generate_30min_slots(
    start: str,
    end: str,
    exclude_start: str,
    exclude_end: str,
) -> list[tuple[str, str]]:
    """Return list of (slot_start, slot_end) for 30-min slots within the window."""
    slots = []
    s = _time_to_minutes(start)
    e = _time_to_minutes(end)
    ex_s = _time_to_minutes(exclude_start) if exclude_start else -1
    ex_e = _time_to_minutes(exclude_end) if exclude_end else -1

    cursor = s
    while cursor + 30 <= e:
        slot_end = cursor + 30
        # Skip if slot overlaps with exclusion window
        if ex_s >= 0 and ex_e > ex_s:
            if cursor < ex_e and slot_end > ex_s:
                cursor += 30
                continue
        slots.append((_minutes_to_time(cursor), _minutes_to_time(slot_end)))
        cursor += 30
    return slots


def _is_slot_booked(date_str: str, slot_start: str, slot_end: str) -> bool:
    """Check whether this slot overlaps any existing booking in meeting_bookings."""
    try:
        start_dt = datetime.datetime.fromisoformat(f"{date_str}T{slot_start}:00+00:00")
        end_dt = datetime.datetime.fromisoformat(f"{date_str}T{slot_end}:00+00:00")
        # Fetch bookings that overlap: existing.start < our end AND existing.end > our start
        result = (
            _db()
            .table("meeting_bookings")
            .select("id, meeting_datetime, duration_minutes")
            .execute()
        )
        for row in result.data or []:
            if not isinstance(row, dict):
                continue
            bdt = row.get("meeting_datetime")
            bdur = row.get("duration_minutes") or 30
            if not bdt:
                continue
            try:
                b_start = datetime.datetime.fromisoformat(cast(str, bdt))
                b_end = b_start + datetime.timedelta(minutes=int(cast(Any, bdur)))
                if b_start < end_dt and b_end > start_dt:
                    return True
            except Exception:
                continue
    except Exception:
        logger.exception("Error checking slot availability")
    return False


def get_available_slots(days_ahead: int = 7) -> list[dict[str, str]]:
    """Return available 30-min slots over the next `days_ahead` days.

    Each entry: {"date": "YYYY-MM-DD", "start": "HH:MM", "end": "HH:MM",
                 "label": "Monday, Jan 06 10:00–10:30"}
    """
    try:
        rows_res = _db().table("information_bot_scheduler").select("*").execute()
        scheduler_rows = [r for r in (rows_res.data or []) if isinstance(r, dict)]
    except Exception:
        logger.exception("Failed to fetch scheduler rows")
        return []

    today = datetime.datetime.utcnow().date()
    available: list[dict[str, str]] = []

    for offset in range(days_ahead):
        target = today + datetime.timedelta(days=offset)
        date_str = target.isoformat()
        weekday = target.strftime("%A")

        # Special-date rows override regular day-of-week rows
        special = [
            r for r in scheduler_rows
            if r.get("is_special_time") and r.get("special_date") == date_str
        ]
        regular = [
            r for r in scheduler_rows
            if not r.get("is_special_time") and r.get("day_of_week") == weekday
        ]
        matching = special if special else regular

        for row in matching:
            slots = _generate_30min_slots(
                start=cast(str, row.get("time_start", "09:00")),
                end=cast(str, row.get("time_end", "17:00")),
                exclude_start=cast(str, row.get("exclude_time_start", "")),
                exclude_end=cast(str, row.get("exclude_time_end", "")),
            )
            for s_start, s_end in slots:
                if not _is_slot_booked(date_str, s_start, s_end):
                    label = f"{target.strftime('%A, %b %d')} {s_start}–{s_end}"
                    available.append({
                        "date": date_str,
                        "start": s_start,
                        "end": s_end,
                        "label": label,
                    })

    return available


_DAY_NAMES: dict[str, int] = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_MONTH_NAMES: dict[str, int] = {
    "jan": 1, "january": 1, "feb": 2, "february": 2,
    "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def _format_time_12h(t: str) -> str:
    """Convert "HH:MM" (24h) to "H:MM AM/PM" (12h)."""
    try:
        h, m = t.split(":")
        h, m = int(h), int(m)
        period = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12}:{m:02d} {period}"
    except Exception:
        return t


def _merge_slots_to_blocks(slots: list[dict[str, str]]) -> dict[str, dict]:
    """Group individual 30-min slots by date and merge consecutive ones into blocks.

    Returns an ordered dict keyed by date string (sorted ascending):
      {"YYYY-MM-DD": {"day_label": "Monday, Jun 15",
                      "blocks": [{"start": "HH:MM", "end": "HH:MM"}, ...]}}
    """
    by_date: dict[str, list[dict[str, str]]] = {}
    for slot in slots:
        by_date.setdefault(slot["date"], []).append(slot)

    result: dict[str, dict] = {}
    for date_str in sorted(by_date.keys()):
        day_slots = sorted(by_date[date_str], key=lambda s: s["start"])
        blocks: list[dict[str, str]] = []
        if day_slots:
            blk_start = day_slots[0]["start"]
            blk_end = day_slots[0]["end"]
            for s in day_slots[1:]:
                if s["start"] == blk_end:
                    blk_end = s["end"]          # extend current block
                else:
                    blocks.append({"start": blk_start, "end": blk_end})
                    blk_start, blk_end = s["start"], s["end"]
            blocks.append({"start": blk_start, "end": blk_end})

        try:
            day_label = datetime.date.fromisoformat(date_str).strftime("%A, %b %d")
        except ValueError:
            day_label = date_str

        result[date_str] = {"day_label": day_label, "blocks": blocks}

    return result


def _format_grouped_slots_message(slots: list[dict[str, str]]) -> str:
    if not slots:
        return (
            "I'm sorry, there are no available slots in the next 7 days. "
            "Please check back later."
        )
    grouped = _merge_slots_to_blocks(slots)
    lines = ["Available slots:\n"]
    for day_info in grouped.values():
        lines.append(f"*{day_info['day_label']}*")
        for block in day_info["blocks"]:
            lines.append(f"  {_format_time_12h(block['start'])} – {_format_time_12h(block['end'])}")
        lines.append("")
    lines.append("Reply with your preferred day and time.")
    lines.append("For example: *Monday 10:30 AM*")
    lines.append(_NO_FOOTER)
    return "\n".join(lines)


def _parse_user_time_selection(
    user_message: str,
    slots: list[dict[str, str]],
) -> dict[str, str] | None:
    """Parse "Monday 10:30 AM" style input and return the matching 30-min slot.

    Recognises:
      - Day-of-week names (full or short) — matched against dates in `slots`
      - Month/day references ("Jun 15", "15 June", "6/15")
      - Times in 12h ("10:30 AM", "10 AM") or 24h ("14:30") — floored to nearest :00 or :30
    Returns the first matching slot from `slots`, or None.
    """
    text = re.sub(r"[,@]", " ", user_message.strip().lower())
    available_dates = sorted({s["date"] for s in slots})
    target_date: str | None = None

    # ── Day-of-week name ──────────────────────────────────────────────────
    for name, weekday in _DAY_NAMES.items():
        if re.search(r"\b" + name + r"\b", text):
            for date_str in available_dates:
                try:
                    if datetime.date.fromisoformat(date_str).weekday() == weekday:
                        target_date = date_str
                        break
                except ValueError:
                    continue
            if target_date:
                break

    # ── Month-name + day number ───────────────────────────────────────────
    if not target_date:
        for m_name, m_num in _MONTH_NAMES.items():
            # "jun 15" or "june 15"
            m = re.search(r"\b" + m_name + r"\w*\s+(\d{1,2})\b", text)
            if m:
                day_num = int(m.group(1))
            else:
                # "15 jun" or "15 june"
                m = re.search(r"\b(\d{1,2})\s+" + m_name + r"\w*\b", text)
                if m:
                    day_num = int(m.group(1))
                else:
                    continue
            for date_str in available_dates:
                try:
                    d = datetime.date.fromisoformat(date_str)
                    if d.month == m_num and d.day == day_num:
                        target_date = date_str
                        break
                except ValueError:
                    continue
            if target_date:
                break

    # ── Numeric month/day: "6/15" or "6-15" ──────────────────────────────
    if not target_date:
        m = re.search(r"\b(\d{1,2})[/\-](\d{1,2})\b", text)
        if m:
            m_num, day_num = int(m.group(1)), int(m.group(2))
            for date_str in available_dates:
                try:
                    d = datetime.date.fromisoformat(date_str)
                    if d.month == m_num and d.day == day_num:
                        target_date = date_str
                        break
                except ValueError:
                    continue

    if not target_date:
        return None

    # ── Parse time ────────────────────────────────────────────────────────
    hour: int | None = None
    minute: int = 0

    # "HH:MM" optionally followed by am/pm
    m = re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        period = m.group(3)
        if period == "pm" and hour != 12:
            hour += 12
        elif period == "am" and hour == 12:
            hour = 0

    # "HH am/pm" (no colon)
    if hour is None:
        m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", text)
        if m:
            hour = int(m.group(1))
            minute = 0
            period = m.group(2)
            if period == "pm" and hour != 12:
                hour += 12
            elif period == "am" and hour == 12:
                hour = 0

    if hour is None:
        return None

    # Floor to nearest 30-minute boundary (e.g. 10:45 → 10:30, 10:14 → 10:00)
    minute = 0 if minute < 30 else 30
    time_str = f"{hour:02d}:{minute:02d}"

    # ── Find exact slot ───────────────────────────────────────────────────
    for slot in slots:
        if slot["date"] == target_date and slot["start"] == time_str:
            return slot

    return None


def _is_valid_email(text: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", text.strip()))


def _build_verification_message(partial: dict[str, Any]) -> str:
    slot_date = partial.get("slot_date", "—")
    slot_start = partial.get("slot_start", "—")
    slot_end = partial.get("slot_end", "—")
    user_name = partial.get("user_name", "—")
    user_email = partial.get("user_email", "—")
    purpose = partial.get("purpose", "—")

    try:
        d = datetime.date.fromisoformat(str(slot_date))
        date_label = d.strftime("%A, %B %d, %Y")
    except (ValueError, TypeError):
        date_label = str(slot_date)

    msg = (
        "Please verify your booking details:\n\n"
        f"📅 Date:  {date_label}\n"
        f"⏰ Time:  {slot_start} – {slot_end}\n"
        f"👤 Name:  {user_name}\n"
        f"📧 Email: {user_email}\n"
        f"📝 Purpose: {purpose}\n\n"
        "Please reply *Confirm* to proceed."
        f"{_NO_FOOTER}"
    )
    return msg


# ── Booking persistence ───────────────────────────────────────────────────────

def save_booking(
    sender: str,
    partial: dict[str, Any],
    meet_link: str,
    purpose: str,
    calendar_event_id: str = "",
    calendar_event_link: str = "",
) -> str | None:
    """Insert a row into meeting_bookings. Returns the new row's id or None."""
    try:
        slot_date = partial["slot_date"]
        slot_start = partial["slot_start"]
        slot_end = partial["slot_end"]
        user_name = partial["user_name"]
        user_email = partial["user_email"]

        start_mins = _time_to_minutes(str(slot_start))
        end_mins = _time_to_minutes(str(slot_end))
        duration = max(30, end_mins - start_mins)

        meeting_dt = datetime.datetime.fromisoformat(f"{slot_date}T{slot_start}:00+00:00")

        row: dict[str, Any] = {
            "sender": sender,
            "user_name": user_name,
            "user_email": user_email,
            "meeting_datetime": meeting_dt.isoformat(),
            "duration_minutes": duration,
            "meet_link": meet_link,
            "purpose": purpose,
        }
        if calendar_event_id:
            row["calendar_event_id"] = calendar_event_id
        if calendar_event_link:
            row["calendar_event_link"] = calendar_event_link

        try:
            res = _db().table("meeting_bookings").insert(row).execute()
        except Exception as exc:
            # The calendar_event_id/calendar_event_link columns may not exist yet
            # in Supabase's schema cache (PGRST204). Retry without them so the
            # booking is still saved.
            if "calendar_event_id" in str(exc) or "calendar_event_link" in str(exc):
                logger.warning(
                    "calendar_event_id/calendar_event_link columns missing; "
                    "retrying booking insert without them for sender=%s", sender,
                )
                row.pop("calendar_event_id", None)
                row.pop("calendar_event_link", None)
                res = _db().table("meeting_bookings").insert(row).execute()
            else:
                raise

        inserted = first_row(res)
        return inserted.get("id") if isinstance(inserted, dict) else None
    except Exception:
        logger.exception("Failed to save booking for sender=%s", sender)
        return None


# ── Main state machine ────────────────────────────────────────────────────────

def process_meeting_step(
    user_message: str,
    conversation_data: list[dict[str, Any]],
    state: dict[str, Any],
    sender: str,
) -> tuple[str, dict[str, Any], bool, dict[str, Any] | None]:
    """Drive one step of the booking flow.

    Called ONLY when state["step"] is in BOOKING_FLOW_STEPS.

    Returns:
        (response_message, updated_state, booking_complete, booking_data)
    where booking_data is the partial dict (used by the caller to generate the
    Meet link and send emails) if booking_complete is True, else None.
    """
    text = user_message.strip()
    step = state.get("step", "idle")
    partial: dict[str, Any] = state.get("partial", {})

    # Universal escape hatch
    if _NO_RE.match(text):
        state["step"] = "idle"
        return (
            "No problem! Booking cancelled. Feel free to ask me anything.",
            state,
            False,
            None,
        )

    # ── showing_slots ─────────────────────────────────────────────────────────
    if step == "showing_slots":
        shown = state.get("shown_slots", [])
        selected = _parse_user_time_selection(text, shown)
        if selected:
            partial["slot_date"] = selected["date"]
            partial["slot_start"] = selected["start"]
            partial["slot_end"] = selected["end"]
            state["partial"] = partial
            state["step"] = "asked_name"
            slot_label = (
                f"{datetime.date.fromisoformat(selected['date']).strftime('%A, %b %d')} "
                f"{_format_time_12h(selected['start'])} – {_format_time_12h(selected['end'])}"
            )
            return (
                f"Great choice! I've noted *{slot_label}*.\n\n"
                "What is your full name?\n\nExample: *John Smith*" + _NO_FOOTER,
                state,
                False,
                None,
            )
        else:
            slots = get_available_slots()
            if not slots:
                state["step"] = "idle"
                return (
                    "Sorry, I couldn't find any available slots right now. "
                    "Please try again later.",
                    state,
                    False,
                    None,
                )
            state["shown_slots"] = slots
            return (
                "Please choose a valid start time from the available blocks:\n\n"
                + _format_grouped_slots_message(slots),
                state,
                False,
                None,
            )

    # ── asked_name ────────────────────────────────────────────────────────────
    if step == "asked_name":
        if len(text) < 2 or len(text) > 100:
            return (
                "Please provide your full name (2–100 characters)." + _NO_FOOTER,
                state,
                False,
                None,
            )
        partial["user_name"] = text
        state["partial"] = partial
        state["step"] = "asked_email"
        return (
            f"Thanks, *{text}*! What is your email address?\n\nExample: john@example.com*" + _NO_FOOTER,
            state,
            False,
            None,
        )

    # ── asked_email ───────────────────────────────────────────────────────────
    if step == "asked_email":
        if not _is_valid_email(text):
            return (
                "That doesn't look like a valid email address. "
                "Please enter a valid email (e.g. name@example.com)." + _NO_FOOTER,
                state,
                False,
                None,
            )
        partial["user_email"] = text
        state["partial"] = partial
        state["step"] = "asked_purpose"
        return (
            "What is the purpose of this meeting? Please describe briefly.\n"
            "For example: Product demo, General inquiry, Support discussion" + _NO_FOOTER,
            state,
            False,
            None,
        )

    # ── asked_purpose ─────────────────────────────────────────────────────────
    if step == "asked_purpose":
        if len(text) < 2 or len(text) > 300:
            return (
                "Please briefly describe the purpose of the meeting (2–300 characters)." + _NO_FOOTER,
                state,
                False,
                None,
            )
        partial["purpose"] = text
        state["partial"] = partial
        state["step"] = "verification"
        return (
            _build_verification_message(partial),
            state,
            False,
            None,
        )

    # ── verification ──────────────────────────────────────────────────────────
    if step == "verification":
        if text.lower().strip() == "confirm":
            state["step"] = "completed"
            return (
                "Your meeting has been booked! You'll receive a confirmation email shortly. "
                "The Google Meet link will be included.",
                state,
                True,
                dict(partial),
            )

        # Anything other than "confirm" — re-ask without allowing edits
        return (
            _build_verification_message(partial),
            state,
            False,
            None,
        )

    # Should never reach here
    state["step"] = "idle"
    return "Something went wrong. Let's start over.", state, False, None


def handle_yes_no_response(
    user_message: str,
    state: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    """Handle a user message when state is 'asked_yes_no'.

    Returns (extra_message_or_None, updated_state).
    extra_message is the slots list if user said Yes, a decline ack if No,
    or None if we just continue with normal AI (unanswered, re-ask via suggestion).
    """
    text = user_message.strip()

    if _YES_RE.match(text):
        slots = get_available_slots()
        if not slots:
            state["step"] = "idle"
            return (
                "I'm sorry, there are no available time slots in the next 7 days. "
                "Please check back later.",
                state,
            )
        state["step"] = "showing_slots"
        state["shown_slots"] = slots
        return _format_grouped_slots_message(slots), state

    if _NO_RE.match(text):
        state["step"] = "declined"
        state["declined_date"] = datetime.datetime.utcnow().date().isoformat()
        return (
            "No problem at all! Feel free to ask me anything else.",
            state,
        )

    # User sent something unrelated — did not answer yes/no
    count = state.get("suggestion_count", 0)
    if count >= 2:
        # Two unanswered prompts already — disable for session
        state["step"] = "idle"
        state["suggestion_count"] = 99  # won't trigger again
    # Return None so the caller knows no extra message is needed right now;
    # the scheduler suggestion logic will decide whether to re-ask
    return None, state
