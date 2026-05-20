import asyncio
import copy
import datetime
import logging
from typing import Any

from fastapi import APIRouter, Request

from app.core.config import settings
from app.db.supabase_client import first_row, supabase, supabase_admin
from app.services.knowledge import answer_query_from_knowledge
from app.services.whatsapp import send_whatsapp_text, send_whatsapp_typing_indicator
from app.services.flow_ai import (
    should_use_flow,
    process_flow_message,
    get_flow_state,
    build_flow_confirmation_details,
    get_flow_lead_label,
)
from app.services.rag import classify_knowledge_lead_label


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
    old_lead_label: str,
) -> None:
    """Background task: generate AI response (flow or knowledge) and update conversation asynchronously."""
    db_client = _conversation_client()
    
    try:
        # Load flow builder state
        flow_builder = None
        try:
            flow_res = (
                supabase.table("service_agent_setup")
                .select("flow_builder")
                .eq("id", 1)
                .limit(1)
                .execute()
            )
            flow_row = first_row(flow_res) or {}
            if isinstance(flow_row, dict):
                flow_builder = flow_row.get("flow_builder")
        except Exception:
            logger.exception("Failed to load flow builder state")
        
        ai_reply: str = ""
        updated_flow_state: dict[str, Any] | None = None
        flow_enabled = should_use_flow(flow_builder)
        flow_state = get_flow_state(conversation_data)

        if flow_enabled and flow_state.get("completed"):
            logger.info(
                "Flow already completed for sender=%s; switching to Knowledge AI",
                sender,
            )
            flow_enabled = False

        if flow_enabled:
            # Use Flow AI for conversation
            logger.info("Using Flow AI for sender=%s", sender)
            ai_reply, updated_flow_state = process_flow_message(text, flow_state, conversation_data, flow_builder)
            if updated_flow_state and updated_flow_state.get("completed") and ai_reply == "":
                logger.info(
                    "Flow completed but flow message returned empty reply for sender=%s; falling back to Knowledge AI",
                    sender,
                )
                flow_enabled = False

        if not flow_enabled:
            # Use Knowledge AI
            logger.info("Using Knowledge AI for sender=%s", sender)
            
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

            lead_label = await classify_knowledge_lead_label(
                text,
                ai_reply,
                conversation_data,
                old_label=old_lead_label,
            )
        else:
            lead_label = get_flow_lead_label(updated_flow_state or flow_state, flow_builder)
        
        if conversation_data and isinstance(conversation_data[-1], dict):
            conversation_data[-1]["response"] = ai_reply

        # Save confirmed details when the flow completes
        if flow_enabled and record_id and isinstance(updated_flow_state, dict) and updated_flow_state.get("completed"):
            try:
                confirmation_payload = build_flow_confirmation_details(flow_builder, updated_flow_state)
                db_client.table("whatsapp_flow_confirmations").upsert(
                    {
                        "conversation_id": record_id,
                        "sender": sender,
                        "details": confirmation_payload,
                        "confirmed_at": datetime.datetime.utcnow().isoformat(),
                    },
                    on_conflict="conversation_id",
                ).execute()
            except Exception:
                logger.exception("Failed to persist flow confirmation for conversation_id=%s", record_id)

        # Update database with response
        if record_id:
            db_client.table("whatsapp_conversations").update(
                {
                    "conversation": conversation_data,
                    "updated_at": datetime.datetime.utcnow().isoformat(),
                    "lead_label": lead_label,
                }
            ).eq("id", record_id).execute()
        else:
            db_client.table("whatsapp_conversations").update(
                {
                    "conversation": conversation_data,
                    "updated_at": datetime.datetime.utcnow().isoformat(),
                    "lead_label": lead_label,
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
    message_id = message.get("id")
    message_type = message.get("type")

    logger.debug(
        "Message details: sender=%s, message_id=%s, message_type=%s",
        sender,
        message_id,
        message_type,
    )

    # Send typing indicator to show the bot is processing the received message
    if isinstance(sender, str) and sender and isinstance(message_id, str):
        logger.info(
            "Sending typing indicator for incoming message from sender=%s message_id=%s type=%s",
            sender,
            message_id,
            message_type,
        )
        try:
            send_whatsapp_typing_indicator(message_id)
            logger.info("Typing indicator sent successfully")
        except Exception:
            logger.exception("Error sending typing indicator for sender=%s", sender)
    else:
        logger.debug(
            "Skipping typing indicator because sender or message_id is missing/invalid: sender=%s message_id=%s",
            sender,
            message_id,
        )

    # Now extract text for text messages only
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
                .select("id, conversation, lead_label")
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
        existing_lead_label = first_existing.get("lead_label")
    else:
        existing_lead_label = None

    # Append incoming query with empty response (will be filled by background task)
    try:
        now_iso = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()

        # Initialize message entry with flow_state if needed
        message_entry: dict[str, Any] = {
            "query": text,
            "response": "",
            "time": now_iso,
        }

        # Check if flow is enabled to initialize flow_state
        flow_builder = None
        try:
            flow_res = (
                supabase.table("service_agent_setup")
                .select("flow_builder")
                .eq("id", 1)
                .limit(1)
                .execute()
            )
            flow_row = first_row(flow_res) or {}
            if isinstance(flow_row, dict):
                flow_builder = flow_row.get("flow_builder")
        except Exception:
            logger.exception("Failed to load flow builder state during message append")

        initial_lead_label = existing_lead_label if isinstance(existing_lead_label, str) and existing_lead_label.strip() else None
        if initial_lead_label is None or initial_lead_label == "none":
            initial_lead_label = "general"

        if should_use_flow(flow_builder):
            if not conversation_data:
                # First flow message in this conversation
                message_entry["flow_state"] = {
                    "started": False,
                    "current_question_index": 0,
                    "answers": {},
                    "completed": False,
                }
            else:
                # Preserve existing flow state from the previous conversation turn
                previous_entry = conversation_data[-1]
                previous_flow_state = previous_entry.get("flow_state")
                if isinstance(previous_flow_state, dict):
                    message_entry["flow_state"] = copy.deepcopy(previous_flow_state)

        conversation_data.append(message_entry)

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
                    "lead_label": initial_lead_label,
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
    asyncio.create_task(_generate_response_and_update(sender, text, record_id, conversation_data, initial_lead_label))
