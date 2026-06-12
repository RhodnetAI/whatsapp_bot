"""Admin API for the Sales Bot: product catalog CRUD, import from the Information
Bot catalog, Meta Commerce Catalog sync, payment/shipping settings, and orders."""

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile

from app.core.config import settings
from app.core.security import verify_token
from app.db.supabase_client import first_row, supabase, supabase_admin
from app.models.schemas import (
    DeleteKnowledgeResponse,
    SalesCatalogSyncResponse,
    SalesOrderItemOut,
    SalesOrderOut,
    SalesOrdersListResponse,
    SalesPaymentSettingsResponse,
    SalesPaymentSettingsUpdate,
    SalesProductCreate,
    SalesProductSaveResponse,
    SalesProductsListResponse,
    SalesProductsUploadStartResponse,
    SalesProductsUploadStatusResponse,
    SalesProductUpdate,
)
from app.services.sales import catalog, catalog_sync, orders, razorpay_service

logger = logging.getLogger("whatsapp")

router = APIRouter(prefix="/settings/sales", tags=["sales"])

SINGLETON_ID = 1


def _db():
    return supabase_admin if supabase_admin is not None else supabase


# ── Products ─────────────────────────────────────────────────────────────────
@router.get("/products", response_model=SalesProductsListResponse)
async def list_products(token: dict = Depends(verify_token)):
    return SalesProductsListResponse(items=catalog.list_products())


@router.post("/products", response_model=SalesProductSaveResponse)
async def create_product(payload: SalesProductCreate, token: dict = Depends(verify_token)):
    return SalesProductSaveResponse(item=catalog.create_product(payload))


@router.put("/products/{product_id}", response_model=SalesProductSaveResponse)
async def update_product(product_id: str, payload: SalesProductUpdate, token: dict = Depends(verify_token)):
    return SalesProductSaveResponse(item=catalog.update_product(product_id, payload))


@router.delete("/products/{product_id}", response_model=DeleteKnowledgeResponse)
async def delete_product(product_id: str, token: dict = Depends(verify_token)):
    catalog.delete_product(product_id)
    return DeleteKnowledgeResponse(message="Deleted")


@router.post("/products/upload", response_model=SalesProductsUploadStartResponse)
async def upload_products_excel(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    token: dict = Depends(verify_token),
):
    if not (file.filename or "").lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="Please upload an Excel (.xlsx) file")
    job_id, total = await catalog.start_excel_upload(file, background_tasks)
    return SalesProductsUploadStartResponse(job_id=job_id, total=total)


@router.get("/products/upload/{job_id}", response_model=SalesProductsUploadStatusResponse)
async def get_upload_status(job_id: str, token: dict = Depends(verify_token)):
    return catalog.get_upload_job_status(job_id)


# ── Meta Commerce Catalog sync ───────────────────────────────────────────────
@router.post("/catalog/sync", response_model=SalesCatalogSyncResponse)
async def sync_catalog(token: dict = Depends(verify_token)):
    result = await asyncio.to_thread(catalog_sync.sync_all)
    return SalesCatalogSyncResponse(
        synced=result.get("synced", 0),
        failed=result.get("failed", 0),
        items=catalog.list_products(),
    )


# ── Payment / shipping settings ──────────────────────────────────────────────
@router.get("/payments", response_model=SalesPaymentSettingsResponse)
async def get_payment_settings(token: dict = Depends(verify_token)):
    row = first_row(_db().table("sales_bot").select("*").eq("id", SINGLETON_ID).execute()) or {}
    return SalesPaymentSettingsResponse(
        payment_enabled=bool(row.get("payment_enabled")),
        default_currency=row.get("default_currency") or "INR",
        flat_shipping_minor=int(row.get("flat_shipping_minor") or 0),
        free_shipping_threshold_minor=row.get("free_shipping_threshold_minor"),
        razorpay_configured=razorpay_service.is_configured(),
        catalog_configured=catalog_sync.is_configured(),
        checkout_flow_configured=bool(settings.whatsapp_checkout_flow_id),
    )


@router.put("/payments", response_model=SalesPaymentSettingsResponse)
async def update_payment_settings(payload: SalesPaymentSettingsUpdate, token: dict = Depends(verify_token)):
    _db().table("sales_bot").update(
        {
            "default_currency": payload.default_currency or "INR",
            "flat_shipping_minor": int(payload.flat_shipping_minor or 0),
            "free_shipping_threshold_minor": payload.free_shipping_threshold_minor,
        }
    ).eq("id", SINGLETON_ID).execute()
    return await get_payment_settings(token)


# ── Orders (read-only admin view) ────────────────────────────────────────────
@router.get("/orders", response_model=SalesOrdersListResponse)
async def list_orders(token: dict = Depends(verify_token)):
    rows = orders.list_orders()
    result: list[SalesOrderOut] = []
    for order in rows:
        items = [
            SalesOrderItemOut(
                name=i.get("name") or "",
                retailer_id=i.get("retailer_id") or "",
                quantity=int(i.get("quantity") or 0),
                unit_price_minor=int(i.get("unit_price_minor") or 0),
                line_total_minor=int(i.get("line_total_minor") or 0),
            )
            for i in orders.get_order_items(order["id"])
        ]
        address = order.get("shipping_address")
        result.append(
            SalesOrderOut(
                id=str(order.get("id")),
                order_number=order.get("order_number") or "",
                sender=order.get("sender") or "",
                status=order.get("status") or "",
                subtotal_minor=int(order.get("subtotal_minor") or 0),
                shipping_minor=int(order.get("shipping_minor") or 0),
                total_minor=int(order.get("total_minor") or 0),
                currency=order.get("currency") or "INR",
                customer_name=order.get("customer_name") or "",
                customer_phone=order.get("customer_phone") or "",
                shipping_address=address if isinstance(address, dict) else {},
                payment_status=order.get("payment_status") or "created",
                created_at=order.get("created_at"),
                items=items,
            )
        )
    return SalesOrdersListResponse(orders=result)
