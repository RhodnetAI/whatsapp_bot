from fastapi import APIRouter, Depends

from app.core.security import verify_token
from app.models.schemas import (
    SchedulerCreate,
    SchedulerListResponse,
    SchedulerSaveResponse,
    SchedulerUpdate,
)
from app.services.scheduler import create_item, delete_item, list_items, update_item

router = APIRouter(prefix="/settings/scheduler", tags=["scheduler"])


@router.get("", response_model=SchedulerListResponse)
async def get_scheduler(token: dict = Depends(verify_token)):
    return SchedulerListResponse(items=list_items())


@router.post("", response_model=SchedulerSaveResponse)
async def create_scheduler(payload: SchedulerCreate, token: dict = Depends(verify_token)):
    item = await create_item(payload)
    return SchedulerSaveResponse(item=item)


@router.put("/{item_id}", response_model=SchedulerSaveResponse)
async def update_scheduler(
    item_id: str,
    payload: SchedulerUpdate,
    token: dict = Depends(verify_token),
):
    item = await update_item(item_id, payload)
    return SchedulerSaveResponse(item=item)


@router.delete("/{item_id}")
async def delete_scheduler(item_id: str, token: dict = Depends(verify_token)):
    await delete_item(item_id)
    return {"status": "ok"}
