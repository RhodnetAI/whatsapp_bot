import asyncio
from typing import Any, cast

from openai import OpenAI

from app.core.config import settings


async def generate_ai_reply(messages_for_ai: list[dict[str, str]]) -> str:
    if settings.openai_api_key.strip() == "":
        return "I'm sorry, the AI service is not configured yet."

    try:
        client = OpenAI(api_key=settings.openai_api_key)
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o-mini",
            messages=cast(Any, messages_for_ai),
        )
        ai_content = getattr(response.choices[0].message, "content", "")
        return ai_content.strip() if isinstance(ai_content, str) else ""
    except Exception:
        return "I'm sorry, I'm having trouble responding right now. Please try again later."
