import datetime
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from postgrest.exceptions import APIError

from app.core.security import verify_token
from app.db.supabase_client import first_row, supabase, supabase_admin
from app.models.schemas import (
    RenameClientRequest,
    SenderActionRequest,
    SendMessageRequest,
    ToggleClientRequest,
)
from app.services.knowledge import answer_query_from_knowledge
from app.services.whatsapp import send_whatsapp_text


router = APIRouter(tags=["clients"])
logger = logging.getLogger("whatsapp")


def _conversation_client() -> Any:
    return supabase_admin if supabase_admin is not None else supabase


def _load_setup_configuration() -> dict[str, str]:
    setup_row = first_row(
        supabase.table("service_agent_setup").select("*").eq("id", 1).limit(1).execute()
    )
    return {
        "main_instruction": str(setup_row.get("main_instruction") or "") if setup_row else "",
        "dos": str(setup_row.get("dos") or "") if setup_row else "",
        "donts": str(setup_row.get("donts") or "") if setup_row else "",
    }


@router.get("/clients")
async def get_clients(auth: dict[str, Any] = Depends(verify_token)) -> dict[str, list[dict[str, Any]]]:
    _ = auth

    try:
        db_client = _conversation_client()
        data = (
            db_client.table("whatsapp_conversations")
            .select("sender, client_name, unread, bookmarked, blocked, updated_at, conversation_date, lead_label")
            .order("updated_at", desc=True)
            .execute()
        )
    except APIError as exc:
        err = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {}
        if err.get("code") == "PGRST205":
            raise HTTPException(
                status_code=503,
                detail=(
                    "Missing Supabase table 'whatsapp_conversations'. "
                    "Run backend/sql/001_create_whatsapp_conversations.sql in Supabase SQL Editor."
                ),
            ) from exc
        raise

    clients: list[dict[str, Any]] = []
    seen: set[str] = set()

    for d in data.data or []:
        if not isinstance(d, dict):
            continue

        sender = d.get("sender")
        if not isinstance(sender, str) or sender in seen:
            continue

        seen.add(sender)
        clients.append(
            {
                "sender": sender,
                "client_name": d.get("client_name"),
                "unread": d.get("unread", False),
                "bookmarked": d.get("bookmarked", False),
                "blocked": d.get("blocked", False),
                "updated_at": d.get("updated_at"),
                "conversation_date": d.get("conversation_date"),
                "lead_label": d.get("lead_label") or "general",
            }
        )

    return {"clients": clients}


@router.get("/messages/{sender}")
async def get_messages(sender: str, auth: dict[str, Any] = Depends(verify_token)) -> dict[str, Any]:
    _ = auth
    db_client = _conversation_client()
    result = db_client.table("whatsapp_conversations").select("*").eq("sender", sender).execute()
    conversation_rows: list[dict[str, Any]] = []
    for row in result.data or []:
        if not isinstance(row, dict):
            continue
        normalized_row = dict(row)
        if not isinstance(normalized_row.get("conversation"), list):
            normalized_row["conversation"] = []
        conversation_rows.append(normalized_row)
    return {"conversation": conversation_rows}


@router.post("/send_message")
async def send_message(
    body: SendMessageRequest, auth: dict[str, Any] = Depends(verify_token)
) -> dict[str, str]:
    _ = auth
    sender = body.sender
    message = body.message
    db_client = _conversation_client()

    try:
        blocked_check = (
            db_client.table("whatsapp_conversations")
            .select("blocked")
            .eq("sender", sender)
            .limit(1)
            .execute()
        )
        blocked_row = first_row(blocked_check)
        if blocked_row and blocked_row.get("blocked") is True:
            raise HTTPException(status_code=403, detail="Client is blocked")
    except HTTPException:
        raise
    except Exception:
        logger.exception("Block check failed for sender=%s", sender)

    meta_text = "fake"
    if not sender.startswith("fake_"):
        try:
            response_meta = send_whatsapp_text(sender, message)
            meta_text = response_meta.text
            if response_meta.status_code >= 400:
                logger.error("Meta manual send error %s: %s", response_meta.status_code, response_meta.text)
        except Exception:
            logger.exception("Meta manual send failure for sender=%s", sender)
            meta_text = "send_failed"

    existing = (
        db_client.table("whatsapp_conversations")
        .select("id, conversation")
        .eq("sender", sender)
        .execute()
    )

    conversation_data: list[dict[str, Any]] = []
    record_id = None

    first_existing = first_row(existing)
    if first_existing is not None:
        record_id = first_existing.get("id")
        conversation_data = first_existing.get("conversation") or []
        if not isinstance(conversation_data, list):
            conversation_data = []

    conversation_data.append(
        {
            "query": message,
            "response": "",
            "manual": True,
            "time": datetime.datetime.utcnow().isoformat(),
        }
    )

    if record_id:
        (
            db_client.table("whatsapp_conversations")
            .update({"conversation": conversation_data, "updated_at": datetime.datetime.utcnow().isoformat()})
            .eq("id", record_id)
            .execute()
        )
    else:
        insert_res = (
            db_client.table("whatsapp_conversations")
            .upsert(
                {"sender": sender, "conversation": conversation_data},
                on_conflict="sender",
            )
            .execute()
        )
        inserted_row = first_row(insert_res)
        record_id = inserted_row.get("id") if inserted_row else None

    if sender.startswith("fake_"):
        setup_config = _load_setup_configuration()
        ai_reply = await answer_query_from_knowledge(message, setup_config=setup_config)

        if conversation_data:
            conversation_data[-1]["response"] = ai_reply

        if record_id:
            (
                supabase.table("whatsapp_conversations")
                .update(
                    {
                        "conversation": conversation_data,
                        "updated_at": datetime.datetime.utcnow().isoformat(),
                        "unread": True,
                    }
                )
                .eq("id", record_id)
                .execute()
            )

    return {"status": "sent", "meta": meta_text}


@router.delete("/client/{sender}")
async def delete_client(sender: str, auth: dict[str, Any] = Depends(verify_token)) -> dict[str, str]:
    _ = auth
    db_client = _conversation_client()
    db_client.table("whatsapp_conversations").delete().eq("sender", sender).execute()
    return {"status": "deleted"}


@router.post("/client/name")
async def rename_client(
    body: RenameClientRequest, auth: dict[str, Any] = Depends(verify_token)
) -> dict[str, str]:
    _ = auth
    db_client = _conversation_client()
    db_client.table("whatsapp_conversations").update({"client_name": body.name}).eq("sender", body.sender).execute()
    return {"status": "renamed"}


@router.post("/client/read")
async def mark_as_read(
    body: SenderActionRequest, auth: dict[str, Any] = Depends(verify_token)
) -> dict[str, str]:
    _ = auth
    db_client = _conversation_client()
    (
        db_client.table("whatsapp_conversations")
        .update({"unread": False})
        .eq("sender", body.sender)
        .execute()
    )
    return {"status": "read"}


@router.post("/client/bookmark")
async def bookmark_client(
    body: ToggleClientRequest, auth: dict[str, Any] = Depends(verify_token)
) -> dict[str, Any]:
    _ = auth
    bookmarked = bool(body.bookmarked) if body.bookmarked is not None else True
    db_client = _conversation_client()
    try:
        (
            db_client.table("whatsapp_conversations")
            .update({"bookmarked": bookmarked})
            .eq("sender", body.sender)
            .execute()
        )
        return {"status": "bookmarked", "bookmarked": bookmarked}
    except Exception as exc:
        logger.exception("Bookmark update failed for sender=%s", body.sender)
        raise HTTPException(status_code=500, detail="Failed to update bookmark") from exc


@router.post("/client/block")
async def block_client(
    body: ToggleClientRequest, auth: dict[str, Any] = Depends(verify_token)
) -> dict[str, Any]:
    _ = auth
    blocked = bool(body.blocked) if body.blocked is not None else True
    db_client = _conversation_client()
    try:
        (
            db_client.table("whatsapp_conversations")
            .update({"blocked": blocked})
            .eq("sender", body.sender)
            .execute()
        )
        return {"status": "blocked", "blocked": blocked}
    except Exception as exc:
        logger.exception("Block update failed for sender=%s", body.sender)
        raise HTTPException(status_code=500, detail="Failed to update block status") from exc
