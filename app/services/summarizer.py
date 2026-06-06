import asyncio
import hashlib
from typing import Any

from openai import OpenAI

from app.core.config import settings


def build_raw_instructions(data: dict[str, Any]) -> str:
    parts = []

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
        return raw_text

    try:
        client = OpenAI(api_key=settings.openai_api_key)
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a system prompt optimizer. Take a raw set of instructions, do's, "
                        "and don'ts for an AI assistant and condense them into a clear, concise, "
                        "effective system prompt. Preserve all key rules, behaviors, and constraints. "
                        "Output only the final system prompt text, nothing else."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Summarize and optimize the following instructions:\n\n{raw_text}",
                },
            ],
        )
        content = getattr(response.choices[0].message, "content", "")
        return content.strip() if isinstance(content, str) else raw_text
    except Exception:
        return raw_text
