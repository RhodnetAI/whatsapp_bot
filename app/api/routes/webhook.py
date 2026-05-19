import asyncio
import datetime
import logging
from typing import Any

from fastapi import APIRouter, Request

from app.core.config import settings
from app.db.supabase_client import first_row, supabase, supabase_admin
from app.services.knowledge import answer_query_from_knowledge
from app.services.whatsapp import send_whatsapp_text


router = APIRouter(tags=["webhook"])
logger = logging.getLogger("whatsapp")


def _conversation_client() -> Any:
    return supabase_admin if supabase_admin is not None else supabase


@router.get("/webhook")
async def verify(request: Request) -> int | dict[str, str]:
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == settings.verify_token and challenge is not None:
        return int(challenge)
    return {"error": "Verification failed"}


@router.post("/webhook")
async def receive_message(request: Request) -> dict[str, str]:
    data = await request.json()
    logger.info("Webhook received")
    await process_message(data)
    return {"status": "received"}


async def _generate_response_and_update(
    sender: str,
    text: str,
    record_id: str | None,
    conversation_data: list[dict[str, Any]],
) -> None:
    """Background task: generate AI response and update conversation asynchronously."""
    db_client = _conversation_client()
    
    try:
        # Load setup config
        setup_config: dict[str, str] = {
            "main_instruction": "",
            "dos": "",
            "donts": "",
        }
        try:
            config_res = (
                supabase.table("service_agent_setup")
                .select("main_instruction,dos,donts")
                .eq("id", 1)
                .limit(1)
                .execute()
            )
            config_row = first_row(config_res) or {}
            if isinstance(config_row, dict):
                setup_config = {
                    "main_instruction": config_row.get("main_instruction") or "",
                    "dos": config_row.get("dos") or "",
                    "donts": config_row.get("donts") or "",
                }
        except Exception:
            logger.exception("Failed to load setup configuration for webhook response")

        # Generate AI response
        ai_reply = await answer_query_from_knowledge(text, setup_config=setup_config)
        if conversation_data and isinstance(conversation_data[-1], dict):
            conversation_data[-1]["response"] = ai_reply

        # Update database with response
        if record_id:
            db_client.table("whatsapp_conversations").update(
                {
                    "conversation": conversation_data,
                    "updated_at": datetime.datetime.utcnow().isoformat(),
                }
            ).eq("id", record_id).execute()
        else:
            db_client.table("whatsapp_conversations").update(
                {
                    "conversation": conversation_data,
                    "updated_at": datetime.datetime.utcnow().isoformat(),
                }
            ).eq("sender", sender).execute()

        # Send WhatsApp response
        try:
            meta_response = send_whatsapp_text(sender, ai_reply)
            if meta_response.status_code >= 400:
                logger.error("Meta send error %s: %s", meta_response.status_code, meta_response.text)
        except Exception:
            logger.exception("Meta send failure for sender=%s", sender)

    except Exception:
        logger.exception("Background response generation failed for sender=%s", sender)


async def process_message(data: Any) -> None:
    if not isinstance(data, dict):
        return

    entry = data.get("entry")
    if not isinstance(entry, list) or not entry:
        return

    first_entry = entry[0]
    if not isinstance(first_entry, dict):
        return

    changes = first_entry.get("changes")
    if not isinstance(changes, list) or not changes:
        return

    first_change = changes[0]
    if not isinstance(first_change, dict):
        return

    value = first_change.get("value")
    if not isinstance(value, dict):
        return

    messages = value.get("messages")
    if not isinstance(messages, list) or not messages:
        return

    message = messages[0]
    if not isinstance(message, dict):
        return

    sender = message.get("from")
    text = ""
    text_field = message.get("text")
    if isinstance(text_field, dict):
        text = text_field.get("body", "") or ""

    if not isinstance(sender, str) or text == "":
        return

    if not sender.startswith("+"):
        sender = f"+{sender}"

    # Parallelize database queries: check blocked status + fetch existing conversation
    db_client = _conversation_client()

    async def get_blocked_status():
        try:
            blocked_check = (
                db_client.table("whatsapp_conversations")
                .select("blocked")
                .eq("sender", sender)
                .limit(1)
                .execute()
            )
            blocked_row = first_row(blocked_check)
            return blocked_row and blocked_row.get("blocked") is True
        except Exception:
            logger.exception("Block check failed for sender=%s", sender)
            return False

    async def get_existing_conversation():
        try:
            return (
                db_client.table("whatsapp_conversations")
                .select("id, conversation")
                .eq("sender", sender)
                .execute()
            )
        except Exception:
            logger.exception("Failed to fetch existing conversation for sender=%s", sender)
            return None

    is_blocked, existing = await asyncio.gather(get_blocked_status(), get_existing_conversation())
    if is_blocked:
        return

    conversation_data: list[dict[str, Any]] = []
    record_id = None

    first_existing = first_row(existing)
    if first_existing is not None:
        record_id = first_existing.get("id")
        conversation_data = first_existing.get("conversation") or []
        if not isinstance(conversation_data, list):
            conversation_data = []

    # Append incoming query with empty response (will be filled by background task)
    try:
        now_iso = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
        conversation_data.append({"query": text, "response": "", "time": now_iso})

        # Use UPSERT to avoid race conditions with duplicate key errors
        upsert_res = (
            db_client.table("whatsapp_conversations")
            .upsert(
                {
                    "sender": sender,
                    "client_name": sender,  # Use phone number as default client name
                    "conversation": conversation_data,
                    "updated_at": datetime.datetime.utcnow().isoformat(),
                    "unread": True,
                },
                on_conflict="sender",
            )
            .execute()
        )
        upserted_row = first_row(upsert_res)
        record_id = upserted_row.get("id") if upserted_row else None
    except Exception:
        logger.exception("Failed to persist incoming user message for sender=%s", sender)
        return

    # Spawn background task to generate response and update database
    # This allows the webhook to return immediately (within 3 seconds per WhatsApp spec)
    asyncio.create_task(_generate_response_and_update(sender, text, record_id, conversation_data))
