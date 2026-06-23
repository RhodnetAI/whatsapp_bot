import asyncio
import copy
import datetime
import logging
from typing import Any

from fastapi import APIRouter, Request

from app.core.config import settings
from app.db.supabase_client import first_row, supabase, supabase_admin
from app.services.bot_chat import generate_bot_reply, is_first_message_of_session
from app.services.sales.handler import handle_sales_message
from app.services.whatsapp import send_whatsapp_text, send_whatsapp_typing_indicator
from app.services.flow_ai import (
    should_use_flow,
    process_flow_message,
    get_flow_state,
    set_flow_state,
    fresh_flow_state,
    get_run_mode,
    build_flow_confirmation_details,
    get_flow_lead_label,
    RUN_MODE_ONCE_PER_USER,
)
from app.services.rag import classify_knowledge_lead_label
from app.services.meeting_booking import (
    BOOKING_FLOW_STEPS,
    fresh_meeting_state,
    get_meeting_state,
    handle_yes_no_response,
    is_suppressed_for_session,
    process_meeting_step,
    save_booking,
    set_meeting_state,
)
from app.services.email_service import (
    build_booking_body,
    build_flow_completion_body,
    send_flow_completion_email,
    send_meeting_confirmation,
)
from app.services.meet_service import create_meeting_event


router = APIRouter(tags=["webhook"])
logger = logging.getLogger("whatsapp")


def _conversation_client() -> Any:
    return supabase_admin if supabase_admin is not None else supabase


async def _active_bot_is_sales() -> bool:
    """True when the Sales Bot is the selected bot. Used to route the webhook to
    the self-contained sales pipeline; otherwise the Information Bot path runs."""
    try:
        row = first_row(
            _conversation_client().table("sales_bot").select("is_selected").eq("id", 1).limit(1).execute()
        )
        return bool(row and row.get("is_selected"))
    except Exception:
        logger.exception("Failed to check sales_bot selection")
        return False


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


# WhatsApp auto-dismisses a typing indicator ~25s after it's sent. Re-send it
# on this cadence (comfortably under that ceiling) so it stays visible for the
# whole background task, not just its first 25s — important for paths like
# Knowledge retrieval (embeddings + Qdrant + optional VoyageAI rerank) on top
# of the LLM call, which can together run past 25s.
_TYPING_KEEPALIVE_INTERVAL_SECONDS = 20.0


async def _typing_keepalive(sender: str, message_id: str) -> None:
    """Re-send the typing indicator every `_TYPING_KEEPALIVE_INTERVAL_SECONDS`
    until cancelled by the caller once the real reply is ready to send."""
    try:
        while True:
            await asyncio.sleep(_TYPING_KEEPALIVE_INTERVAL_SECONDS)
            try:
                resp = send_whatsapp_typing_indicator(message_id)
                if resp.status_code >= 400:
                    logger.warning(
                        "Typing indicator keepalive rejected sender=%s status=%s",
                        sender, resp.status_code,
                    )
            except Exception:
                logger.exception("Typing indicator keepalive failed for sender=%s", sender)
    except asyncio.CancelledError:
        pass


async def _generate_response_and_update(
    sender: str,
    text: str,
    record_id: str | None,
    conversation_data: list[dict[str, Any]],
    old_lead_label: str,
    message_id: str | None = None,
) -> None:
    """Background task: generate AI response (flow or knowledge) and update conversation asynchronously."""
    db_client = _conversation_client()

    # Refresh the typing indicator at the start of the background task so the
    # user sees it for the full duration of DB loads + response generation.
    # This is especially important for the flow path which is fast/deterministic
    # (no LLM wait) — the initial indicator from process_message may have
    # already scrolled past the perception threshold by the time we send.
    if isinstance(message_id, str) and message_id:
        try:
            _typing_resp = send_whatsapp_typing_indicator(message_id)
            if _typing_resp.status_code >= 400:
                logger.warning(
                    "Background typing indicator rejected sender=%s status=%s",
                    sender, _typing_resp.status_code,
                )
        except Exception:
            logger.exception("Background typing indicator failed for sender=%s", sender)

    keepalive_task: asyncio.Task[None] | None = None
    if isinstance(message_id, str) and message_id:
        keepalive_task = asyncio.create_task(_typing_keepalive(sender, message_id))

    try:
        # ── Load flow builder + admin notification toggles ──────────────────
        flow_builder = None
        flow_creation_enabled = False
        flow_notify_whatsapp = False
        flow_notify_email = False
        try:
            flow_res = (
                db_client.table("information_bot")
                .select("flow_builder, flow_creation_enabled, flow_notify_whatsapp_enabled, flow_notify_email_enabled")
                .eq("id", 1)
                .limit(1)
                .execute()
            )
            flow_row = first_row(flow_res) or {}
            if isinstance(flow_row, dict):
                flow_builder = flow_row.get("flow_builder")
                flow_creation_enabled = bool(flow_row.get("flow_creation_enabled"))
                flow_notify_whatsapp = bool(flow_row.get("flow_notify_whatsapp_enabled"))
                flow_notify_email = bool(flow_row.get("flow_notify_email_enabled"))
        except Exception:
            logger.exception("Failed to load flow builder state")

        ai_reply: str = ""
        updated_flow_state: dict[str, Any] | None = None
        lead_label: str = old_lead_label or "general"
        messages_to_send: list[str] = []

        flow_enabled = should_use_flow(flow_builder, flow_creation_enabled)
        flow_state = get_flow_state(conversation_data)
        run_mode = get_run_mode(flow_builder)

        # "Every new session" run mode: if the flow was completed in a
        # previous session, restart it for this new session.
        if flow_enabled and run_mode != RUN_MODE_ONCE_PER_USER and flow_state.get("completed"):
            prior_entries = conversation_data[:-1] if conversation_data else []
            if is_first_message_of_session(prior_entries):
                flow_state = fresh_flow_state()
                set_flow_state(conversation_data, flow_state)

        # "Once per user" run mode: check if flow already completed at any
        # point in the past (handles race conditions too).
        flow_already_completed = False
        if flow_enabled and run_mode == RUN_MODE_ONCE_PER_USER:
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

            # Load scheduler_enabled + admin notification toggles once
            scheduler_enabled = False
            notify_admin_whatsapp = False
            notify_admin_email = False
            try:
                info_res = (
                    db_client.table("information_bot")
                    .select("scheduler_enabled, scheduler_notify_whatsapp_enabled, scheduler_notify_email_enabled")
                    .eq("id", 1)
                    .limit(1)
                    .execute()
                )
                info_row = first_row(info_res) or {}
                scheduler_enabled = bool(info_row.get("scheduler_enabled"))
                notify_admin_whatsapp = bool(info_row.get("scheduler_notify_whatsapp_enabled"))
                notify_admin_email = bool(info_row.get("scheduler_notify_email_enabled"))
            except Exception:
                logger.exception("Failed to load scheduler_enabled for sender=%s", sender)

            meeting_state = get_meeting_state(conversation_data) if scheduler_enabled else None

            # A "session" is one calendar day (UTC), derived from prior entries
            # in whatsapp_conversations — same definition as is_first_message_of_session
            # below. Reset any already-consumed suggestion (declined / completed /
            # exhausted-unanswered) at the start of a new day so the user gets a
            # fresh scheduling opportunity each day. Active mid-flow steps
            # (BOOKING_FLOW_STEPS) are left untouched so an in-progress booking
            # never gets wiped out by a midnight rollover.
            if (
                scheduler_enabled
                and meeting_state
                and meeting_state["step"] not in BOOKING_FLOW_STEPS
                and meeting_state.get("suggestion_count", 0) > 0
            ):
                prior_entries = conversation_data[:-1] if conversation_data else []
                if is_first_message_of_session(prior_entries):
                    meeting_state = fresh_meeting_state()
                    set_meeting_state(conversation_data, meeting_state)

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
                    # Show typing indicator while we create the calendar event,
                    # send emails, and prepare the confirmation messages below.
                    if isinstance(message_id, str) and message_id:
                        try:
                            typing_resp = send_whatsapp_typing_indicator(message_id)
                            if typing_resp.status_code >= 400:
                                logger.error(
                                    "Typing indicator rejected during booking completion sender=%s status=%s body=%s",
                                    sender, typing_resp.status_code, typing_resp.text,
                                )
                        except Exception:
                            logger.exception("Failed to send typing indicator for sender=%s", sender)

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
                        purpose = booking_data.get("purpose") or "Not specified"

                        # Build attendees list from user + admin emails
                        attendees: list[str] = [booking_data["user_email"]]
                        if settings.admin_email and settings.admin_email != booking_data["user_email"]:
                            attendees.append(settings.admin_email)

                        description = (
                            f"Booked via WhatsApp for {booking_data['user_name']} "
                            f"({booking_data['user_email']}).\n\n"
                            f"Purpose: {purpose}"
                        )

                        logger.info(
                            "Creating calendar event for sender=%s attendees=%s",
                            sender, attendees,
                        )
                        meet_link, calendar_event_id, calendar_event_link = await asyncio.to_thread(
                            create_meeting_event,
                            meeting_dt,
                            duration,
                            "Business Consultation",
                            attendees,
                            description,
                        )
                        logger.info(
                            "Calendar event result: meet=%s event_id=%s calendar=%s",
                            meet_link, calendar_event_id, calendar_event_link,
                        )

                        save_booking(
                            sender, booking_data, meet_link, purpose,
                            calendar_event_id=calendar_event_id,
                            calendar_event_link=calendar_event_link,
                        )

                        logger.info("Sending confirmation email for sender=%s to %s", sender, booking_data["user_email"])
                        await asyncio.to_thread(
                            send_meeting_confirmation,
                            user_email=booking_data["user_email"],
                            user_name=booking_data["user_name"],
                            meeting_datetime=meeting_dt,
                            duration_minutes=duration,
                            meet_link=meet_link,
                            purpose=purpose,
                            calendar_event_link=calendar_event_link,
                            email_enabled=notify_admin_email,
                        )
                        logger.info("Confirmation email completed for sender=%s", sender)

                        # Second WhatsApp message: full booking details
                        whatsapp_summary = build_booking_body(
                            user_name=booking_data["user_name"],
                            user_email=booking_data["user_email"],
                            meeting_datetime=meeting_dt,
                            duration_minutes=duration,
                            meet_link=meet_link,
                            purpose=purpose,
                            calendar_event_link=calendar_event_link,
                        )
                        messages_to_send.append(whatsapp_summary)

                        # ── Admin notifications (Scheduler toggles) ─────────
                        if notify_admin_whatsapp and settings.admin_whatsapp_number:
                            try:
                                admin_whatsapp_summary = build_booking_body(
                                    user_name=booking_data["user_name"],
                                    user_email=booking_data["user_email"],
                                    meeting_datetime=meeting_dt,
                                    duration_minutes=duration,
                                    meet_link=meet_link,
                                    purpose=purpose,
                                    calendar_event_link=calendar_event_link,
                                    recipient_is_admin=True,
                                )
                                resp = send_whatsapp_text(
                                    settings.admin_whatsapp_number, admin_whatsapp_summary
                                )
                                if resp.status_code >= 400:
                                    logger.error(
                                        "Admin WhatsApp notification failed %s: %s",
                                        resp.status_code, resp.text,
                                    )
                                else:
                                    logger.info(
                                        "Admin WhatsApp notification sent to %s",
                                        settings.admin_whatsapp_number,
                                    )
                            except Exception:
                                logger.exception(
                                    "Failed to send admin WhatsApp notification for sender=%s", sender
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
            confirmation_payload: dict[str, Any] | None = None
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

            if confirmation_payload is not None:
                questions = confirmation_payload.get("questions", [])

                if flow_notify_email:
                    try:
                        await asyncio.to_thread(
                            send_flow_completion_email,
                            sender=sender,
                            questions=questions,
                            email_enabled=True,
                        )
                    except Exception:
                        logger.exception("Failed to send flow completion email for sender=%s", sender)

                if flow_notify_whatsapp and settings.admin_whatsapp_number:
                    try:
                        flow_whatsapp_summary = build_flow_completion_body(sender, questions)
                        flow_resp = send_whatsapp_text(settings.admin_whatsapp_number, flow_whatsapp_summary)
                        if flow_resp.status_code >= 400:
                            logger.error(
                                "Admin WhatsApp flow notification rejected sender=%s status=%s body=%s",
                                sender, flow_resp.status_code, flow_resp.text,
                            )
                        else:
                            logger.info(
                                "Admin WhatsApp flow notification sent to %s", settings.admin_whatsapp_number
                            )
                    except Exception:
                        logger.exception("Failed to send admin WhatsApp flow notification for sender=%s", sender)

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
        # Stop the keepalive first: WhatsApp dismisses the indicator once a
        # message is sent, and we don't want a stray refresh racing past that.
        if keepalive_task is not None:
            keepalive_task.cancel()
            keepalive_task = None
        _send_messages(sender, messages_to_send)

    except Exception:
        logger.exception("Background response generation failed for sender=%s", sender)
    finally:
        if keepalive_task is not None:
            keepalive_task.cancel()


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

    if not isinstance(sender, str) or not sender:
        return
    if not sender.startswith("+"):
        sender = f"+{sender}"

    # ── Routing fork: the Sales Bot owns its own pipeline end-to-end ─────────
    # (interactive lists/buttons, native-cart `order` messages, Flow replies).
    # Reached for every inbound message; when the Sales Bot is selected the
    # Information Bot path below is skipped entirely (mutual exclusivity).
    try:
        if await _active_bot_is_sales():
            # Process in the background so the webhook returns within WhatsApp's
            # timeout (same pattern as the Information Bot path below).
            asyncio.create_task(handle_sales_message(sender, message, message_id))
            return
    except Exception:
        logger.exception("Sales routing failed for sender=%s", sender)
        return

    # ── Information Bot path (text only, unchanged below) ───────────────────
    text = ""
    text_field = message.get("text")
    if isinstance(text_field, dict):
        text = text_field.get("body", "") or ""
    if text == "":
        return

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
        flow_creation_enabled = False
        try:
            flow_res = (
                db_client.table("information_bot")
                .select("flow_builder, flow_creation_enabled")
                .eq("id", 1)
                .limit(1)
                .execute()
            )
            flow_row = first_row(flow_res) or {}
            if isinstance(flow_row, dict):
                flow_builder = flow_row.get("flow_builder")
                flow_creation_enabled = bool(flow_row.get("flow_creation_enabled"))
        except Exception:
            logger.exception("Failed to load flow builder state during message append")

        initial_lead_label = existing_lead_label if isinstance(existing_lead_label, str) and existing_lead_label.strip() else None
        if initial_lead_label is None or initial_lead_label == "none":
            initial_lead_label = "general"

        if should_use_flow(flow_builder, flow_creation_enabled):
            if not conversation_data:
                # First flow message in this conversation
                message_entry["flow_state"] = {
                    "started": False,
                    "current_question_id": None,
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
            typing_resp = send_whatsapp_typing_indicator(message_id)
            if typing_resp.status_code >= 400:
                logger.error(
                    "Typing indicator rejected for sender=%s status=%s body=%s",
                    sender, typing_resp.status_code, typing_resp.text,
                )
            else:
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
    asyncio.create_task(_generate_response_and_update(sender, text, record_id, conversation_data, initial_lead_label, message_id))
