"""Sales Bot conversational purchase flow (intent detection).

Additive layer on top of the existing Sales Bot pipeline. It runs ONLY when a
customer types a free-text message that the existing flow would otherwise have
bounced straight back to the main menu — i.e. not a greeting, not an exit word,
and not inside an existing step (``ai_qa`` / ``awaiting_*`` / ``shopping``).

This module owns the *new* logic only: classifying the message as a shopping
intent vs. an unrelated one, and (when shopping) picking matching categories /
products from the live catalog. The actual rendering (product cards, category
lists) and the cart → checkout → Razorpay payment path are reused as-is from
``handler.py``; nothing in the existing welcome / Open Store Flow / Menu / Talk
to AI flows is touched.

Detection is a two-step hybrid:
  1. A fast keyword pre-match against the catalog (exact product/category
     mentions) — high precision, no model cost.
  2. An LLM (``gpt-5-nano-2025-08-07``, same model the rest of the bot uses) for
     semantic intent ("something for the gym") and the buy-vs-unrelated call.

The LLM's category/product picks are always validated against the real catalog,
so it can never surface a product or category that doesn't exist.
"""

import asyncio
import json
import logging
from typing import Any, cast

from openai import OpenAI
from openai.types.responses import ResponseInputParam

from app.core.config import settings
from app.services.sales import catalog
from app.services.sales import messaging as m

logger = logging.getLogger("whatsapp")

MODEL = "gpt-5-nano-2025-08-07"

# Exact wording requested for general/unrelated messages.
SORRY_MESSAGE = "Sorry, I am programmed to assist in guiding you to buy products."

# How many product/category candidates to consider, and how much catalog to
# show the classifier.
_MAX_SUGGESTIONS = 6
_MAX_CATALOG_LINES = 80
_HISTORY_TURNS = 3


def _cat_of(row: dict[str, Any]) -> str:
    return (row.get("category") or "").strip() or "Other"


# ── Keyword pre-match ────────────────────────────────────────────────────────
def _keyword_match(query: str, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    """Fast exact-ish match: returns (product_matches, matched_category). An exact
    category-name match wins (so we can jump straight into that category)."""
    q = (query or "").strip().lower()
    if not q:
        return [], None
    cats = {_cat_of(r).lower(): _cat_of(r) for r in rows}
    if q in cats:
        return [], cats[q]
    matches = [
        r for r in rows
        if q in f"{r.get('name', '')} {r.get('category', '')} {r.get('description', '')}".lower()
    ]
    return matches, None


# ── LLM classification ───────────────────────────────────────────────────────
def _catalog_digest(rows: list[dict[str, Any]], currency: str) -> str:
    lines: list[str] = []
    for r in rows[:_MAX_CATALOG_LINES]:
        price = m.format_money(r.get("price_minor") or 0, r.get("currency") or currency)
        stock = r.get("stock_quantity")
        avail = "out of stock" if (stock is not None and int(stock) <= 0) else "available"
        lines.append(f"{r.get('id')} | {r.get('name')} | {_cat_of(r)} | {price} | {avail}")
    return "\n".join(lines)


def _recent_history(conversation_data: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The last few user/assistant turns (excluding the just-appended current
    message), so follow-ups like 'show me more' or 'the red one' have context."""
    prior = conversation_data[:-1] if conversation_data else []
    msgs: list[dict[str, str]] = []
    for entry in prior[-_HISTORY_TURNS:]:
        if not isinstance(entry, dict):
            continue
        q = entry.get("query")
        resp = entry.get("response")
        if isinstance(q, str) and q.strip():
            msgs.append({"role": "user", "content": q})
        if isinstance(resp, str) and resp.strip():
            msgs.append({"role": "assistant", "content": resp})
    return msgs


def _build_messages(
    message: str, history: list[dict[str, str]], rows: list[dict[str, Any]], cfg: dict[str, Any]
) -> list[dict[str, str]]:
    categories = sorted({_cat_of(r) for r in rows})
    digest = _catalog_digest(rows, cfg.get("currency") or "INR")
    system = (
        "You are the intent classifier for a WhatsApp shopping assistant. Decide whether the "
        "customer's latest message expresses an intent to buy or browse products from the catalog, "
        "or is unrelated.\n\n"
        "Return ONLY a JSON object with keys: intent, category, product_ids, reply.\n"
        '- intent: "buy" if they mention or ask for a specific product or type of product; '
        '"browse" if they want to shop but are vague (e.g. "I want to buy something", "show me what you have"); '
        '"unrelated" if the message has nothing to do with shopping for these products.\n'
        "- category: the single best-matching category name from the list below, or null.\n"
        "- product_ids: up to 5 ids of catalog products matching their interest, most relevant first; [] if none.\n"
        "- reply: one short friendly WhatsApp line (max 160 chars) introducing the suggestions; "
        'empty string "" if unrelated.\n'
        "Use ONLY category names and product ids that appear EXACTLY in the catalog below. Never invent them.\n\n"
        f"CATEGORIES:\n{', '.join(categories) or '(none)'}\n\n"
        f"CATALOG (id | name | category | price | availability):\n{digest or '(no products)'}"
    )
    msgs: list[dict[str, str]] = [{"role": "system", "content": system}]
    msgs.extend(history)
    msgs.append({"role": "user", "content": message})
    return msgs


def _parse_json(raw: str) -> dict[str, Any] | None:
    s = (raw or "").strip()
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(s[start : end + 1])
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def _classify_llm(messages: list[dict[str, str]]) -> dict[str, Any] | None:
    if settings.openai_api_key.strip() == "":
        return None
    try:
        client = OpenAI(api_key=settings.openai_api_key)
        input_messages = [
            {"role": "developer" if mm["role"] == "system" else mm["role"], "content": mm["content"]}
            for mm in messages
        ]
        response = await asyncio.to_thread(
            client.responses.create,
            model=MODEL,
            input=cast(ResponseInputParam, input_messages),
            reasoning={"effort": "minimal"},
            text={"verbosity": "low"},
        )
        return _parse_json(response.output_text or "")
    except Exception:
        logger.exception("Conversational classify: LLM call failed")
        return None


# ── Public entry point ───────────────────────────────────────────────────────
async def classify(message: str, conversation_data: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    """Classify a free-text message into a shopping decision.

    Returns ``{"intent", "category", "product_ids", "reply"}`` where ``intent``
    is one of ``"buy" | "browse" | "unrelated"``, ``category`` is a real catalog
    category (or None), ``product_ids`` are real active product ids, and ``reply``
    is a short intro line (may be empty). All catalog references are validated, so
    callers can use them directly.
    """
    result: dict[str, Any] = {"intent": "unrelated", "category": None, "product_ids": [], "reply": ""}
    raw = (message or "").strip()
    if not raw:
        return result

    rows = catalog.fetch_active_rows()
    if not rows:
        # No products at all — let the caller fall back to its empty-catalog path.
        return {"intent": "browse", "category": None, "product_ids": [], "reply": ""}

    valid_ids = {str(r["id"]) for r in rows if r.get("id")}
    cats_by_lower = {_cat_of(r).lower(): _cat_of(r) for r in rows}

    # 1) Keyword pre-match — exact mentions are reliable, take precedence.
    kw_matches, kw_cat = _keyword_match(raw, rows)
    if kw_cat:
        return {"intent": "buy", "category": kw_cat, "product_ids": [], "reply": ""}
    if kw_matches:
        ids = [str(r["id"]) for r in kw_matches[:_MAX_SUGGESTIONS]]
        return {"intent": "buy", "category": None, "product_ids": ids, "reply": ""}

    # 2) LLM semantic classification (validated against the real catalog).
    history = _recent_history(conversation_data)
    llm = await _classify_llm(_build_messages(raw, history, rows, cfg))
    if not llm:
        # Model unavailable and no keyword hit — can't confirm intent; nudge to browse.
        return {"intent": "browse", "category": None, "product_ids": [], "reply": ""}

    intent = llm.get("intent") if llm.get("intent") in ("buy", "browse", "unrelated") else "unrelated"

    category = llm.get("category")
    category = cats_by_lower.get(category.lower()) if isinstance(category, str) else None

    product_ids = [str(pid) for pid in (llm.get("product_ids") or []) if str(pid) in valid_ids][:_MAX_SUGGESTIONS]
    reply = llm.get("reply") if isinstance(llm.get("reply"), str) else ""

    if intent == "unrelated" and not product_ids and not category:
        return {"intent": "unrelated", "category": None, "product_ids": [], "reply": ""}
    if product_ids or category:
        return {"intent": "buy", "category": category, "product_ids": product_ids, "reply": reply}
    return {"intent": "browse", "category": None, "product_ids": [], "reply": reply}
