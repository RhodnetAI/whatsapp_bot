import datetime
import logging
import os
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from postgrest.exceptions import APIError

from app.core.security import verify_token
from app.db.supabase_client import first_row, supabase, supabase_admin
from app.services.document_extraction import extract_document_text
from app.services.vectorizer import delete_document_vectors, store_document_vectors


router = APIRouter(tags=["setup"])
logger = logging.getLogger("whatsapp")
SETUP_TABLE = "service_agent_setup"
DOCUMENTS_TABLE = "service_agent_documents"
SETUP_ROW_ID = 1
DOCUMENTS_BUCKET = "service-agent-documents"


class SetupConfigRequest(BaseModel):
    main_instruction: str = Field(default="", max_length=600)
    dos: str = Field(default="", max_length=300)
    donts: str = Field(default="", max_length=300)


class SetupFlowBuilderRequest(BaseModel):
    enabled: bool = Field(default=False)
    state: dict[str, Any] | None = None


class SetupDocumentResponse(BaseModel):
    id: str
    file_name: str
    original_name: str
    mime_type: str | None = None
    char_count: int
    created_at: str


class SetupStatusResponse(BaseModel):
    setup_completed: bool
    setup_table_ready: bool = True
    configuration: dict[str, str]
    documents: list[dict[str, Any]]
    flow_builder: dict[str, Any]



def _default_configuration() -> dict[str, str]:
    return {
        "main_instruction": "",
        "dos": "",
        "donts": "",
    }


def _default_flow_builder() -> dict[str, Any]:
    return {
        "enabled": False,
        "state": None,
        "placeholder": "Drag blocks to build your setup flow",
    }


def _default_status(setup_table_ready: bool = True) -> SetupStatusResponse:
    return SetupStatusResponse(
        setup_completed=False,
        setup_table_ready=setup_table_ready,
        configuration=_default_configuration(),
        documents=[],
        flow_builder=_default_flow_builder(),
    )



def _is_missing_table_error(exc: Exception) -> bool:
    if not isinstance(exc, APIError):
        return False
    payload = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {}
    code = payload.get("code")
    # PGRST204 = missing column, PGRST205 = missing table
    return code in ("PGRST204", "PGRST205")


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0



def _load_setup_row() -> dict[str, Any] | None:
    result = (
        supabase.table(SETUP_TABLE)
        .select("*")
        .eq("id", SETUP_ROW_ID)
        .limit(1)
        .execute()
    )
    return first_row(result)



def _upsert_setup_row(values: dict[str, Any]) -> dict[str, Any]:
    payload = {"id": SETUP_ROW_ID, **values}
    result = supabase.table(SETUP_TABLE).upsert(payload).execute()
    row = first_row(result)
    if row is None:
        raise HTTPException(status_code=500, detail="Failed to save setup configuration")
    return row



def _load_documents() -> list[dict[str, Any]]:
    result = (
        supabase.table(DOCUMENTS_TABLE)
        .select("*")
        .eq("setup_id", SETUP_ROW_ID)
        .order("created_at", desc=True)
        .execute()
    )
    documents: list[dict[str, Any]] = []
    for item in result.data or []:
        if not isinstance(item, dict):
            continue

        documents.append(
            {
                "id": str(item.get("id")),
                "file_name": item.get("file_name", ""),
                "original_name": item.get("original_name", ""),
                "mime_type": item.get("mime_type"),
                "char_count": _coerce_int(item.get("char_count")),
                "created_at": item.get("created_at", ""),
            }
        )
    return documents


@router.get("/setup/status", response_model=SetupStatusResponse)
def get_setup_status(auth: dict[str, Any] = Depends(verify_token)) -> SetupStatusResponse:
    _ = auth
    try:
        setup_row = _load_setup_row()
        documents = _load_documents()
    except Exception as exc:
        if _is_missing_table_error(exc):
            return _default_status(setup_table_ready=False)
        raise

    if setup_row is None:
        return _default_status()

    return SetupStatusResponse(
        setup_completed=bool(setup_row.get("setup_completed", False)),
        setup_table_ready=True,
        configuration={
            "main_instruction": setup_row.get("main_instruction") or "",
            "dos": setup_row.get("dos") or "",
            "donts": setup_row.get("donts") or "",
        },
        documents=documents,
        flow_builder=setup_row.get("flow_builder") or _default_flow_builder(),
    )


@router.post("/setup/config", response_model=SetupStatusResponse)
@router.put("/setup/config", response_model=SetupStatusResponse)
def save_setup_config(
    payload: SetupConfigRequest, auth: dict[str, Any] = Depends(verify_token)
) -> SetupStatusResponse:
    _ = auth
    try:
        row = _upsert_setup_row(
            {
                "main_instruction": payload.main_instruction.strip(),
                "dos": payload.dos.strip(),
                "donts": payload.donts.strip(),
                "setup_completed": False,
                "updated_at": datetime.datetime.utcnow().isoformat(),
            }
        )
        documents = _load_documents()
    except Exception as exc:
        if _is_missing_table_error(exc):
            raise HTTPException(
                status_code=503,
                detail=f"Missing setup table '{SETUP_TABLE}'. Run backend/sql/002_service_agent_setup.sql in Supabase SQL Editor.",
            ) from exc
        raise

    return SetupStatusResponse(
        setup_completed=bool(row.get("setup_completed", False)),
        setup_table_ready=True,
        configuration={
            "main_instruction": row.get("main_instruction") or "",
            "dos": row.get("dos") or "",
            "donts": row.get("donts") or "",
        },
        documents=documents,
        flow_builder=row.get("flow_builder") or _default_flow_builder(),
    )


@router.post("/setup/flow-builder", response_model=SetupStatusResponse)
def save_flow_builder(
    payload: SetupFlowBuilderRequest, auth: dict[str, Any] = Depends(verify_token)
) -> SetupStatusResponse:
    _ = auth
    try:
        row = _upsert_setup_row(
            {
                "flow_builder": {
                    "enabled": payload.enabled,
                    "state": payload.state,
                    "placeholder": "Flow Builder saved",
                },
                "setup_completed": False,
                "updated_at": datetime.datetime.utcnow().isoformat(),
            }
        )
        documents = _load_documents()
    except Exception as exc:
        if _is_missing_table_error(exc):
            raise HTTPException(
                status_code=503,
                detail=f"Missing setup table '{SETUP_TABLE}'. Run backend/sql/002_service_agent_setup.sql in Supabase SQL Editor.",
            ) from exc
        raise

    return SetupStatusResponse(
        setup_completed=bool(row.get("setup_completed", False)),
        setup_table_ready=True,
        configuration={
            "main_instruction": row.get("main_instruction") or "",
            "dos": row.get("dos") or "",
            "donts": row.get("donts") or "",
        },
        documents=documents,
        flow_builder=row.get("flow_builder") or _default_flow_builder(),
    )


@router.post("/setup/documents", response_model=SetupDocumentResponse)
async def upload_setup_document(
    document: UploadFile = File(...), auth: dict[str, Any] = Depends(verify_token)
) -> SetupDocumentResponse:
    _ = auth
    try:
        if not document.filename:
            raise HTTPException(status_code=400, detail="No filename provided")
        
        logger.info(f"Starting document upload: {document.filename}")
        
        # Read the file content first
        file_content = await document.read()
        if not file_content:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")
        
        # Create a new UploadFile for extraction with the same content
        import io
        extract_file = UploadFile(
            file=io.BytesIO(file_content),
            size=len(file_content),
            filename=document.filename
        )
        
        # Extract text from the document (only for char_count)
        text, original_name = await extract_document_text(extract_file)
        logger.info(f"Extracted {len(text)} characters from {original_name}")
        
        # Upload file to Supabase bucket (use admin client to bypass RLS)
        stored_name = f"{str(uuid4())[:8]}_{original_name}"
        bucket_path = f"setup-docs/{SETUP_ROW_ID}/{stored_name}"
        
        # Determine which client to use for storage
        storage_client = supabase_admin if supabase_admin else supabase
        
        try:
            logger.info(f"Uploading document to bucket: {bucket_path} (size: {len(file_content)} bytes)")
            storage_client.storage.from_(DOCUMENTS_BUCKET).upload(
                path=bucket_path,
                file=file_content,
                file_options={"content-type": document.content_type or "application/octet-stream"}
            )
            logger.info(f"Successfully uploaded document to bucket: {bucket_path}")
        except Exception as bucket_exc:
            logger.error(f"Failed to upload document to bucket: {bucket_exc}")
            raise HTTPException(status_code=500, detail=f"Failed to upload document to storage: {str(bucket_exc)}")
        
        row_payload = {
            "setup_id": SETUP_ROW_ID,
            "file_name": stored_name,
            "original_name": original_name,
            "mime_type": document.content_type,
            "bucket_path": bucket_path,
            "char_count": len(text),
            "created_at": datetime.datetime.utcnow().isoformat(),
            "updated_at": datetime.datetime.utcnow().isoformat(),
        }
        
        logger.info(f"Storing document in database: {original_name}")
        result = supabase.table(DOCUMENTS_TABLE).insert(row_payload).execute()
        row = first_row(result)

        if row is None:
            raise HTTPException(status_code=500, detail="Failed to store document in database")

        logger.info(f"Document stored successfully: {row.get('id')}")

        try:
            await store_document_vectors(
                document_id=str(row.get("id")),
                original_name=original_name,
                bucket_path=bucket_path,
                text=text,
                created_at=row_payload["created_at"],
            )
        except Exception as vector_exc:
            logger.warning(
                "Document uploaded but local vector store update failed: %s",
                str(vector_exc),
            )
        
        return SetupDocumentResponse(
            id=str(row.get("id")),
            file_name=row.get("file_name") or original_name,
            original_name=row.get("original_name") or original_name,
            mime_type=row.get("mime_type"),
            char_count=_coerce_int(row.get("char_count") or len(text)),
            created_at=row.get("created_at") or datetime.datetime.utcnow().isoformat(),
        )
    except HTTPException:
        raise
    except Exception as exc:
        if _is_missing_table_error(exc):
            raise HTTPException(
                status_code=503,
                detail=f"Missing documents table '{DOCUMENTS_TABLE}'. Run backend/sql/002_service_agent_setup.sql in Supabase SQL Editor.",
            ) from exc
        logger.error(f"Error in upload_setup_document: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(exc)}")


@router.delete("/setup/documents/{document_id}")
async def delete_setup_document(document_id: str, auth: dict[str, Any] = Depends(verify_token)) -> dict[str, str]:
    _ = auth
    try:
        UUID(document_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid document id") from exc

    try:
        # Get document first to retrieve bucket_path
        result = supabase.table(DOCUMENTS_TABLE).select("bucket_path").eq("id", document_id).limit(1).execute()
        doc = first_row(result)

        # Delete vectors first so the document cannot keep resurfacing in search.
        try:
            await delete_document_vectors(document_id)
        except Exception as vector_exc:
            logger.warning(f"Failed to delete document vectors: {vector_exc}")
        
        # Delete from bucket if bucket_path exists (use admin client)
        if doc and doc.get("bucket_path"):
            try:
                storage_client = supabase_admin if supabase_admin else supabase
                storage_client.storage.from_(DOCUMENTS_BUCKET).remove([doc["bucket_path"]])
            except Exception as bucket_exc:
                logger.warning(f"Failed to delete document from bucket: {bucket_exc}")
        
        # Delete database record
        supabase.table(DOCUMENTS_TABLE).delete().eq("id", document_id).execute()
    except Exception as exc:
        if _is_missing_table_error(exc):
            raise HTTPException(
                status_code=503,
                detail=f"Missing documents table '{DOCUMENTS_TABLE}'. Run backend/sql/002_service_agent_setup.sql in Supabase SQL Editor.",
            ) from exc
        raise

    return {"status": "deleted"}


@router.post("/setup/complete", response_model=SetupStatusResponse)
def complete_setup(auth: dict[str, Any] = Depends(verify_token)) -> SetupStatusResponse:
    _ = auth
    try:
        row = _upsert_setup_row(
            {
                "setup_completed": True,
                "updated_at": datetime.datetime.utcnow().isoformat(),
            }
        )
        documents = _load_documents()
    except Exception as exc:
        if _is_missing_table_error(exc):
            raise HTTPException(
                status_code=503,
                detail=f"Missing setup table '{SETUP_TABLE}'. Run backend/sql/002_service_agent_setup.sql in Supabase SQL Editor.",
            ) from exc
        raise

    return SetupStatusResponse(
        setup_completed=bool(row.get("setup_completed", False)),
        setup_table_ready=True,
        configuration={
            "main_instruction": row.get("main_instruction") or "",
            "dos": row.get("dos") or "",
            "donts": row.get("donts") or "",
        },
        documents=documents,
        flow_builder=row.get("flow_builder") or _default_flow_builder(),
    )
