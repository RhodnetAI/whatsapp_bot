import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.core.security import verify_token
from app.models.schemas import PushRegisterRequest
from app.services.push_service import register_token, unregister_token


router = APIRouter(tags=["push"])
logger = logging.getLogger("whatsapp")


@router.post("/push/register")
async def push_register(
    body: PushRegisterRequest, auth: dict[str, Any] = Depends(verify_token)
) -> dict[str, str]:
    _ = auth
    if not body.token.strip():
        raise HTTPException(status_code=400, detail="Missing push token")
    try:
        register_token(body.token.strip(), body.platform)
    except Exception as exc:
        logger.exception("Failed to register push token")
        raise HTTPException(status_code=500, detail="Failed to register token") from exc
    return {"status": "registered"}


@router.post("/push/unregister")
async def push_unregister(
    body: PushRegisterRequest, auth: dict[str, Any] = Depends(verify_token)
) -> dict[str, str]:
    _ = auth
    try:
        unregister_token(body.token.strip())
    except Exception as exc:
        logger.exception("Failed to unregister push token")
        raise HTTPException(status_code=500, detail="Failed to unregister token") from exc
    return {"status": "unregistered"}
