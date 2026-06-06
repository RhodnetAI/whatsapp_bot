import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.core.security import verify_token
from app.db.supabase_client import first_row, supabase
from app.models.schemas import BotConfigResponse, BotGreetingRequest, BotInstructionsRequest


router = APIRouter(tags=["setup"])
logger = logging.getLogger("whatsapp")

_SETUP_ID = 1
_SETUP_FIELDS = "bot_name, main_instruction, dos, donts, greeting_message"


def _fetch_setup() -> dict[str, Any]:
    row = first_row(
        supabase.table("service_agent_setup")
        .select(_SETUP_FIELDS)
        .eq("id", _SETUP_ID)
        .limit(1)
        .execute()
    )
    if row is None:
        raise HTTPException(status_code=503, detail="Setup record not found. Run SQL migration 002.")
    return row


@router.get("/setup", response_model=BotConfigResponse)
async def get_setup(auth: dict[str, Any] = Depends(verify_token)) -> BotConfigResponse:
    _ = auth
    row = _fetch_setup()
    return BotConfigResponse(
        bot_name=row.get("bot_name") or "",
        main_instruction=row.get("main_instruction") or "",
        dos=row.get("dos") or "",
        donts=row.get("donts") or "",
        greeting_message=row.get("greeting_message") or "",
    )


@router.put("/setup/instructions", response_model=BotConfigResponse)
async def update_instructions(
    body: BotInstructionsRequest,
    auth: dict[str, Any] = Depends(verify_token),
) -> BotConfigResponse:
    _ = auth

    bot_name = body.bot_name.strip()
    if not bot_name:
        raise HTTPException(status_code=422, detail="bot_name is required")

    main_instruction = body.main_instruction.strip()
    if not main_instruction:
        raise HTTPException(status_code=422, detail="main_instruction is required")

    payload = {
        "bot_name": bot_name[:200],
        "main_instruction": main_instruction[:3000],
        "dos": body.dos.strip()[:1000],
        "donts": body.donts.strip()[:1000],
    }

    try:
        supabase.table("service_agent_setup").update(payload).eq("id", _SETUP_ID).execute()
    except Exception as exc:
        logger.exception("Failed to update bot instructions")
        raise HTTPException(status_code=500, detail="Failed to save instructions") from exc

    row = _fetch_setup()
    return BotConfigResponse(
        bot_name=row.get("bot_name") or "",
        main_instruction=row.get("main_instruction") or "",
        dos=row.get("dos") or "",
        donts=row.get("donts") or "",
        greeting_message=row.get("greeting_message") or "",
    )


@router.put("/setup/greeting", response_model=BotConfigResponse)
async def update_greeting(
    body: BotGreetingRequest,
    auth: dict[str, Any] = Depends(verify_token),
) -> BotConfigResponse:
    _ = auth

    try:
        supabase.table("service_agent_setup").update(
            {"greeting_message": body.greeting_message.strip()}
        ).eq("id", _SETUP_ID).execute()
    except Exception as exc:
        logger.exception("Failed to update greeting message")
        raise HTTPException(status_code=500, detail="Failed to save greeting message") from exc

    row = _fetch_setup()
    return BotConfigResponse(
        bot_name=row.get("bot_name") or "",
        main_instruction=row.get("main_instruction") or "",
        dos=row.get("dos") or "",
        donts=row.get("donts") or "",
        greeting_message=row.get("greeting_message") or "",
    )