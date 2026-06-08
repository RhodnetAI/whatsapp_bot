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
# sql/014_add_bot_prompt_config_entries.sql and
# sql/015_add_company_info_products_prompt_config_entries.sql for their stored
# values.
_PROMPT_CONFIG_KEYS = (
    "answering_guidelines",
    "response_format_rules",
    "history_header",
    "history_footer",
    "links_references_prompt",
    "contact_info_prompt",
    "products_services_prompt",
)

_PRODUCTS_SERVICES_TABLE = "information_bot_products_services"

# Order/labels mirror the "Available fields" list in the products_services_prompt
# stored in app_config (sql/015_add_company_info_products_prompt_config_entries.sql).
_PRODUCT_SERVICE_FIELDS = (
    ("name", "Name"),
    ("short_description", "Short Description"),
    ("category", "Category"),
    ("full_description", "Full Description"),
    ("price", "Price"),
    ("discount_price", "Discount Price"),
    ("status", "Status"),
    ("images", "Images"),
    ("rating", "Rating"),
    ("reviews_count", "Reviews Count"),
    ("purchased_count", "Purchased Count"),
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
        .select(
            "is_selected, bot_name, greeting, summarized_instruction, "
            "company_info_enabled, company_address, company_phone, company_email, social_handles, "
            "products_services_enabled"
        )
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

    social_handles = row.get("social_handles") if bot_table == "information_bot" else None

    return {
        "bot_table": bot_table,
        "history_table": _HISTORY_TABLES[bot_table],
        "bot_name": row.get("bot_name") or "",
        "greeting": row.get("greeting") or "",
        "summarized_instruction": row.get("summarized_instruction") or "",
        # Information Agent only — used to conditionally inject the Company
        # Info / Products & Services sections into the prompt (see
        # _build_company_info_section / _build_products_services_section).
        "company_info_enabled": bot_table == "information_bot" and bool(row.get("company_info_enabled")),
        "products_services_enabled": bot_table == "information_bot" and bool(row.get("products_services_enabled")),
        "company_address": row.get("company_address") or "" if bot_table == "information_bot" else "",
        "company_phone": row.get("company_phone") or "" if bot_table == "information_bot" else "",
        "company_email": row.get("company_email") or "" if bot_table == "information_bot" else "",
        "social_handles": social_handles if isinstance(social_handles, list) else [],
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


def _build_company_info_section(bot: dict[str, Any], prompt_config: dict[str, str]) -> str:
    """Build the Company Info system-prompt section (Contact Details +
    Links/References) from the active bot's own row, framed by the
    contact_info_prompt / links_references_prompt loaded from app_config."""
    blocks: list[str] = []

    contact_prompt = prompt_config.get("contact_info_prompt", "").strip()
    if contact_prompt:
        contact_lines = []
        if bot.get("company_phone"):
            contact_lines.append(f"Phone: {bot['company_phone']}")
        if bot.get("company_email"):
            contact_lines.append(f"Email: {bot['company_email']}")
        if bot.get("company_address"):
            contact_lines.append(f"Address: {bot['company_address']}")
        block = contact_prompt
        if contact_lines:
            block += "\n" + "\n".join(contact_lines)
        blocks.append(block)

    links_prompt = prompt_config.get("links_references_prompt", "").strip()
    if links_prompt:
        link_lines = [
            f"{handle.get('platform')}: {handle.get('url')}"
            for handle in bot.get("social_handles") or []
            if isinstance(handle, dict) and handle.get("platform") and handle.get("url")
        ]
        block = links_prompt
        if link_lines:
            block += "\n" + "\n".join(link_lines)
        blocks.append(block)

    return "\n\n".join(blocks)


async def _fetch_products_services_rows() -> list[dict[str, Any]]:
    """Fetch every product/service row for injection into the Information
    Agent's prompt (gated on products_services_enabled by the caller)."""
    try:
        result = (
            _db()
            .table(_PRODUCTS_SERVICES_TABLE)
            .select(", ".join(field for field, _ in _PRODUCT_SERVICE_FIELDS))
            .execute()
        )
        return [row for row in (result.data or []) if isinstance(row, dict)]
    except Exception:
        logger.exception("Failed to fetch products/services for prompt injection")
        return []


def _format_product_service_item(row: dict[str, Any]) -> str:
    lines = [
        f"{label}: {row[field]}"
        for field, label in _PRODUCT_SERVICE_FIELDS
        if row.get(field) not in (None, "")
    ]
    return "\n".join(lines)


def _build_products_services_section(prompt_config: dict[str, str], rows: list[dict[str, Any]]) -> str:
    """Build the Products & Services system-prompt section from the fetched
    rows, framed by the products_services_prompt loaded from app_config."""
    prompt = prompt_config.get("products_services_prompt", "").strip()
    if not prompt:
        return ""

    items = [item for item in (_format_product_service_item(row) for row in rows) if item]
    if not items:
        return prompt
    return prompt + "\n\n" + "\n\n".join(items)


def build_messages(
    summarized_instruction: str,
    prompt_config: dict[str, str],
    history: list[dict[str, str]],
    user_message: str,
    extra_sections: list[str] | None = None,
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
            *(extra_sections or []),
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

    # Information Agent only: inject the Company Info (Contact Details +
    # Links/References) and Products & Services sections, each gated strictly
    # on its own enabled toggle from the active information_bot row.
    extra_sections: list[str] = []
    if bot["bot_table"] == "information_bot":
        if bot["company_info_enabled"]:
            section = _build_company_info_section(bot, prompt_config)
            if section:
                extra_sections.append(section)
        if bot["products_services_enabled"]:
            rows = await _fetch_products_services_rows()
            section = _build_products_services_section(prompt_config, rows)
            if section:
                extra_sections.append(section)

    messages = build_messages(summarized_instruction, prompt_config, history, user_message, extra_sections)
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
