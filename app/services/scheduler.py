import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db.supabase_client import first_row, supabase, supabase_admin
from app.models.schemas import SchedulerCreate, SchedulerItem, SchedulerUpdate

logger = logging.getLogger("whatsapp")

TABLE_NAME = "information_bot_scheduler"


def _db():
    """Return the admin client (bypasses RLS) when available, else fall back to anon client."""
    return supabase_admin if supabase_admin is not None else supabase


def _row_to_item(row: dict) -> SchedulerItem:
    return SchedulerItem(
        id=str(row.get("id")),
        day_of_week=row.get("day_of_week") or "",
        time_start=row.get("time_start") or "09:00",
        time_end=row.get("time_end") or "17:00",
        exclude_time_start=row.get("exclude_time_start") or "",
        exclude_time_end=row.get("exclude_time_end") or "",
        is_special_time=row.get("is_special_time") or False,
        special_date=row.get("special_date") or "",
        source=row.get("source") or "manual",
        vectorization_status=row.get("vectorization_status") or "processing",
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def list_items() -> list[SchedulerItem]:
    result = _db().table(TABLE_NAME).select("*").order("created_at", desc=True).execute()
    rows = getattr(result, "data", None) or []
    return [_row_to_item(row) for row in rows]


async def _create_row(fields: dict) -> SchedulerItem:
    item_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "id": item_id,
        **fields,
        "source": "manual",
        "vectorization_status": "done",
        "created_at": now,
        "updated_at": now,
    }
    _db().table(TABLE_NAME).insert(row).execute()

    return _row_to_item(row)


async def create_item(payload: SchedulerCreate) -> SchedulerItem:
    fields = payload.model_dump()
    return await _create_row(fields)


async def update_item(item_id: str, payload: SchedulerUpdate) -> SchedulerItem:
    existing = first_row(_db().table(TABLE_NAME).select("*").eq("id", item_id).execute())
    if existing is None:
        raise HTTPException(status_code=404, detail="Scheduler entry not found")
    
    fields = payload.model_dump()
    now = datetime.now(timezone.utc).isoformat()
    update_payload = {**fields, "vectorization_status": "done", "updated_at": now}
    _db().table(TABLE_NAME).update(update_payload).eq("id", item_id).execute()

    return _row_to_item({**existing, **update_payload})


async def delete_item(item_id: str) -> None:
    existing = first_row(_db().table(TABLE_NAME).select("*").eq("id", item_id).execute())
    if existing is None:
        raise HTTPException(status_code=404, detail="Scheduler entry not found")

    _db().table(TABLE_NAME).delete().eq("id", item_id).execute()
