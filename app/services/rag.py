import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Callable, TypeVar

from openai import OpenAI
from app.services.ai import generate_ai_reply
from app.services.vectorizer import search_vectors
from app.core.config import settings

try:
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_groq import ChatGroq  # type: ignore[import]
except ImportError:  # pragma: no cover
    HumanMessage = None
    SystemMessage = None
    ChatGroq = None

try:
    from langsmith import traceable
except ImportError:  # pragma: no cover
    F = TypeVar("F", bound=Callable[..., Any])

    def _fallback_traceable(*args: Any, **kwargs: Any) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            return func
        return decorator

    traceable = _fallback_traceable  # type: ignore[assignment]


@dataclass
class IntentOnly:
    source_filter: str = "both"


@dataclass
class QueryIntent(IntentOnly):
    rewritten_query: str = ""


_INTENT_ONLY_SYSTEM_PROMPT = (
    "Classify which knowledge source is needed to answer a user query.\n"
    "Respond with JSON only, no other text.\n\n"
    "source_filter options:\n"
    "- rag: question about uploaded documents\n"
    "- web: question about website content\n"
    "- both: unclear which source\n"
    "- general: no knowledge base needed (greetings, small talk, answerable from history)\n\n"
    "Use 'general' only when certain no company knowledge is needed. When in doubt, use 'both'.\n\n"
    'Response format: {"source_filter": "<rag|web|both|general>"}'
)

_INTENT_COMBINED_SYSTEM_PROMPT = (
    "Classify and rewrite a user query. Respond with JSON only, no other text.\n\n"
    "source_filter options:\n"
    "- rag: question about uploaded documents\n"
    "- web: question about website content\n"
    "- both: unclear which source\n"
    "- general: no knowledge base needed (greetings, small talk, answerable from history)\n\n"
    "rewritten_query: standalone version with all pronouns and implicit references resolved using the chat history. If already standalone, repeat unchanged.\n\n"
    "Use 'general' only when certain no company knowledge is needed. When in doubt, use 'both'.\n\n"
    'Response format: {"source_filter": "<rag|web|both|general>", "rewritten_query": "<query>"}'
)

_GREETING_RE = re.compile(
    r"^\s*(hi+|hello+|hey+|greetings|howdy|sup|good\s+(morning|afternoon|evening|day|night)|"
    r"what'?s\s+up|thanks?|thank\s+you|ty|cheers|bye+|goodbye|see\s+ya?|"
    r"ok|okay|sure|cool|got\s+it|sounds?\s+good|great|nice|perfect|alright|alrite)\W*\s*$",
    re.IGNORECASE,
)


def _is_obvious_greeting(message: str) -> bool:
    stripped = message.strip()
    if _GREETING_RE.match(stripped):
        return True
    words = stripped.split()
    return len(words) <= 2 and "?" not in stripped


def _sanitize_json(response_text: str) -> str:
    return response_text.strip().split("\n")[-1].strip()


async def _classify_and_rewrite_query(user_message: str, chat_history: Optional[List[Dict[str, Any]]] = None) -> QueryIntent:
    if _is_obvious_greeting(user_message):
        return QueryIntent(source_filter="general", rewritten_query=user_message)

    history_block = ""
    if chat_history:
        lines: List[str] = []
        for turn in chat_history[-4:]:
            user_msg = turn.get("query") or ""
            bot_msg = turn.get("response") or ""
            lines.append(f"User:{user_msg}")
            lines.append(f"Bot:{bot_msg}")
        history_block = "\n".join(lines)

    human_content = (
        f"History:\n{history_block}\nQuery:{user_message}" if history_block else f"Query:{user_message}"
    )

    if ChatGroq and settings.groq_api_key.strip() != "":
        try:
            groq = ChatGroq(model="llama-3.1-8b-instant", temperature=0, api_key=settings.groq_api_key)
            if HumanMessage and SystemMessage:
                result = await groq.ainvoke(
                    [SystemMessage(content=_INTENT_COMBINED_SYSTEM_PROMPT), HumanMessage(content=human_content)]
                )
                parsed = getattr(result, "parsed", None) or {}
                return QueryIntent(
                    source_filter=parsed.get("source_filter", "both"),
                    rewritten_query=parsed.get("rewritten_query", user_message),
                )
        except Exception:
            pass

    if settings.openai_api_key.strip() == "":
        return QueryIntent(source_filter="both", rewritten_query=user_message)

    client = OpenAI(api_key=settings.openai_api_key)
    response = await asyncio.to_thread(
        client.chat.completions.create,
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": _INTENT_COMBINED_SYSTEM_PROMPT},
            {"role": "user", "content": human_content},
        ],
    )
    content = getattr(response.choices[0].message, "content", "") or ""
    payload = _sanitize_json(content)
    try:
        parsed = json.loads(payload)
        return QueryIntent(
            source_filter=parsed.get("source_filter", "both"),
            rewritten_query=parsed.get("rewritten_query", user_message),
        )
    except Exception:
        return QueryIntent(source_filter="both", rewritten_query=user_message)


def _build_knowledge_prompt(query: str, chunks: List[dict[str, Any]], setup_config: dict[str, str]) -> List[dict[str, str]]:
    instruction_lines = [
        "You are a knowledge-based assistant.",
        "Answer the user's question only from the provided document excerpts.",
        "If the exact answer is not contained in the excerpts, reply with: 'I don't have enough information to answer that.'",
        "Do not hallucinate or invent information.",
        "Cite only the provided excerpts and keep the answer concise.",
    ]

    if setup_config.get("main_instruction"):
        instruction_lines.append(f"Main instruction: {setup_config['main_instruction']}")
    if setup_config.get("dos"):
        instruction_lines.append(f"Do: {setup_config['dos']}")
    if setup_config.get("donts"):
        instruction_lines.append(f"Don't: {setup_config['donts']}")

    source_text = []
    for index, chunk in enumerate(chunks, start=1):
        source_text.append(
            f"Excerpt {index} from {chunk.get('original_name', 'document')}:\n{chunk.get('chunk', '')}"
        )

    user_message = (
        "Use only the excerpts below to answer the query. Do not use any outside knowledge." +
        "\n\n" + "\n\n".join(source_text) + f"\n\nUser query: {query}"
    )

    return [
        {"role": "system", "content": "\n".join(instruction_lines)},
        {"role": "user", "content": user_message},
    ]


def _build_general_prompt(query: str, setup_config: dict[str, str]) -> List[dict[str, str]]:
    instruction_lines = [
        "You are a helpful assistant.",
        "Answer the user's question directly and politely.",
    ]

    if setup_config.get("main_instruction"):
        instruction_lines.append(f"Main instruction: {setup_config['main_instruction']}")
    if setup_config.get("dos"):
        instruction_lines.append(f"Do: {setup_config['dos']}")
    if setup_config.get("donts"):
        instruction_lines.append(f"Don't: {setup_config['donts']}")

    return [
        {"role": "system", "content": "\n".join(instruction_lines)},
        {"role": "user", "content": query},
    ]


@traceable(name="rag_retrieval", run_type="retriever")
async def answer_query_from_rag(query: str, setup_config: dict[str, str] | None = None) -> str:
    setup_config = setup_config or {}
    intent = await _classify_and_rewrite_query(query)

    if intent.source_filter == "general":
        return await generate_ai_reply(_build_general_prompt(intent.rewritten_query, setup_config))

    chunks = await search_vectors(intent.rewritten_query, top_k=4)
    if not chunks:
        return "I don't have enough information to answer that from the uploaded documents."

    messages_for_ai = _build_knowledge_prompt(intent.rewritten_query, chunks, setup_config)
    return await generate_ai_reply(messages_for_ai)
