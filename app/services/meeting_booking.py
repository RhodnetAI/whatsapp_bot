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
verification  – all details collected, showing summary and asking to confirm
completed     – booking saved and confirmed
declined      – user said No (suppressed for the rest of the day)
"""

import datetime
import logging
import re
from typing import Any

from app.db.supabase_client import first_row, supabase, supabase_admin

logger = logging.getLogger("whatsapp")

# Booking steps that are inside the strict booking flow (no normal AI reply)
BOOKING_FLOW_STEPS = {"showing_slots", "asked_name", "asked_email", "verification"}

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
                b_start = datetime.datetime.fromisoformat(bdt)
                b_end = b_start + datetime.timedelta(minutes=int(bdur))
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
                start=row.get("time_start", "09:00"),
                end=row.get("time_end", "17:00"),
                exclude_start=row.get("exclude_time_start", ""),
                exclude_end=row.get("exclude_time_end", ""),
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


def _format_slots_message(slots: list[dict[str, str]]) -> str:
    if not slots:
        return (
            "I'm sorry, there are no available slots in the next 7 days. "
            "Please check back later."
        )
    lines = ["Here are the available time slots:\n"]
    for i, slot in enumerate(slots, start=1):
        lines.append(f"{i}. {slot['label']}")
    lines.append("\nPlease reply with the number of your preferred slot.")
    lines.append(_NO_FOOTER)
    return "\n".join(lines)


def _parse_slot_selection(
    user_message: str, slots: list[dict[str, str]]
) -> dict[str, str] | None:
    """Try to match user_message to one of the shown slots by number or label."""
    text = user_message.strip()
    # Try numeric selection
    try:
        idx = int(text) - 1
        if 0 <= idx < len(slots):
            return slots[idx]
    except ValueError:
        pass
    # Try partial label match (case-insensitive)
    text_lower = text.lower()
    for slot in slots:
        if text_lower in slot["label"].lower():
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
        f"📧 Email: {user_email}\n\n"
        "Are all details correct? Reply *confirm* to book, or tell me what you'd like to change."
        f"{_NO_FOOTER}"
    )
    return msg


def _apply_correction(partial: dict[str, Any], user_message: str) -> tuple[dict[str, Any], str]:
    """Try to detect what the user wants to change and apply it.
    Returns (updated_partial, clarification_or_empty)."""
    text = user_message.strip()
    # Email correction
    if _is_valid_email(text):
        partial["user_email"] = text
        return partial, ""
    # Name correction heuristic: short text with no @ and no digits
    if len(text) < 60 and "@" not in text and not any(c.isdigit() for c in text):
        lower = text.lower()
        # If message says "name is X" or "my name is X", extract X
        name_match = re.search(r"(?:name\s+is|name:)\s*(.+)", lower)
        if name_match:
            partial["user_name"] = name_match.group(1).strip().title()
            return partial, ""
        # Otherwise treat the whole message as a name replacement if it looks like one
        if 2 <= len(text.split()) <= 4:
            partial["user_name"] = text.title()
            return partial, ""
    # Couldn't figure out what to change — ask to go through booking again
    return partial, (
        "I wasn't sure what to change. Let me walk you through the booking again. "
        "What would you like to update — date/time, name, or email?"
    )


def build_conversation_summary(conversation_data: list[dict[str, Any]], max_turns: int = 6) -> str:
    """Extract the last few conversation turns as a brief context summary."""
    entries = [e for e in conversation_data if isinstance(e, dict)]
    recent = entries[-max_turns:] if len(entries) > max_turns else entries
    lines = []
    for e in recent:
        q = (e.get("query") or "").strip()
        r = (e.get("response") or "").strip()
        if q:
            lines.append(f"User: {q}")
        if r:
            # Truncate very long bot replies in summary
            snippet = r[:200] + "…" if len(r) > 200 else r
            lines.append(f"Bot: {snippet}")
    return "\n".join(lines) if lines else "No conversation context available."


# ── Booking persistence ───────────────────────────────────────────────────────

def save_booking(
    sender: str,
    partial: dict[str, Any],
    meet_link: str,
    summary: str,
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

        row = {
            "sender": sender,
            "user_name": user_name,
            "user_email": user_email,
            "meeting_datetime": meeting_dt.isoformat(),
            "duration_minutes": duration,
            "meet_link": meet_link,
            "conversation_summary": summary,
        }
        res = _db().table("meeting_bookings").insert(row).execute()
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
        selected = _parse_slot_selection(text, shown)
        if selected:
            partial["slot_date"] = selected["date"]
            partial["slot_start"] = selected["start"]
            partial["slot_end"] = selected["end"]
            state["partial"] = partial
            state["step"] = "asked_name"
            return (
                f"Great choice! I've noted *{selected['label']}*.\n\n"
                "What is your full name?" + _NO_FOOTER,
                state,
                False,
                None,
            )
        else:
            # Re-show slots
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
                "I didn't recognise that selection. Please choose a number from the list:\n\n"
                + _format_slots_message(slots),
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
            f"Thanks, *{text}*! What is your email address?" + _NO_FOOTER,
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
        state["step"] = "verification"
        return (
            _build_verification_message(partial),
            state,
            False,
            None,
        )

    # ── verification ──────────────────────────────────────────────────────────
    if step == "verification":
        lower = text.lower()
        if lower in ("confirm", "yes", "correct", "ok", "okay", "looks good", "confirmed"):
            state["step"] = "completed"
            return (
                "Your meeting has been booked! You'll receive a confirmation email shortly. "
                "The Google Meet link will be included.",
                state,
                True,
                dict(partial),
            )

        updated_partial, clarification = _apply_correction(partial, text)
        state["partial"] = updated_partial
        if clarification:
            return clarification + _NO_FOOTER, state, False, None
        return _build_verification_message(updated_partial), state, False, None

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
        return _format_slots_message(slots), state

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
