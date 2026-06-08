import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db.supabase_client import first_row, supabase, supabase_admin
from app.models.schemas import SchedulerCreate, SchedulerItem, SchedulerUpdate
from app.services.vectorizer import delete_document_vectors, store_scheduler_vectors

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


def _build_embedding_text(item_dict: dict) -> str:
    """Convert scheduler entry to natural-language text suitable for chunking/embedding.
    This text is what the Information Agent's RAG search will match against."""
    lines = []
    
    day = item_dict.get("day_of_week", "")
    time_start = item_dict.get("time_start", "09:00")
    time_end = item_dict.get("time_end", "17:00")
    exclude_start = item_dict.get("exclude_time_start", "")
    exclude_end = item_dict.get("exclude_time_end", "")
    is_special = item_dict.get("is_special_time", False)
    special_date = item_dict.get("special_date", "")
    
    if is_special:
        label = f"Special hours on {special_date}"
    else:
        label = f"Business hours on {day}"
        if special_date:
            label = f"{label} ({special_date})"
    
    lines.append(f"{label}: {time_start} to {time_end}")
    
    if exclude_start and exclude_end:
        lines.append(f"Excluding {exclude_start} to {exclude_end}")
    
    return "\n".join(lines)


async def _embed_item(item_id: str, item_dict: dict) -> str:
    """Embed the scheduler entry and upsert into Qdrant."""
    try:
        text = _build_embedding_text(item_dict)
        await store_scheduler_vectors(
            item_id=item_id,
            text=text,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        return "done"
    except Exception:
        logger.exception("Failed to embed scheduler entry %s", item_id)
        return "failed"


async def _create_row(fields: dict) -> SchedulerItem:
    item_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "id": item_id,
        **fields,
        "source": "manual",
        "vectorization_status": "processing",
        "created_at": now,
        "updated_at": now,
    }
    _db().table(TABLE_NAME).insert(row).execute()
    
    status = await _embed_item(item_id, row)
    row["vectorization_status"] = status
    _db().table(TABLE_NAME).update({"vectorization_status": status}).eq("id", item_id).execute()
    
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
    update_payload = {**fields, "vectorization_status": "processing", "updated_at": now}
    _db().table(TABLE_NAME).update(update_payload).eq("id", item_id).execute()
    
    try:
        await delete_document_vectors(item_id)
    except Exception:
        logger.warning("Failed to delete previous vectors for scheduler entry %s", item_id)
    
    status = await _embed_item(item_id, {**existing, **update_payload})
    _db().table(TABLE_NAME).update({"vectorization_status": status}).eq("id", item_id).execute()
    
    return _row_to_item({**existing, **update_payload, "vectorization_status": status})


async def delete_item(item_id: str) -> None:
    existing = first_row(_db().table(TABLE_NAME).select("*").eq("id", item_id).execute())
    if existing is None:
        raise HTTPException(status_code=404, detail="Scheduler entry not found")
    
    try:
        await delete_document_vectors(item_id)
    except Exception:
        logger.warning("Failed to delete vectors for scheduler entry %s", item_id)
    
    _db().table(TABLE_NAME).delete().eq("id", item_id).execute()
