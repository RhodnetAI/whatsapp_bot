"""Read-only schedule overview for the dashboard calendar.

Combines configured business availability (information_bot_scheduler) with
confirmed meetings (meeting_bookings) so the frontend can render a real-time
weekly calendar. Strictly read-only — booking creation stays in the WhatsApp
flow (see app/services/meeting_booking.py).
"""

import logging

from fastapi import APIRouter, Depends

from app.core.security import verify_token
from app.db.supabase_client import supabase, supabase_admin

logger = logging.getLogger("whatsapp")

router = APIRouter(prefix="/schedule", tags=["schedule"])


def _db():
    return supabase_admin if supabase_admin is not None else supabase


@router.get("/overview")
async def get_schedule_overview(token: dict = Depends(verify_token)):
    """Return raw availability rows and meeting bookings for the calendar.

    The frontend maps these onto whichever week is in view, so the response is
    not date-filtered — both datasets are small (working-hours config + a
    rolling set of bookings).
    """
    availability: list[dict] = []
    bookings: list[dict] = []

    try:
        res = (
            _db()
            .table("information_bot_scheduler")
            .select(
                "id, day_of_week, time_start, time_end, exclude_time_start, "
                "exclude_time_end, is_special_time, special_date"
            )
            .execute()
        )
        availability = [r for r in (getattr(res, "data", None) or []) if isinstance(r, dict)]
    except Exception:
        logger.exception("Failed to fetch information_bot_scheduler rows")

    try:
        res = (
            _db()
            .table("meeting_bookings")
            .select(
                "id, sender, user_name, user_email, meeting_datetime, "
                "duration_minutes, meet_link, purpose, calendar_event_link, created_at"
            )
            .order("meeting_datetime", desc=False)
            .execute()
        )
        bookings = [r for r in (getattr(res, "data", None) or []) if isinstance(r, dict)]
    except Exception:
        logger.exception("Failed to fetch meeting_bookings rows")

    return {"availability": availability, "bookings": bookings}
