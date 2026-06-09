import datetime
import logging
from typing import Any

from app.db.supabase_client import first_row, supabase, supabase_admin
from app.services.ai import generate_ai_reply
from app.services.vectorizer import search_knowledge_chunks

logger = logging.getLogger("whatsapp")

SINGLETON_ID = 1
MAX_HISTORY_TURNS = 5

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
    "knowledge_context_prompt",
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
    and return its identity/instruction fields."""
    info_row = first_row(
        _db()
        .table("information_bot")
        .select(
            "is_selected, bot_name, greeting, summarized_instruction, "
            "company_info_enabled, company_address, company_phone, company_email, social_handles, "
            "products_services_enabled, knowledge_enabled, enhanced_retrieval_enabled, "
            "conversation_history_enabled"
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
                .select("bot_name, greeting, summarized_instruction, conversation_history_enabled")
                .eq("id", SINGLETON_ID)
                .limit(1)
                .execute()
            )
            or {}
        )

    social_handles = row.get("social_handles") if bot_table == "information_bot" else None

    is_info = bot_table == "information_bot"
    return {
        "bot_table": bot_table,
        "bot_name": row.get("bot_name") or "",
        "greeting": row.get("greeting") or "",
        "summarized_instruction": row.get("summarized_instruction") or "",
        "company_info_enabled": is_info and bool(row.get("company_info_enabled")),
        "products_services_enabled": is_info and bool(row.get("products_services_enabled")),
        "knowledge_enabled": is_info and bool(row.get("knowledge_enabled")),
        "enhanced_retrieval_enabled": is_info and bool(row.get("enhanced_retrieval_enabled")),
        "conversation_history_enabled": bool(row.get("conversation_history_enabled", True)),
        "company_address": row.get("company_address") or "" if is_info else "",
        "company_phone": row.get("company_phone") or "" if is_info else "",
        "company_email": row.get("company_email") or "" if is_info else "",
        "social_handles": social_handles if isinstance(social_handles, list) else [],
    }


def _entry_time(entry: dict[str, Any]) -> datetime.datetime | None:
    """Parse a `conversation` JSON entry's ISO `time` field into a datetime,
    or None if it is missing/unparseable."""
    time_value = entry.get("time")
    if not isinstance(time_value, str):
        return None
    try:
        return datetime.datetime.fromisoformat(time_value)
    except ValueError:
        return None


def is_first_message_of_session(prior_entries: list[dict[str, Any]]) -> bool:
    """A session is one calendar day: the session has just started if none of
    this conversation's prior entries (the JSON array minus the just-appended
    current message) were sent earlier today (UTC), per whatsapp_conversations
    — the same day-based definition used previously, now derived from the
    conversation JSON instead of a separate history table."""
    today = datetime.datetime.utcnow().date()
    for entry in prior_entries:
        if not isinstance(entry, dict):
            continue
        entry_time = _entry_time(entry)
        if entry_time is not None and entry_time.date() == today:
            return False
    return True


def _entry_to_messages(entry: dict[str, Any]) -> list[dict[str, str]]:
    """Convert one `conversation` JSON entry into zero or more {"role",
    "content"} messages, oldest first.

    Normal entries store the inbound user message in `query` and the
    generated reply in `response`. Manual dashboard messages (clients.py
    send_message) are recorded the same way but flagged `manual: True`, with
    the admin's outgoing text in `query` and an empty `response` — i.e. they
    speak AS the assistant, so they surface to the model as assistant turns."""
    query = entry.get("query")
    response = entry.get("response")
    messages: list[dict[str, str]] = []

    if entry.get("manual"):
        if isinstance(query, str) and query.strip():
            messages.append({"role": "assistant", "content": query})
    else:
        if isinstance(query, str) and query.strip():
            messages.append({"role": "user", "content": query})
        if isinstance(response, str) and response.strip():
            messages.append({"role": "assistant", "content": response})

    return messages


def fetch_recent_turns(
    prior_entries: list[dict[str, Any]], turns: int = MAX_HISTORY_TURNS
) -> list[dict[str, str]]:
    """Build the last `turns` user/assistant turns (up to turns*2 messages),
    oldest first, as {"role", "content"} dicts ready to be placed directly
    into the messages array sent to the model — derived straight from
    whatsapp_conversations.conversation, which already holds both AI-handled
    user messages and manually-sent admin messages."""
    messages: list[dict[str, str]] = []
    for entry in prior_entries:
        if isinstance(entry, dict):
            messages.extend(_entry_to_messages(entry))
    return messages[-(turns * 2):]


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


def _build_knowledge_context_section(chunks: list[dict[str, Any]], prompt_config: dict[str, str]) -> str:
    """Format retrieved knowledge chunks as a system-prompt section, framed by
    the knowledge_context_prompt loaded from app_config."""
    context_prompt = prompt_config.get("knowledge_context_prompt", "").strip()
    if not context_prompt:
        return ""
    items: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        chunk_text = (chunk.get("chunk") or "").strip()
        if not chunk_text:
            continue
        source = chunk.get("original_name") or ""
        header = f"Excerpt {i}" + (f" from {source}" if source else "")
        items.append(f"{header}:\n{chunk_text}")
    if not items:
        return context_prompt
    return context_prompt + "\n\n" + "\n\n".join(items)


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
            prompt_config.get("response_format_rules", "").strip(),
            prompt_config.get("answering_guidelines", "").strip(),
            summarized_instruction.strip(),
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


async def generate_bot_reply(
    user_message: str, conversation_data: list[dict[str, Any]]
) -> tuple[str, dict[str, Any]]:
    """Generate the reply for an incoming message from the active bot, using
    whatsapp_conversations.conversation as the sole source of session/greeting
    detection and conversational memory (no separate history tables).

    `conversation_data` is the sender's full conversation JSON array,
    including the just-appended current message as its last entry — so all
    prior entries (everything but the last) form the session/history context,
    and already include both AI-handled user turns and manually-sent admin
    messages (clients.py records those into the same array).

    Returns (reply_text, bot) where `bot` is the active-bot info dict.
    """
    bot = await get_active_bot()
    prior_entries = conversation_data[:-1] if conversation_data else []

    is_new_session = is_first_message_of_session(prior_entries)

    if bot.get("conversation_history_enabled", True):
        history = fetch_recent_turns(prior_entries)
    else:
        history = []

    prompt_config = await _fetch_prompt_config()
    summarized_instruction = bot["summarized_instruction"] or f"You are {bot['bot_name'] or 'a helpful assistant'}."

    # Information Agent only: inject enabled sections into the prompt in spec order:
    # Company Info → Products & Services → Retrieved Knowledge Context.
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
        if bot["knowledge_enabled"]:
            top_k = 10 if bot["enhanced_retrieval_enabled"] else 4
            chunks = await search_knowledge_chunks(
                user_message,
                top_k=top_k,
                enhanced=bool(bot["enhanced_retrieval_enabled"]),
            )
            section = _build_knowledge_context_section(chunks, prompt_config)
            if section:
                extra_sections.append(section)

    messages = build_messages(summarized_instruction, prompt_config, history, user_message, extra_sections)
    reply = await generate_ai_reply(messages)

    if is_new_session and bot["greeting"]:
        reply = f"{bot['greeting']}\n\n{reply}"

    return reply, bot
