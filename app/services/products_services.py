import logging
import re
import uuid
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Literal, cast

from fastapi import BackgroundTasks, HTTPException, UploadFile
from openpyxl import load_workbook

from app.db.supabase_client import first_row, supabase, supabase_admin
from app.models.schemas import (
    ProductServiceCreate,
    ProductServiceItem,
    ProductServiceUpdate,
    ProductsServicesUploadStatusResponse,
)
from app.services.vectorizer import delete_document_vectors, store_product_service_vectors

logger = logging.getLogger("whatsapp")

TABLE_NAME = "information_bot_products_services"

FIELD_NAMES = [
    "name",
    "short_description",
    "full_description",
    "category",
    "price",
    "discount_price",
    "status",
    "images",
    "rating",
    "reviews_count",
    "purchased_count",
]

# Maps normalized Excel column headers to the field they populate.
_COLUMN_ALIASES = {
    "name": "name",
    "short description": "short_description",
    "full description": "full_description",
    "category": "category",
    "price": "price",
    "discount price": "discount_price",
    "status": "status",
    "images": "images",
    "rating": "rating",
    "reviews count": "reviews_count",
    "purchased count": "purchased_count",
}

MAX_EXCEL_ROWS = 500

# In-memory tracking for Excel upload jobs so the frontend can poll real
# per-row progress. This is an admin-only, low-volume settings tool (mirrors
# the module-level client caching already used in vectorizer.py), so a plain
# dict is sufficient — no need for a database-backed job table.
_upload_jobs: dict[str, dict[str, Any]] = {}


def _db():
    """Return the admin client (bypasses RLS) when available, else fall back to anon client."""
    return supabase_admin if supabase_admin is not None else supabase


def _row_to_item(row: dict[str, Any]) -> ProductServiceItem:
    return ProductServiceItem(
        id=str(row.get("id")),
        kind=cast(Literal["product", "service"], row.get("kind") or "product"),
        name=row.get("name") or "",
        short_description=row.get("short_description") or "",
        full_description=row.get("full_description") or "",
        category=row.get("category") or "",
        price=row.get("price") or "",
        discount_price=row.get("discount_price") or "",
        status=row.get("status") or "Active",
        images=row.get("images") or "",
        rating=row.get("rating") or "",
        reviews_count=row.get("reviews_count") or "",
        purchased_count=row.get("purchased_count") or "",
        source=row.get("source") or "manual",
        vectorization_status=row.get("vectorization_status") or "processing",
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def list_items() -> list[ProductServiceItem]:
    result = _db().table(TABLE_NAME).select("*").order("created_at", desc=True).execute()
    rows = getattr(result, "data", None) or []
    return [_row_to_item(row) for row in rows]


def _build_embedding_text(kind: str, fields: dict[str, str]) -> str:
    """Turn the structured fields into a natural-language blob suitable for
    chunking/embedding — this is what the Information Agent's RAG search will
    actually match against and quote from."""
    label = "Product" if kind == "product" else "Service"
    lines = [f"{label} name: {fields.get('name') or 'Untitled'}"]

    if fields.get("category"):
        lines.append(f"Category: {fields['category']}")
    lines.append(f"Availability status: {fields.get('status') or 'Active'}")

    price_bits = []
    if fields.get("price"):
        price_bits.append(f"price {fields['price']}")
    if fields.get("discount_price"):
        price_bits.append(f"discounted price {fields['discount_price']}")
    if price_bits:
        lines.append("Pricing: " + ", ".join(price_bits))

    feedback_bits = []
    if fields.get("rating"):
        feedback_bits.append(f"average rating {fields['rating']}")
    if fields.get("reviews_count"):
        feedback_bits.append(f"{fields['reviews_count']} reviews")
    if fields.get("purchased_count"):
        feedback_bits.append(f"purchased {fields['purchased_count']} times")
    if feedback_bits:
        lines.append("Customer feedback: " + ", ".join(feedback_bits))

    if fields.get("short_description"):
        lines.append(f"Summary: {fields['short_description']}")
    if fields.get("full_description"):
        lines.append(f"Details: {fields['full_description']}")

    return "\n".join(lines)


async def _embed_item(item_id: str, kind: str, fields: dict[str, str]) -> str:
    """Embed the item and upsert its vectors into Qdrant. Returns the resulting vectorization_status."""
    try:
        text = _build_embedding_text(kind, fields)
        await store_product_service_vectors(
            item_id=item_id,
            name=fields.get("name") or "Untitled",
            text=text,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        return "done"
    except Exception:
        logger.exception("Failed to embed product/service %s", item_id)
        return "failed"


async def _create_row(kind: str, fields: dict[str, str], source: str) -> ProductServiceItem:
    item_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "id": item_id,
        "kind": kind,
        **fields,
        "source": source,
        "vectorization_status": "processing",
        "created_at": now,
        "updated_at": now,
    }
    _db().table(TABLE_NAME).insert(row).execute()

    status = await _embed_item(item_id, kind, fields)
    row["vectorization_status"] = status
    _db().table(TABLE_NAME).update({"vectorization_status": status}).eq("id", item_id).execute()

    return _row_to_item(row)


async def create_item(payload: ProductServiceCreate) -> ProductServiceItem:
    fields = payload.model_dump(exclude={"kind"})
    return await _create_row(payload.kind, fields, source="manual")


async def update_item(item_id: str, payload: ProductServiceUpdate) -> ProductServiceItem:
    existing = first_row(_db().table(TABLE_NAME).select("*").eq("id", item_id).execute())
    if existing is None:
        raise HTTPException(status_code=404, detail="Product/service not found")

    fields = payload.model_dump()
    now = datetime.now(timezone.utc).isoformat()
    update_payload = {**fields, "vectorization_status": "processing", "updated_at": now}
    _db().table(TABLE_NAME).update(update_payload).eq("id", item_id).execute()

    try:
        await delete_document_vectors(item_id)
    except Exception:
        logger.warning("Failed to delete previous vectors for product/service %s", item_id)

    status = await _embed_item(item_id, cast(str, existing.get("kind") or "product"), fields)
    _db().table(TABLE_NAME).update({"vectorization_status": status}).eq("id", item_id).execute()

    return _row_to_item({**existing, **update_payload, "vectorization_status": status})


async def delete_item(item_id: str) -> None:
    existing = first_row(_db().table(TABLE_NAME).select("*").eq("id", item_id).execute())
    if existing is None:
        raise HTTPException(status_code=404, detail="Product/service not found")

    try:
        await delete_document_vectors(item_id)
    except Exception:
        logger.warning("Failed to delete vectors for product/service %s", item_id)

    _db().table(TABLE_NAME).delete().eq("id", item_id).execute()


def _normalize_header(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _parse_excel_rows(raw_bytes: bytes) -> list[dict[str, str]]:
    try:
        workbook: Any = load_workbook(BytesIO(raw_bytes), read_only=True, data_only=True)
        if workbook is None:
            raise HTTPException(status_code=400, detail="Could not read Excel file")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read Excel file: {exc}") from exc

    sheet = workbook.active
    rows_iter = sheet.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        raise HTTPException(status_code=400, detail="Excel file is empty")

    column_map: dict[int, str] = {}
    for index, header in enumerate(header_row or []):
        field = _COLUMN_ALIASES.get(_normalize_header(header))
        if field:
            column_map[index] = field

    if "name" not in column_map.values():
        raise HTTPException(status_code=400, detail="Excel file must include a 'Name' column")

    rows: list[dict[str, str]] = []
    for raw_row in rows_iter:
        if raw_row is None or all(cell is None or str(cell).strip() == "" for cell in raw_row):
            continue

        fields = {field_name: "" for field_name in FIELD_NAMES}
        for index, field_name in column_map.items():
            if index < len(raw_row) and raw_row[index] is not None:
                fields[field_name] = str(raw_row[index]).strip()

        if not fields["name"]:
            continue

        rows.append(fields)
        if len(rows) >= MAX_EXCEL_ROWS:
            break

    if not rows:
        raise HTTPException(status_code=400, detail="No usable rows were found in the Excel file")

    return rows


async def start_excel_upload(kind: str, file: UploadFile, background_tasks: BackgroundTasks) -> tuple[str, int]:
    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="The uploaded file is empty")

    rows = _parse_excel_rows(raw_bytes)

    job_id = str(uuid.uuid4())
    _upload_jobs[job_id] = {
        "status": "processing",
        "total": len(rows),
        "processed": 0,
        "items": [],
        "error": None,
    }
    background_tasks.add_task(_process_upload_job, job_id, kind, rows)
    return job_id, len(rows)


async def _process_upload_job(job_id: str, kind: str, rows: list[dict[str, str]]) -> None:
    job = _upload_jobs[job_id]
    try:
        for fields in rows:
            item = await _create_row(kind, fields, source="excel")
            job["items"].append(item)
            job["processed"] += 1
        job["status"] = "done"
    except Exception as exc:
        logger.exception("Excel upload job %s failed", job_id)
        job["status"] = "failed"
        job["error"] = str(exc)


def get_upload_job_status(job_id: str) -> ProductsServicesUploadStatusResponse:
    job = _upload_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Upload job not found")

    return ProductsServicesUploadStatusResponse(
        status=job["status"],
        total=job["total"],
        processed=job["processed"],
        items=list(job["items"]),
        error=job["error"],
    )
