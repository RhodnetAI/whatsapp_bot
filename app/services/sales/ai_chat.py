"""Sales Bot 'Talk to AI' — prompt assembly + reply generation.

Mirrors the Information Bot's "answer from instructions and company info"
approach (see ``app.services.bot_chat``: ``get_active_bot`` /
``_fetch_prompt_config`` / ``_build_company_info_section`` / ``build_messages``
/ ``generate_bot_reply``) — same system-prompt structure (response format rules
+ answering guidelines + the bot's own summarized instruction + Company Info),
same history replay, and the same ``generate_ai_reply`` model call.

This module is fully self-contained for the Sales Bot: it reads the
``sales_bot`` singleton row directly (its own ``summarized_instruction`` /
``company_info_enabled`` / ``company_address`` / ``company_phone`` /
``company_email`` / ``social_handles``), never information_bot's. The shared
``app_config`` prompt-formatting building blocks (response format rules,
answering guidelines, contact/links framing, history header/footer) are
platform-level copy and are read the same way the Information Bot reads them.
"""

import logging
from typing import Any

from app.db.supabase_client import first_row, supabase, supabase_admin
from app.services.ai import generate_ai_reply

logger = logging.getLogger("whatsapp")

SINGLETON_ID = 1
MAX_HISTORY_TURNS = 5

_PROMPT_CONFIG_KEYS = (
    "answering_guidelines",
    "response_format_rules",
    "history_header",
    "history_footer",
    "links_references_prompt",
    "contact_info_prompt",
)


def _db():
    return supabase_admin if supabase_admin is not None else supabase


async def _fetch_prompt_config() -> dict[str, str]:
    """Fetch the shared prompt-assembly building blocks from app_config by key
    (same platform-level rows the Information Bot reads)."""
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
        logger.exception("Sales AI: failed to fetch prompt config from app_config")
    return values


async def get_sales_bot_context() -> dict[str, Any]:
    """The Sales Bot's own identity/instructions/company-info fields — always
    reads the ``sales_bot`` singleton row (never information_bot's)."""
    row = (
        first_row(
            _db()
            .table("sales_bot")
            .select(
                "bot_name, summarized_instruction, company_info_enabled, "
                "company_address, company_phone, company_email, social_handles, "
                "conversation_history_enabled"
            )
            .eq("id", SINGLETON_ID)
            .limit(1)
            .execute()
        )
        or {}
    )
    social_handles = row.get("social_handles")
    return {
        "bot_name": row.get("bot_name") or "",
        "summarized_instruction": row.get("summarized_instruction") or "",
        "company_info_enabled": bool(row.get("company_info_enabled")),
        "company_address": row.get("company_address") or "",
        "company_phone": row.get("company_phone") or "",
        "company_email": row.get("company_email") or "",
        "social_handles": social_handles if isinstance(social_handles, list) else [],
        "conversation_history_enabled": bool(row.get("conversation_history_enabled", True)),
    }


def _build_company_info_section(bot: dict[str, Any], prompt_config: dict[str, str]) -> str:
    """Company Info system-prompt section (Contact Details + Links/References),
    built from the Sales Bot's own row — same construction as
    ``bot_chat._build_company_info_section``."""
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


# ── Conversation history (same shape as bot_chat) ────────────────────────────
def _entry_to_messages(entry: dict[str, Any]) -> list[dict[str, str]]:
    """Convert one ``conversation`` JSON entry into zero or more {"role",
    "content"} messages, oldest first — same mapping as
    ``bot_chat._entry_to_messages``."""
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
    """Build the last ``turns`` user/assistant turns (up to turns*2 messages),
    oldest first — same as ``bot_chat.fetch_recent_turns``."""
    messages: list[dict[str, str]] = []
    for entry in prior_entries:
        if isinstance(entry, dict):
            messages.extend(_entry_to_messages(entry))
    return messages[-(turns * 2):]


# ── Prompt assembly & generation ─────────────────────────────────────────────
def build_messages(
    summarized_instruction: str,
    prompt_config: dict[str, str],
    history: list[dict[str, str]],
    user_message: str,
    extra_sections: list[str] | None = None,
) -> list[dict[str, str]]:
    """Assemble the messages array the same way ``bot_chat.build_messages``
    does: system message (response format rules + answering guidelines + the
    bot's own instruction + extra sections), replayed history framed by the
    configured header/footer, then the current user message."""
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


async def generate_sales_ai_reply(
    user_message: str,
    conversation_data: list[dict[str, Any]],
    extra_sections: list[str] | None = None,
) -> str:
    """Generate a Talk-to-AI reply using the Sales Bot's own instructions and
    company info, following the same prompt-assembly + model-call approach as
    ``bot_chat.generate_bot_reply``.

    ``conversation_data`` is the sender's full conversation JSON array,
    including the just-appended current message as its last entry — prior
    entries (everything but the last) form the replayed history, mirroring
    ``generate_bot_reply``.

    ``extra_sections`` lets the caller append Sales-specific context (e.g. the
    product catalog) after the Company Info section, in the same "extra
    sections" slot ``generate_bot_reply`` uses for Information-Bot sections.
    """
    bot = await get_sales_bot_context()
    prior_entries = conversation_data[:-1] if conversation_data else []

    if bot["conversation_history_enabled"]:
        history = fetch_recent_turns(prior_entries)
    else:
        history = []

    prompt_config = await _fetch_prompt_config()
    summarized_instruction = bot["summarized_instruction"] or f"You are {bot['bot_name'] or 'a helpful assistant'}."

    sections: list[str] = []
    if bot["company_info_enabled"]:
        section = _build_company_info_section(bot, prompt_config)
        if section:
            sections.append(section)
    sections.extend(extra_sections or [])

    messages = build_messages(summarized_instruction, prompt_config, history, user_message, sections)
    return await generate_ai_reply(messages)
