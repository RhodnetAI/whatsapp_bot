import asyncio

from openai import OpenAI

from app.core.config import settings


async def generate_ai_reply(messages_for_ai: list[dict[str, str]]) -> str:
    if settings.openai_api_key.strip() == "":
        return "I'm sorry, the AI service is not configured yet."

    try:
        client = OpenAI(api_key=settings.openai_api_key)
        input_messages = [
            {"role": "developer" if m["role"] == "system" else m["role"], "content": m["content"]}
            for m in messages_for_ai
        ]
        response = await asyncio.to_thread(
            client.responses.create,
            model="gpt-5-nano-2025-08-07",
            input=input_messages,
            reasoning_effort="minimal",
            verbosity="low",
        )
        return (response.output_text or "").strip()
    except Exception:
        return "I'm sorry, I'm having trouble responding right now. Please try again later."
