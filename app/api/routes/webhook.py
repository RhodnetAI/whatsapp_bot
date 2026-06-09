import asyncio
import copy
import datetime
import logging
from typing import Any

from fastapi import APIRouter, Request

from app.core.config import settings
from app.db.supabase_client import first_row, supabase, supabase_admin
from app.services.bot_chat import generate_bot_reply
from app.services.whatsapp import send_whatsapp_text, send_whatsapp_typing_indicator
from app.services.flow_ai import (
    should_use_flow,
    process_flow_message,
    get_flow_state,
    build_flow_confirmation_details,
    get_flow_lead_label,
)
from app.services.rag import classify_knowledge_lead_label
from app.services.meeting_booking import (
    BOOKING_FLOW_STEPS,
    build_conversation_summary,
    get_meeting_state,
    handle_yes_no_response,
    is_suppressed_for_session,
    process_meeting_step,
    save_booking,
    set_meeting_state,
)
from app.services.email_service import send_meeting_confirmation
from app.services.meet_service import generate_meet_link


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


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _send_messages(sender: str, messages: list[str]) -> None:
    """Send each non-empty message to the sender in order."""
    for msg in messages:
        if not msg or not msg.strip():
            continue
        try:
            resp = send_whatsapp_text(sender, msg)
            if resp.status_code >= 400:
                logger.error("Meta send error %s: %s", resp.status_code, resp.text)
        except Exception:
            logger.exception("Meta send failure for sender=%s", sender)


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
        # ── Load flow builder ──────────────────────────────────────────────
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
        lead_label: str = old_lead_label or "general"
        messages_to_send: list[str] = []

        flow_enabled = should_use_flow(flow_builder)
        flow_state = get_flow_state(conversation_data)

        # Check if flow already completed (handles race conditions)
        flow_already_completed = False
        if flow_enabled:
            try:
                completion_check = (
                    db_client.table("whatsapp_flow_confirmations")
                    .select("id")
                    .eq("sender", sender)
                    .limit(1)
                    .execute()
                )
                if first_row(completion_check):
                    flow_already_completed = True
                    logger.info(
                        "Flow completion found in database for sender=%s; switching to Knowledge AI",
                        sender,
                    )
            except Exception:
                logger.exception("Failed to check flow completion status for sender=%s", sender)

        if flow_enabled and (flow_state.get("completed") or flow_already_completed):
            logger.info("Flow already completed for sender=%s; switching to Knowledge AI", sender)
            flow_enabled = False

        # ── Flow AI path (unchanged) ───────────────────────────────────────
        if flow_enabled:
            logger.info("Using Flow AI for sender=%s", sender)
            ai_reply, updated_flow_state = process_flow_message(
                text, flow_state, conversation_data, flow_builder
            )
            if updated_flow_state and updated_flow_state.get("completed") and ai_reply == "":
                logger.info(
                    "Flow completed but returned empty reply for sender=%s; falling back to Knowledge AI",
                    sender,
                )
                flow_enabled = False

        # ── Knowledge / bot path (with scheduler integration) ─────────────
        if not flow_enabled:
            logger.info("Using bot conversation AI for sender=%s", sender)

            # Load scheduler_enabled once
            scheduler_enabled = False
            try:
                info_res = (
                    db_client.table("information_bot")
                    .select("scheduler_enabled")
                    .eq("id", 1)
                    .limit(1)
                    .execute()
                )
                info_row = first_row(info_res) or {}
                scheduler_enabled = bool(info_row.get("scheduler_enabled"))
            except Exception:
                logger.exception("Failed to load scheduler_enabled for sender=%s", sender)

            meeting_state = get_meeting_state(conversation_data) if scheduler_enabled else None

            # ── Pure booking-flow step (no normal AI) ──────────────────────
            if scheduler_enabled and meeting_state and meeting_state["step"] in BOOKING_FLOW_STEPS:
                logger.info(
                    "Meeting booking flow step=%s for sender=%s",
                    meeting_state["step"],
                    sender,
                )
                booking_reply, meeting_state, is_complete, booking_data = process_meeting_step(
                    text, conversation_data, meeting_state, sender
                )
                ai_reply = booking_reply
                set_meeting_state(conversation_data, meeting_state)
                messages_to_send = [ai_reply]

                if is_complete and booking_data:
                    try:
                        slot_date = booking_data["slot_date"]
                        slot_start = booking_data["slot_start"]
                        meeting_dt = datetime.datetime.fromisoformat(
                            f"{slot_date}T{slot_start}:00+00:00"
                        )
                        start_mins = int(slot_start.split(":")[0]) * 60 + int(slot_start.split(":")[1])
                        slot_end = booking_data.get("slot_end", "")
                        end_mins = int(slot_end.split(":")[0]) * 60 + int(slot_end.split(":")[1]) if slot_end else start_mins + 30
                        duration = max(30, end_mins - start_mins)
                        meet_link = generate_meet_link(meeting_dt, duration)
                        summary = build_conversation_summary(conversation_data)
                        save_booking(sender, booking_data, meet_link, summary)
                        asyncio.create_task(
                            asyncio.to_thread(
                                send_meeting_confirmation,
                                user_email=booking_data["user_email"],
                                user_name=booking_data["user_name"],
                                meeting_datetime=meeting_dt,
                                duration_minutes=duration,
                                meet_link=meet_link,
                                summary=summary,
                            )
                        )
                        logger.info(
                            "Booking completed for sender=%s meet_link=%s", sender, meet_link
                        )
                    except Exception:
                        logger.exception("Failed to finalise booking for sender=%s", sender)

            # ── asked_yes_no: hybrid — check yes/no, else run normal AI ───
            elif scheduler_enabled and meeting_state and meeting_state["step"] == "asked_yes_no":
                extra_msg, meeting_state = handle_yes_no_response(text, meeting_state)

                if extra_msg is not None:
                    # User answered yes or no — no normal AI needed
                    ai_reply = extra_msg
                    set_meeting_state(conversation_data, meeting_state)
                    messages_to_send = [ai_reply]
                else:
                    # User sent unrelated message — run normal AI
                    ai_reply, _, session_greeting = await generate_bot_reply(
                        text, conversation_data
                    )
                    lead_label = await classify_knowledge_lead_label(
                        text, ai_reply, conversation_data, old_label=old_lead_label
                    )

                    # Re-ask if still in asked_yes_no (handle_yes_no_response left it there,
                    # meaning suggestion_count was < 2 before this turn)
                    re_ask: str = ""
                    if meeting_state["step"] == "asked_yes_no":
                        meeting_state["suggestion_count"] = meeting_state.get("suggestion_count", 1) + 1
                        re_ask = (
                            "Just checking — would you like to schedule a meeting or "
                            "consultation with our team? Please reply *Yes* or *No*."
                            "\n\nType *NO* at any time to stop the booking and return to normal chat."
                        )
                    set_meeting_state(conversation_data, meeting_state)

                    messages_to_send = []
                    if session_greeting:
                        messages_to_send.append(session_greeting)
                    messages_to_send.append(ai_reply)
                    if re_ask:
                        messages_to_send.append(re_ask)

            # ── Normal AI path (idle / suppressed / scheduler off) ─────────
            else:
                ai_reply, _, session_greeting = await generate_bot_reply(
                    text, conversation_data
                )
                lead_label = await classify_knowledge_lead_label(
                    text, ai_reply, conversation_data, old_label=old_lead_label
                )

                meeting_suggestion: str = ""
                if (
                    scheduler_enabled
                    and meeting_state
                    and not is_suppressed_for_session(meeting_state)
                    and lead_label in ("high intent", "hot lead")
                    and meeting_state.get("suggestion_count", 0) == 0
                ):
                    meeting_state["step"] = "asked_yes_no"
                    meeting_state["suggestion_count"] = 1
                    meeting_suggestion = (
                        "Would you like to schedule a meeting or consultation with our team? "
                        "Please reply *Yes* or *No*."
                        "\n\nType *NO* at any time to stop the booking and return to normal chat."
                    )
                    set_meeting_state(conversation_data, meeting_state)

                messages_to_send = []
                if session_greeting:
                    messages_to_send.append(session_greeting)
                messages_to_send.append(ai_reply)
                if meeting_suggestion:
                    messages_to_send.append(meeting_suggestion)

            lead_label = lead_label if isinstance(lead_label, str) and lead_label.strip() else "general"

        else:
            # Flow AI path — lead label from flow state
            lead_label = get_flow_lead_label(updated_flow_state or flow_state, flow_builder)
            messages_to_send = [ai_reply]

        # ── Persist primary reply in conversation array ────────────────────
        if conversation_data and isinstance(conversation_data[-1], dict):
            conversation_data[-1]["response"] = ai_reply

        # ── Save flow confirmation if just completed ───────────────────────
        if flow_enabled and record_id and isinstance(updated_flow_state, dict) and updated_flow_state.get("completed"):
            try:
                confirmation_payload = build_flow_confirmation_details(flow_builder, updated_flow_state)
                db_client.table("whatsapp_flow_confirmations").upsert(
                    {
                        "conversation_id": record_id,
                        "sender": sender,
                        "details": confirmation_payload,
                        "confirmed_at": _now_iso(),
                    },
                    on_conflict="conversation_id",
                ).execute()
            except Exception:
                logger.exception(
                    "Failed to persist flow confirmation for conversation_id=%s", record_id
                )

        # ── Update conversation in database ───────────────────────────────
        update_payload = {
            "conversation": conversation_data,
            "updated_at": _now_iso(),
            "lead_label": lead_label,
        }
        if record_id:
            db_client.table("whatsapp_conversations").update(update_payload).eq(
                "id", record_id
            ).execute()
        else:
            db_client.table("whatsapp_conversations").update(update_payload).eq(
                "sender", sender
            ).execute()

        # ── Send all WhatsApp messages ─────────────────────────────────────
        _send_messages(sender, messages_to_send)

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
                .select("id, conversation, lead_label, ai_disabled")
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
        existing_ai_disabled = bool(first_existing.get("ai_disabled"))
    else:
        existing_lead_label = None
        existing_ai_disabled = False

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

        # Carry forward meeting_state across turns so the booking flow survives
        # across multiple messages (same pattern as flow_state above)
        if conversation_data:
            prev_meeting_state = conversation_data[-1].get("meeting_state")
            if isinstance(prev_meeting_state, dict):
                message_entry["meeting_state"] = copy.deepcopy(prev_meeting_state)

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

    if existing_ai_disabled:
        logger.info("AI disabled for sender=%s; skipping automated response", sender)
        return

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

    # Spawn background task to generate response and update database
    # This allows the webhook to return immediately (within 3 seconds per WhatsApp spec)
    asyncio.create_task(_generate_response_and_update(sender, text, record_id, conversation_data, initial_lead_label))
