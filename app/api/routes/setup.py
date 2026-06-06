import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from app.core.security import verify_token
from app.db.supabase_client import first_row, supabase
from app.models.schemas import (
    BotIdentityUpdate,
    CompanyInfoUpdate,
    FlowConfigUpdate,
    InstructionsUpdate,
    SettingsResponse,
)
from app.services.summarizer import (
    build_raw_instructions,
    generate_summarized_instruction,
    hash_raw_instructions,
)

logger = logging.getLogger("whatsapp")

router = APIRouter(prefix="/settings", tags=["settings"])

SINGLETON_ID = 1


def _get_row() -> dict:
    result = (
        supabase.table("service_agent_setup")
        .select("*")
        .eq("id", SINGLETON_ID)
        .execute()
    )
    row = first_row(result)
    if not row:
        raise HTTPException(
            status_code=404,
            detail="Settings not found. Run SQL migrations first.",
        )
    return row


@router.get("", response_model=SettingsResponse)
async def get_settings(token: dict = Depends(verify_token)):
    row = _get_row()
    return SettingsResponse(
        bot_name=row.get("bot_name"),
        greeting=row.get("greeting"),
        main_instruction=row.get("main_instruction"),
        dos=row.get("dos"),
        donts=row.get("donts"),
        company_address=row.get("company_address"),
        company_phone=row.get("company_phone"),
        company_email=row.get("company_email"),
        social_handles=row.get("social_handles") or [],
        flow_builder=row.get("flow_builder"),
        setup_completed=bool(row.get("setup_completed")),
    )


@router.put("/bot-identity")
async def update_bot_identity(
    payload: BotIdentityUpdate,
    token: dict = Depends(verify_token),
):
    supabase.table("service_agent_setup").update(
        {"bot_name": payload.bot_name, "greeting": payload.greeting}
    ).eq("id", SINGLETON_ID).execute()
    return {"ok": True}


async def _do_summarize(raw_text: str, current_hash: str | None) -> None:
    new_hash = hash_raw_instructions(raw_text)
    if new_hash == current_hash:
        return
    summarized = await generate_summarized_instruction(raw_text)
    supabase.table("service_agent_setup").update(
        {"summarized_instruction": summarized, "instruction_hash": new_hash}
    ).eq("id", SINGLETON_ID).execute()


@router.put("/instructions")
async def update_instructions(
    payload: InstructionsUpdate,
    token: dict = Depends(verify_token),
):
    row = _get_row()

    supabase.table("service_agent_setup").update(
        {
            "main_instruction": payload.main_instruction,
            "dos": payload.dos,
            "donts": payload.donts,
        }
    ).eq("id", SINGLETON_ID).execute()

    raw_text = build_raw_instructions(
        {
            "main_instruction": payload.main_instruction,
            "dos": payload.dos,
            "donts": payload.donts,
        }
    )
    asyncio.create_task(_do_summarize(raw_text, row.get("instruction_hash")))

    return {"ok": True}


@router.put("/company-info")
async def update_company_info(
    payload: CompanyInfoUpdate,
    token: dict = Depends(verify_token),
):
    supabase.table("service_agent_setup").update(
        {
            "company_address": payload.company_address,
            "company_phone": payload.company_phone,
            "company_email": payload.company_email,
            "social_handles": [h.model_dump() for h in payload.social_handles],
        }
    ).eq("id", SINGLETON_ID).execute()
    return {"ok": True}


@router.get("/flow")
async def get_flow(token: dict = Depends(verify_token)):
    row = _get_row()
    return {"config": row.get("flow_builder")}


@router.put("/flow")
async def update_flow(
    payload: FlowConfigUpdate,
    token: dict = Depends(verify_token),
):
    supabase.table("service_agent_setup").update(
        {"flow_builder": payload.config}
    ).eq("id", SINGLETON_ID).execute()
    return {"ok": True}
