import logging
import os
import re
import uuid
from datetime import datetime, timezone

from fastapi import BackgroundTasks, HTTPException, UploadFile

from app.db.supabase_client import first_row, supabase, supabase_admin
from app.models.schemas import KnowledgeDocument
from app.services.document_extraction import extract_document_text
from app.services.vectorizer import delete_document_vectors, store_document_vectors

logger = logging.getLogger("whatsapp")

BUCKET_NAME = "service-agent-documents"
TABLE_NAME = "information_bot"
SINGLETON_ID = 1

_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9\-_.]+")


def _db():
    """Return the admin client (bypasses RLS) when available, else fall back to anon client."""
    return supabase_admin if supabase_admin is not None else supabase


def _sanitize_file_name(name: str) -> str:
    base = os.path.basename((name or "").strip()) or "document"
    stem, ext = os.path.splitext(base)
    safe_stem = _UNSAFE_CHARS_RE.sub("_", stem).strip("._") or "document"
    safe_ext = _UNSAFE_CHARS_RE.sub("", ext.lower())
    return f"{safe_stem}{safe_ext}"


def _row_to_document(entry: dict) -> KnowledgeDocument:
    return KnowledgeDocument(
        id=str(entry.get("id")),
        name=entry.get("original_name") or entry.get("file_name") or "document",
        size=int(entry.get("file_size") or 0),
        character_count=int(entry.get("char_count") or 0),
        status=entry.get("vectorization_status") or "pending",
        created_at=entry.get("created_at"),
    )


def _get_documents() -> list[dict]:
    result = (
        _db().table(TABLE_NAME)
        .select("knowledge_documents")
        .eq("id", SINGLETON_ID)
        .execute()
    )
    row = first_row(result)
    return list((row or {}).get("knowledge_documents") or [])


def _save_documents(documents: list[dict]) -> None:
    _db().table(TABLE_NAME).update({"knowledge_documents": documents}).eq("id", SINGLETON_ID).execute()


def _set_document_status(document_id: str, status: str) -> None:
    documents = _get_documents()
    for entry in documents:
        if str(entry.get("id")) == document_id:
            entry["vectorization_status"] = status
            break
    _save_documents(documents)


def list_documents() -> list[KnowledgeDocument]:
    documents = sorted(_get_documents(), key=lambda entry: entry.get("created_at") or "", reverse=True)
    return [_row_to_document(entry) for entry in documents]


async def _process_document(
    document_id: str,
    original_name: str,
    bucket_path: str,
    text: str,
    created_at: str,
) -> None:
    try:
        await store_document_vectors(document_id, original_name, bucket_path, text, created_at)
        _set_document_status(document_id, "done")
    except Exception:
        logger.exception("Failed to vectorize knowledge document %s", document_id)
        _set_document_status(document_id, "failed")


async def upload_documents(files: list[UploadFile], background_tasks: BackgroundTasks) -> list[KnowledgeDocument]:
    if not files:
        raise HTTPException(status_code=400, detail="No files were provided")

    documents = _get_documents()
    created: list[KnowledgeDocument] = []

    for file in files:
        text, original_name = await extract_document_text(file)
        await file.seek(0)
        raw_bytes = await file.read()

        document_id = str(uuid.uuid4())
        safe_name = _sanitize_file_name(original_name)
        bucket_path = f"{document_id}/{safe_name}"
        content_type = file.content_type or "application/octet-stream"

        try:
            _db().storage.from_(BUCKET_NAME).upload(
                bucket_path,
                raw_bytes,
                file_options={"content-type": content_type, "upsert": "true"},
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to store '{original_name}': {exc}") from exc

        created_at = datetime.now(timezone.utc).isoformat()
        entry = {
            "id": document_id,
            "file_name": safe_name,
            "original_name": original_name,
            "mime_type": content_type,
            "bucket_path": bucket_path,
            "char_count": len(text),
            "file_size": len(raw_bytes),
            "vectorization_status": "processing",
            "created_at": created_at,
        }
        documents.insert(0, entry)
        background_tasks.add_task(_process_document, document_id, original_name, bucket_path, text, created_at)
        created.append(_row_to_document(entry))

    _save_documents(documents)
    return created


async def delete_document(document_id: str) -> None:
    documents = _get_documents()
    entry = next((item for item in documents if str(item.get("id")) == document_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="Document not found")

    bucket_path = entry.get("bucket_path")
    if bucket_path:
        try:
            _db().storage.from_(BUCKET_NAME).remove([bucket_path])
        except Exception:
            logger.warning("Failed to remove storage object %s for document %s", bucket_path, document_id)

    try:
        await delete_document_vectors(document_id)
    except Exception:
        logger.warning("Failed to delete vectors for document %s", document_id)

    remaining = [item for item in documents if str(item.get("id")) != document_id]
    _save_documents(remaining)
