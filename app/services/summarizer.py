import asyncio
import hashlib
import logging
from typing import Any

from openai import OpenAI

from app.core.config import settings
from app.db.supabase_client import first_row, supabase, supabase_admin

logger = logging.getLogger("whatsapp")

_FALLBACK_SUMMARIZER_PROMPT = (
    "Condense the provided instructions into a minimal system prompt.\n\n"
    "Requirements:\n"
    "- Preserve all rules, constraints, and behavioral requirements.\n"
    "- Preserve all factual information and key details.\n"
    "- Remove repetition, filler, explanations, and examples.\n"
    "- Keep important structured information (services, features, pricing, policies).\n"
    "- Do not omit facts required to answer user questions.\n"
    "- Compress wording while keeping meaning intact.\n\n"
    "Output format:\n"
    "- Clear sections if present in the source.\n"
    "- Bullet points for lists.\n"
    "- Short sentences for descriptions.\n\n"
    "Do not:\n"
    "- Add new rules or interpretations.\n"
    "- Change the meaning of the instructions.\n"
    "- Introduce external knowledge.\n\n"
    "The output must be directly usable as a system prompt for a chat model."
)


def _db():
    return supabase_admin if supabase_admin is not None else supabase


def _fetch_summarizer_prompt() -> str:
    """Fetch the summarizer system prompt from app_config. Falls back to hardcoded default."""
    try:
        result = (
            _db()
            .table("app_config")
            .select("value")
            .eq("key", "summarization_prompt")
            .limit(1)
            .execute()
        )
        row = first_row(result)
        if row and row.get("value"):
            logger.info("[summarize] loaded summarization_prompt from app_config")
            return str(row["value"])
    except Exception:
        logger.exception("[summarize] failed to fetch summarization_prompt from app_config")
    logger.info("[summarize] using fallback summarization prompt")
    return _FALLBACK_SUMMARIZER_PROMPT


def build_raw_instructions(data: dict[str, Any]) -> str:
    parts = []

    bot_name = (data.get("bot_name") or "").strip()
    if bot_name:
        parts.append(f"Bot Name:\n{bot_name}")

    instructions = (data.get("main_instruction") or "").strip()
    if instructions:
        parts.append(f"Instructions:\n{instructions}")

    dos = (data.get("dos") or "").strip()
    donts = (data.get("donts") or "").strip()
    if dos or donts:
        parts.append(f"Do's:\n{dos}\nDon'ts:\n{donts}")

    return "\n\n".join(p for p in parts if p).strip()


def hash_raw_instructions(raw_text: str) -> str:
    normalized = (raw_text or "").strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def generate_summarized_instruction(raw_text: str) -> str:
    if not settings.openai_api_key.strip():
        logger.warning("[summarize] OPENAI_API_KEY not configured, skipping summarization")
        return raw_text

    system_prompt = await asyncio.to_thread(_fetch_summarizer_prompt)

    try:
        logger.info("[summarize] calling OpenAI (raw_chars=%d)", len(raw_text))
        client = OpenAI(api_key=settings.openai_api_key)
        response = await asyncio.to_thread(
            client.responses.create,
            model="gpt-5-nano-2025-08-07",
            instructions=system_prompt,
            input=raw_text,
            reasoning={"effort": "minimal"},
            text={"verbosity": "low"},
        )
        content = response.output_text
        if isinstance(content, str):
            logger.info("[summarize] OpenAI summarization succeeded (summary_chars=%d)", len(content))
            return content.strip()
        logger.warning("[summarize] OpenAI returned non-string output, using raw text")
        return raw_text
    except Exception:
        logger.exception("[summarize] OpenAI summarization failed, using raw text")
        return raw_text
