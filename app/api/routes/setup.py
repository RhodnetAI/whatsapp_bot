import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from app.core.security import verify_token
from app.db.supabase_client import first_row, supabase, supabase_admin
from app.models.schemas import (
    BotIdentityUpdate,
    BotSelectRequest,
    CompanyInfoUpdate,
    ConversationHistoryUpdate,
    EnhancedRetrievalUpdate,
    FlowConfigUpdate,
    InstructionsUpdate,
    SectionToggleRequest,
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
VALID_BOTS = {"information_bot", "sales_bot"}


def _db():
    """Return the admin client (bypasses RLS) when available, else fall back to anon client."""
    return supabase_admin if supabase_admin is not None else supabase

# Section columns that exist per table
_INFO_BOT_SECTION_COLS = {
    "company_info": "company_info_enabled",
    "flow_creation": "flow_creation_enabled",
    "knowledge": "knowledge_enabled",
    "products_services": "products_services_enabled",
    "scheduler": "scheduler_enabled",
}
_SALES_BOT_SECTION_COLS = {
    "company_info": "company_info_enabled",
    "sales_products": "sales_products_enabled",
    "sales_payments": "payment_enabled",
}


def _ensure_row(table: str) -> None:
    """Upsert the singleton row so it always exists."""
    _db().table(table).upsert(
        {"id": SINGLETON_ID, "is_selected": table == "information_bot"},
        on_conflict="id",
    ).execute()


def _get_active_bot_table() -> str:
    """Return the table name of the currently selected bot."""
    info_row = first_row(
        _db().table("information_bot").select("is_selected").eq("id", SINGLETON_ID).execute()
    )
    if info_row is not None:
        return "information_bot" if info_row.get("is_selected") else "sales_bot"
    # Row missing — initialise both tables and default to information_bot
    _ensure_row("information_bot")
    _ensure_row("sales_bot")
    return "information_bot"


def _get_row(table: str) -> dict:
    result = (
        _db().table(table)
        .select("*")
        .eq("id", SINGLETON_ID)
        .execute()
    )
    row = first_row(result)
    if not row:
        _ensure_row(table)
        result = _db().table(table).select("*").eq("id", SINGLETON_ID).execute()
        row = first_row(result)
    if not row:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to initialise {table} settings row.",
        )
    return row


def _section_cols_for(table: str) -> dict[str, str]:
    return _INFO_BOT_SECTION_COLS if table == "information_bot" else _SALES_BOT_SECTION_COLS


@router.get("", response_model=SettingsResponse)
async def get_settings(token: dict = Depends(verify_token)):
    table = _get_active_bot_table()
    row = _get_row(table)
    section_cols = _section_cols_for(table)
    section_states = {key: bool(row.get(col, False)) for key, col in section_cols.items()}

    flow = row.get("flow_builder") if table == "information_bot" else None
    if not flow or not flow.get("questions"):
        flow = None

    enhanced_retrieval_enabled = (
        bool(row.get("enhanced_retrieval_enabled")) if table == "information_bot" else False
    )
    conversation_history_enabled = bool(row.get("conversation_history_enabled", True))

    return SettingsResponse(
        active_bot=table,
        bot_name=row.get("bot_name"),
        greeting=row.get("greeting"),
        main_instruction=row.get("main_instruction"),
        dos=row.get("dos"),
        donts=row.get("donts"),
        company_address=row.get("company_address"),
        company_phone=row.get("company_phone"),
        company_email=row.get("company_email"),
        social_handles=row.get("social_handles") or [],
        flow_builder=flow,
        section_states=section_states,
        enhanced_retrieval_enabled=enhanced_retrieval_enabled,
        conversation_history_enabled=conversation_history_enabled,
        setup_completed=True,
    )


@router.post("/select-bot")
async def select_bot(
    payload: BotSelectRequest,
    token: dict = Depends(verify_token),
):
    if payload.bot not in VALID_BOTS:
        raise HTTPException(status_code=400, detail=f"Invalid bot. Must be one of: {VALID_BOTS}")

    other = "sales_bot" if payload.bot == "information_bot" else "information_bot"
    _db().table(payload.bot).upsert(
        {"id": SINGLETON_ID, "is_selected": True}, on_conflict="id"
    ).execute()
    _db().table(other).upsert(
        {"id": SINGLETON_ID, "is_selected": False}, on_conflict="id"
    ).execute()
    return {"ok": True}


@router.put("/bot-identity")
async def update_bot_identity(
    payload: BotIdentityUpdate,
    token: dict = Depends(verify_token),
):
    table = _get_active_bot_table()
    _db().table(table).update(
        {"bot_name": payload.bot_name, "greeting": payload.greeting}
    ).eq("id", SINGLETON_ID).execute()
    return {"ok": True}


async def _do_summarize(table: str, raw_text: str, current_hash: str | None) -> None:
    new_hash = hash_raw_instructions(raw_text)
    if new_hash == current_hash:
        logger.info("[summarize] %s: instructions unchanged (hash=%s), skipping", table, new_hash)
        return

    logger.info(
        "[summarize] %s: instructions changed (old_hash=%s, new_hash=%s), starting summarization",
        table, current_hash, new_hash,
    )
    summarized = await generate_summarized_instruction(raw_text)
    logger.info(
        "[summarize] %s: summarization complete (raw_chars=%d, summary_chars=%d)",
        table, len(raw_text), len(summarized),
    )

    _db().table(table).update(
        {"summarized_instruction": summarized, "instruction_hash": new_hash}
    ).eq("id", SINGLETON_ID).execute()
    logger.info("[summarize] %s: stored summarized_instruction and instruction_hash", table)


@router.put("/instructions")
async def update_instructions(
    payload: InstructionsUpdate,
    token: dict = Depends(verify_token),
):
    table = _get_active_bot_table()
    row = _get_row(table)

    logger.info("[summarize] %s: saving instructions, dos and donts", table)
    _db().table(table).update(
        {
            "main_instruction": payload.main_instruction,
            "dos": payload.dos,
            "donts": payload.donts,
        }
    ).eq("id", SINGLETON_ID).execute()

    raw_text = build_raw_instructions(
        {
            "bot_name": row.get("bot_name"),
            "main_instruction": payload.main_instruction,
            "dos": payload.dos,
            "donts": payload.donts,
        }
    )
    logger.info("[summarize] %s: queuing background summarization task (raw_chars=%d)", table, len(raw_text))
    asyncio.create_task(_do_summarize(table, raw_text, row.get("instruction_hash")))

    return {"ok": True}


@router.put("/company-info")
async def update_company_info(
    payload: CompanyInfoUpdate,
    token: dict = Depends(verify_token),
):
    table = _get_active_bot_table()
    _db().table(table).update(
        {
            "company_address": payload.company_address,
            "company_phone": payload.company_phone,
            "company_email": payload.company_email,
            "social_handles": [h.model_dump() for h in payload.social_handles],
        }
    ).eq("id", SINGLETON_ID).execute()
    return {"ok": True}


@router.put("/sections")
async def update_section(
    payload: SectionToggleRequest,
    token: dict = Depends(verify_token),
):
    table = _get_active_bot_table()
    section_cols = _section_cols_for(table)
    col = section_cols.get(payload.section)
    if not col:
        raise HTTPException(
            status_code=400,
            detail=f"Section '{payload.section}' not valid for active bot.",
        )

    # Scheduler and Flow Creation are mutually exclusive on information_bot
    if table == "information_bot" and payload.enabled:
        row = _get_row(table)
        if payload.section == "scheduler" and row.get("flow_creation_enabled"):
            raise HTTPException(
                status_code=409,
                detail="Please disable Flow Creation before enabling the Scheduler.",
            )
        if payload.section == "flow_creation" and row.get("scheduler_enabled"):
            raise HTTPException(
                status_code=409,
                detail="Please disable the Scheduler before enabling Flow Creation.",
            )

    _db().table(table).update({col: payload.enabled}).eq("id", SINGLETON_ID).execute()
    return {"ok": True}


@router.get("/flow")
async def get_flow(token: dict = Depends(verify_token)):
    row = _get_row("information_bot")
    return {"config": row.get("flow_builder")}


@router.put("/flow")
async def update_flow(
    payload: FlowConfigUpdate,
    token: dict = Depends(verify_token),
):
    config_dict = payload.config.model_dump()
    flow_has_questions = len(payload.config.questions) > 0
    _db().table("information_bot").update(
        {
            "flow_builder": config_dict,
            "flow_creation_enabled": flow_has_questions,
        }
    ).eq("id", SINGLETON_ID).execute()
    return {"ok": True}


@router.put("/knowledge/enhanced-retrieval")
async def update_enhanced_retrieval(
    payload: EnhancedRetrievalUpdate,
    token: dict = Depends(verify_token),
):
    _db().table("information_bot").update(
        {"enhanced_retrieval_enabled": payload.enabled}
    ).eq("id", SINGLETON_ID).execute()
    return {"ok": True}


@router.put("/conversation-history")
async def update_conversation_history(
    payload: ConversationHistoryUpdate,
    token: dict = Depends(verify_token),
):
    table = _get_active_bot_table()
    _db().table(table).update(
        {"conversation_history_enabled": payload.enabled}
    ).eq("id", SINGLETON_ID).execute()
    return {"ok": True}
