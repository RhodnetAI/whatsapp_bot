from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile

from app.core.security import verify_token
from app.models.schemas import (
    DeleteKnowledgeResponse,
    ProductServiceCreate,
    ProductServiceSaveResponse,
    ProductServiceUpdate,
    ProductsServicesListResponse,
    ProductsServicesReindexResponse,
    ProductsServicesUploadStartResponse,
    ProductsServicesUploadStatusResponse,
)
from app.services.products_services import (
    create_item,
    delete_item,
    get_upload_job_status,
    list_items,
    reindex_all,
    start_excel_upload,
    update_item,
)

router = APIRouter(prefix="/settings/products-services", tags=["products-services"])


@router.get("", response_model=ProductsServicesListResponse)
async def get_items(token: dict = Depends(verify_token)):
    return ProductsServicesListResponse(items=list_items())


@router.post("", response_model=ProductServiceSaveResponse)
async def create_product_or_service(
    payload: ProductServiceCreate,
    background_tasks: BackgroundTasks,
    token: dict = Depends(verify_token),
):
    item = await create_item(payload, background_tasks)
    return ProductServiceSaveResponse(item=item)


@router.put("/{item_id}", response_model=ProductServiceSaveResponse)
async def update_product_or_service(
    item_id: str,
    payload: ProductServiceUpdate,
    background_tasks: BackgroundTasks,
    token: dict = Depends(verify_token),
):
    item = await update_item(item_id, payload, background_tasks)
    return ProductServiceSaveResponse(item=item)


@router.post("/reindex", response_model=ProductsServicesReindexResponse)
async def reindex_products_services(background_tasks: BackgroundTasks, token: dict = Depends(verify_token)):
    """Backfill vectors for all existing rows — run once after deploying
    semantic retrieval so previously-created items become searchable."""
    total = await reindex_all(background_tasks)
    return ProductsServicesReindexResponse(message="Reindexing started", total=total)


@router.delete("/{item_id}", response_model=DeleteKnowledgeResponse)
async def delete_product_or_service(item_id: str, token: dict = Depends(verify_token)):
    await delete_item(item_id)
    return DeleteKnowledgeResponse(message="Deleted")


@router.post("/upload", response_model=ProductsServicesUploadStartResponse)
async def upload_excel(
    background_tasks: BackgroundTasks,
    kind: Literal["product", "service"] = Form(...),
    file: UploadFile = File(...),
    token: dict = Depends(verify_token),
):
    if not (file.filename or "").lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="Please upload an Excel (.xlsx) file")

    job_id, total = await start_excel_upload(kind, file, background_tasks)
    return ProductsServicesUploadStartResponse(job_id=job_id, total=total)


@router.get("/upload/{job_id}", response_model=ProductsServicesUploadStatusResponse)
async def get_upload_status(job_id: str, token: dict = Depends(verify_token)):
    return get_upload_job_status(job_id)
