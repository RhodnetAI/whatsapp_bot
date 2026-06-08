import datetime
import logging
from typing import Any

from app.db.supabase_client import first_row, supabase, supabase_admin
from app.services.ai import generate_ai_reply

logger = logging.getLogger("whatsapp")

SINGLETON_ID = 1
MAX_HISTORY_TURNS = 5

_HISTORY_TABLES = {
    "information_bot": "information_bot_conversations",
    "sales_bot": "sales_bot_conversations",
}

# Prompt-assembly building blocks loaded from app_config at runtime — see
# sql/014_add_bot_prompt_config_entries.sql for their stored values.
_PROMPT_CONFIG_KEYS = (
    "answering_guidelines",
    "response_format_rules",
    "history_header",
    "history_footer",
)


def _db():
    return supabase_admin if supabase_admin is not None else supabase


def _today() -> str:
    return datetime.datetime.utcnow().date().isoformat()


async def _fetch_prompt_config() -> dict[str, str]:
    """Fetch the prompt-assembly building blocks from app_config by key."""
    values = {key: "" for key in _PROMPT_CONFIG_KEYS}
    try:
        result = (
            _db()
            .table("app_config")
            .select("key, value")
            .in_("key", list(_PROMPT_CONFIG_KEYS))
            .execute()
        )
        for row in result.data or []:
            if not isinstance(row, dict):
                continue
            key = row.get("key")
            if isinstance(key, str) and key in values:
                values[key] = str(row.get("value") or "")
    except Exception:
        logger.exception("Failed to fetch prompt config from app_config")
    return values


async def get_active_bot() -> dict[str, Any]:
    """Identify the active bot (information_bot or sales_bot) via is_selected
    and return its identity/instruction fields plus its history table name."""
    info_row = first_row(
        _db()
        .table("information_bot")
        .select("is_selected, bot_name, greeting, summarized_instruction")
        .eq("id", SINGLETON_ID)
        .limit(1)
        .execute()
    )
    information_active = info_row is None or bool(info_row.get("is_selected"))

    if information_active:
        bot_table = "information_bot"
        row = info_row or {}
    else:
        bot_table = "sales_bot"
        row = (
            first_row(
                _db()
                .table("sales_bot")
                .select("bot_name, greeting, summarized_instruction")
                .eq("id", SINGLETON_ID)
                .limit(1)
                .execute()
            )
            or {}
        )

    return {
        "bot_table": bot_table,
        "history_table": _HISTORY_TABLES[bot_table],
        "bot_name": row.get("bot_name") or "",
        "greeting": row.get("greeting") or "",
        "summarized_instruction": row.get("summarized_instruction") or "",
    }


async def is_first_message_of_session(history_table: str, phone_number: str) -> bool:
    """A session is one calendar day per phone number: the session has just
    started if no message exists yet for this phone number on today's date."""
    try:
        result = (
            _db()
            .table(history_table)
            .select("id")
            .eq("phone_number", phone_number)
            .eq("session_date", _today())
            .limit(1)
            .execute()
        )
        return first_row(result) is None
    except Exception:
        logger.exception(
            "Failed to check session start for phone=%s table=%s", phone_number, history_table
        )
        return False


async def fetch_recent_turns(
    history_table: str, phone_number: str, turns: int = MAX_HISTORY_TURNS
) -> list[dict[str, str]]:
    """Fetch the last `turns` user/assistant turns (up to turns*2 messages)
    from today's session, oldest first, as {"role", "content"} dicts ready
    to be placed directly into the messages array sent to the model."""
    try:
        result = (
            _db()
            .table(history_table)
            .select("role, message, created_at")
            .eq("phone_number", phone_number)
            .eq("session_date", _today())
            .order("created_at", desc=True)
            .limit(turns * 2)
            .execute()
        )
        rows = list(result.data or [])
        rows.reverse()
        return [{"role": row.get("role"), "content": row.get("message") or ""} for row in rows]
    except Exception:
        logger.exception(
            "Failed to fetch conversation history for phone=%s table=%s", phone_number, history_table
        )
        return []


def build_messages(
    summarized_instruction: str,
    prompt_config: dict[str, str],
    history: list[dict[str, str]],
    user_message: str,
) -> list[dict[str, str]]:
    """Assemble the messages array the same way inteliz's basic bot does
    (system message, replayed history, current user message — as a plain
    OpenAI-style messages array), using the prompt building blocks fetched
    from app_config: the bot's own instructions plus the platform-level
    answering guidelines and response format rules form the system message,
    and the replayed history is framed by the configured header/footer."""
    system_sections = [
        section
        for section in (
            summarized_instruction.strip(),
            prompt_config.get("answering_guidelines", "").strip(),
            prompt_config.get("response_format_rules", "").strip(),
        )
        if section
    ]
    messages: list[dict[str, str]] = [{"role": "system", "content": "\n\n".join(system_sections)}]

    if history:
        header = prompt_config.get("history_header", "").strip()
        footer = prompt_config.get("history_footer", "").strip()
        if header:
            messages.append({"role": "system", "content": header})
        messages.extend(history)
        if footer:
            messages.append({"role": "system", "content": footer})

    messages.append({"role": "user", "content": user_message})
    return messages


async def generate_bot_reply(phone_number: str, user_message: str) -> tuple[str, dict[str, Any]]:
    """Generate the reply for an incoming message from the active bot.

    Returns (reply_text, bot) where `bot` is the active-bot info dict
    (including history_table) so the caller can persist the turn afterwards.
    """
    bot = await get_active_bot()
    history_table = bot["history_table"]

    if await is_first_message_of_session(history_table, phone_number):
        reply = bot["greeting"] or f"Hello! I'm {bot['bot_name'] or 'your assistant'}."
        return reply, bot

    history = await fetch_recent_turns(history_table, phone_number)
    prompt_config = await _fetch_prompt_config()
    summarized_instruction = bot["summarized_instruction"] or f"You are {bot['bot_name'] or 'a helpful assistant'}."
    messages = build_messages(summarized_instruction, prompt_config, history, user_message)
    reply = await generate_ai_reply(messages)
    return reply, bot


async def record_turn(history_table: str, phone_number: str, user_message: str, bot_message: str) -> None:
    """Persist a completed user/assistant turn into the bot's own history table."""
    try:
        today = _today()
        user_time = datetime.datetime.utcnow()
        bot_time = user_time + datetime.timedelta(milliseconds=1)
        _db().table(history_table).insert(
            [
                {
                    "phone_number": phone_number,
                    "role": "user",
                    "message": user_message,
                    "session_date": today,
                    "created_at": user_time.isoformat(),
                },
                {
                    "phone_number": phone_number,
                    "role": "assistant",
                    "message": bot_message,
                    "session_date": today,
                    "created_at": bot_time.isoformat(),
                },
            ]
        ).execute()
    except Exception:
        logger.exception(
            "Failed to store conversation turn for phone=%s table=%s", phone_number, history_table
        )


async def record_assistant_message(history_table: str, phone_number: str, message: str) -> None:
    """Persist an outgoing message that did not come from the AI (e.g. an
    admin message sent manually from the dashboard) so it is part of the
    history the AI sees for subsequent replies."""
    try:
        _db().table(history_table).insert(
            {
                "phone_number": phone_number,
                "role": "assistant",
                "message": message,
                "session_date": _today(),
                "created_at": datetime.datetime.utcnow().isoformat(),
            }
        ).execute()
    except Exception:
        logger.exception(
            "Failed to store manual message for phone=%s table=%s", phone_number, history_table
        )
