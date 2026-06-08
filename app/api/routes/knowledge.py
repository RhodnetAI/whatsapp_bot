from fastapi import APIRouter, BackgroundTasks, Depends, File, UploadFile

from app.core.security import verify_token
from app.models.schemas import (
    DeleteKnowledgeResponse,
    KnowledgeFilesResponse,
    UploadKnowledgeResponse,
)
from app.services.knowledge_documents import delete_document, list_documents, upload_documents

router = APIRouter(prefix="/settings/knowledge", tags=["knowledge"])


@router.get("/files", response_model=KnowledgeFilesResponse)
async def get_files(token: dict = Depends(verify_token)):
    return KnowledgeFilesResponse(files=list_documents())


@router.post("/upload", response_model=UploadKnowledgeResponse)
async def upload_files(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    token: dict = Depends(verify_token),
):
    uploaded = await upload_documents(files, background_tasks)
    return UploadKnowledgeResponse(message="Files uploaded. Processing started.", files=uploaded)


@router.delete("/files/{document_id}", response_model=DeleteKnowledgeResponse)
async def delete_file(document_id: str, token: dict = Depends(verify_token)):
    await delete_document(document_id)
    return DeleteKnowledgeResponse(message="Document deleted")
