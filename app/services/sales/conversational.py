"""Sales Bot — Path C: fully conversational, text-only purchase flow (LLM layer).

This module owns ONLY the language understanding for Path C:

* ``classify_entry`` — is an idle free-text message a buying intent or unrelated?
* ``interpret_turn`` — the single per-turn interpreter. Given the current step,
  the message, the session state and a catalog digest, it returns one structured
  ``Decision`` describing what the customer wants (select / set or change quantity
  / remove / replace / add more / go back / answer a doubt / confirm / cancel /
  unrelated). Every product id / category it returns is validated against the live
  catalog, so it can never invent one.

All *rendering* (plain-text categories / products / cart / prompts), *state*
mutation, the cart, orders and payment live in ``handler.py`` and reuse the
existing services. Nothing here emits WhatsApp messages or buttons. See
``docs/Conversational Sales Flow (Path C).md`` (§14).
"""

import asyncio
import json
import logging
import re
from typing import Any, cast

from openai import OpenAI
from openai.types.responses import ResponseInputParam

from app.core.config import settings
from app.services import prompt_config
from app.services.sales import catalog
from app.services.sales import messaging as m

logger = logging.getLogger("whatsapp")

MODEL = "gpt-5-nano-2025-08-07"

# Exact wording for general / unrelated messages (entry and in-flow).
SORRY_MESSAGE = "I am programmed to help you in buying products."

_INTENTS = {
    "select", "set_quantity", "change_quantity", "remove", "replace",
    "add_more", "go_back", "answer_doubt", "confirm", "cancel", "unrelated",
}
_TARGET_STEPS = {"categories": "conv_categories", "products": "conv_products", "confirm": "conv_confirm"}

_MAX_CATALOG_LINES = 120
_MAX_SHOWN = 40
_HISTORY_TURNS = 4

# ── Prompt fallbacks (live text in app_config; [[TOKEN]]s filled in at runtime) ─
_DEFAULT_ENTRY_PROMPT = (
    "You decide if a WhatsApp customer's message shows intent to buy or browse products, "
    "or asks for help shopping (answer 'buy'), versus an unrelated/general message (answer "
    "'unrelated'). Reply with ONLY a JSON object: {\"intent\": \"buy\" | \"unrelated\"}.\n\n"
    "We sell (id | name | category | price | availability):\n[[CATALOG]]"
)

_DEFAULT_INTERPRETER_PROMPT = (
    "You interpret a customer's message during a WhatsApp shopping conversation and output a "
    "SINGLE strict JSON object describing what they want. Do not add any prose outside the JSON.\n\n"
    "CURRENT STEP: [[STEP]]  (categories → products → confirm/cart → address → payment)\n"
    "SESSION:\n[[SESSION]]\n\n"
    "[[SHOWN]]\n\n"
    "CATALOG (id | name | category | price | availability):\n[[CATALOG]]\n"
    "CATEGORIES: [[CATEGORIES]]\n\n"
    "Output JSON with these keys (include only what's relevant; omit or empty otherwise):\n"
    '  "intent": one of select | set_quantity | change_quantity | remove | replace | add_more | '
    "go_back | answer_doubt | confirm | cancel | unrelated\n"
    '  "categories": [catalog category names the user referenced]\n'
    '  "items": [{"product_id": "<name, shown number, or id>", "quantity": <int or null if unstated>}]\n'
    '  "quantity_updates": [{"product_id": "<name/number/id>", "quantity": <int>}]  (change qty of a cart item)\n'
    '  "removals": ["<name/number/id>"]\n'
    '  "replacements": [{"from_id": "<name/number/id>", "to_id": "<name/number/id>", "quantity": <int or null>}]\n'
    '  "target_step": "categories" | "products" | "confirm"   (only for go_back)\n'
    '  "answer": "<concise friendly WhatsApp answer using ONLY catalog facts>"  (only for answer_doubt)\n\n'
    "Rules:\n"
    "- Use ONLY products/categories from the catalog above. Refer to a product by its exact name "
    "or its shown number (preferred) — never invent one.\n"
    "- When they name or refer to a product (by name, number, or description), put it in \"items\"; "
    "set quantity to null if they didn't state one.\n"
    "- Only include in \"items\"/\"removals\"/\"quantity_updates\" the products the user is changing in "
    "THIS message. Do NOT echo items already in the cart that they didn't mention.\n"
    "- A \"quantity\" in items is the TOTAL desired for that line, not an amount to add.\n"
    "- intent=answer_doubt for any question/comparison/clarification (about products, categories, "
    "prices, their order); put the full answer in \"answer\". Do NOT change their selections.\n"
    "- intent=confirm ONLY when they clearly approve proceeding (e.g. 'confirm', 'yes go ahead', "
    "'checkout', 'that's all'). Casual chit-chat like 'lol', 'ok cool', 'nice' is intent=unrelated, "
    "NOT confirm.\n"
    "- intent=cancel when they want to stop buying.\n"
    "- intent=unrelated when the message is off-topic (not about shopping or their order).\n"
    "- set_quantity when they give a number that answers the pending quantity question.\n"
    "- Interpret freely — any phrasing/structure/style. React to meaning, not keywords."
)

_DEFAULT_ADDRESS_PROMPT = (
    "Extract delivery address fields from the customer's message. Return ONLY a JSON object: "
    '{"name": "", "address": "", "city": "", "pincode": ""}. Use an empty string for any field '
    "not present. \"address\" is the house/street line. Do not invent values."
)


def _cat_of(row: dict[str, Any]) -> str:
    return (row.get("category") or "").strip() or "Other"


def _coerce_qty(value: Any) -> int | None:
    try:
        q = int(value)
    except (TypeError, ValueError):
        return None
    return q if q >= 1 else None


_WORD_NUMS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "couple": 2, "pair": 2,
}


def _first_int(message: str) -> int | None:
    """First quantity in free text — a digit or a small number word."""
    m = re.search(r"\b(\d+)\b", message or "")
    if m:
        return int(m.group(1))
    for word in re.findall(r"[a-z]+", (message or "").lower()):
        if word in _WORD_NUMS:
            return _WORD_NUMS[word]
    return None


# ── Catalog helpers (shared with handler rendering, single source of order) ──
def active_categories() -> list[str]:
    return catalog.distinct_active_categories(catalog.fetch_active_rows())


def products_in_categories(categories: list[str]) -> list[dict[str, Any]]:
    """Active products whose category is in ``categories`` (case-insensitive),
    in catalog order. Same ordering the handler renders, so display numbers line
    up with the numbered list given to the interpreter."""
    wanted = {(c or "").strip().lower() for c in (categories or [])}
    return [r for r in catalog.fetch_active_rows() if _cat_of(r).lower() in wanted]


def _availability(row: dict[str, Any]) -> str:
    stock = row.get("stock_quantity")
    if stock is None:
        return "available"
    return "out of stock" if int(stock) <= 0 else f"{int(stock)} in stock"


def _catalog_digest(rows: list[dict[str, Any]], currency: str) -> str:
    lines = []
    for r in rows[:_MAX_CATALOG_LINES]:
        price = m.format_money(r.get("price_minor") or 0, r.get("currency") or currency)
        lines.append(f"{r.get('id')} | {r.get('name')} | {_cat_of(r)} | {price} | {_availability(r)}")
    return "\n".join(lines)


# ── LLM plumbing ─────────────────────────────────────────────────────────────
def _parse_json(raw: str) -> dict[str, Any] | None:
    s = (raw or "").strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(s[start : end + 1])
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def _call_llm(messages: list[dict[str, str]], effort: str = "minimal") -> dict[str, Any] | None:
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
            reasoning=cast(Any, {"effort": effort}),
            text=cast(Any, {"verbosity": "low"}),
        )
        return _parse_json(response.output_text or "")
    except Exception:
        logger.exception("Path C: LLM call failed")
        return None


def _recent_history(conversation_data: list[dict[str, Any]]) -> list[dict[str, str]]:
    prior = conversation_data[:-1] if conversation_data else []
    msgs: list[dict[str, str]] = []
    for entry in prior[-_HISTORY_TURNS:]:
        if not isinstance(entry, dict):
            continue
        q, resp = entry.get("query"), entry.get("response")
        if isinstance(q, str) and q.strip():
            msgs.append({"role": "user", "content": q})
        if isinstance(resp, str) and resp.strip():
            msgs.append({"role": "assistant", "content": resp})
    return msgs


# ── Entry classifier (idle → buy vs unrelated) ───────────────────────────────
def _keyword_is_shopping(message: str, rows: list[dict[str, Any]]) -> bool:
    q = (message or "").strip().lower()
    if not q:
        return False
    if any(_cat_of(r).lower() in q or q in _cat_of(r).lower() for r in rows):
        return True
    return any(q in (r.get("name") or "").lower() or (r.get("name") or "").lower() in q for r in rows)


async def classify_entry(message: str, conversation_data: list[dict[str, Any]], cfg: dict[str, Any]) -> str:
    """Return ``"buy"`` if an idle free-text message shows buying interest or asks
    for help shopping, else ``"unrelated"``."""
    raw = (message or "").strip()
    if not raw:
        return "unrelated"
    rows = catalog.fetch_active_rows()
    if rows and _keyword_is_shopping(raw, rows):
        return "buy"

    digest = _catalog_digest(rows, cfg.get("currency") or "INR")
    system = prompt_config.get_prompt("sales_conv_entry_prompt", _DEFAULT_ENTRY_PROMPT).replace(
        "[[CATALOG]]", digest or "(no products)"
    )
    messages = [{"role": "system", "content": system}, *_recent_history(conversation_data),
                {"role": "user", "content": raw}]
    result = await _call_llm(messages)
    intent = (result or {}).get("intent")
    return "buy" if intent == "buy" else "unrelated"


# ── The per-turn interpreter ─────────────────────────────────────────────────
def _state_summary(state: dict[str, Any]) -> str:
    parts = []
    sel = state.get("selected_categories") or []
    parts.append(f"Selected categories: {', '.join(sel) if sel else '(none)'}")
    cart = state.get("cart") or []
    if cart:
        parts.append("Cart: " + "; ".join(f"{i.get('name')} x{i.get('quantity')}" for i in cart))
    else:
        parts.append("Cart: (empty)")
    pending = state.get("pending_quantity") or []
    if pending:
        parts.append("Awaiting a quantity for: " + ", ".join(p.get("name") or "" for p in pending))
    if state.get("order_number"):
        parts.append(f"Order: {state['order_number']}")
    return "\n".join(parts)


def _shown_rows(step: str, state: dict[str, Any]) -> list[dict[str, Any]]:
    """The product rows currently on the customer's screen, in display order (so
    ordinals like 'the 2nd one' resolve to the right product)."""
    if step in ("conv_products", "conv_confirm"):
        return products_in_categories(state.get("selected_categories") or [])[:_MAX_SHOWN]
    return []


def _shown_block(step: str, state: dict[str, Any]) -> str:
    """The numbered list currently on the customer's screen."""
    if step == "conv_categories":
        cats = active_categories()
        return "Currently shown categories:\n" + "\n".join(f"{i+1}. {c}" for i, c in enumerate(cats))
    rows = _shown_rows(step, state)
    if rows:
        return "Currently shown products (number. name [id]):\n" + "\n".join(
            f"{i+1}. {r.get('name')} [{r.get('id')}]" for i, r in enumerate(rows)
        )
    return ""


def _build_messages(step: str, message: str, state: dict[str, Any],
                    conversation_data: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, str]]:
    digest = _catalog_digest(catalog.fetch_active_rows(), cfg.get("currency") or "INR")
    shown = _shown_block(step, state)
    system = (
        prompt_config.get_prompt("sales_conv_interpreter_prompt", _DEFAULT_INTERPRETER_PROMPT)
        .replace("[[STEP]]", step)
        .replace("[[SESSION]]", _state_summary(state))
        .replace("[[SHOWN]]", shown)
        .replace("[[CATALOG]]", digest or "(no products)")
        .replace("[[CATEGORIES]]", ", ".join(active_categories()) or "(none)")
    )
    return [{"role": "system", "content": system}, *_recent_history(conversation_data),
            {"role": "user", "content": message}]


def _validate(decision: dict[str, Any], rows: list[dict[str, Any]], shown_rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid_ids = {str(r["id"]) for r in rows if r.get("id")}
    cats_by_lower = {_cat_of(r).lower(): _cat_of(r) for r in rows}
    id_by_name = {(r.get("name") or "").strip().lower(): str(r["id"]) for r in rows if r.get("id") and r.get("name")}
    shown_ids = [str(r["id"]) for r in shown_rows if r.get("id")]

    def resolve(raw: Any) -> str | None:
        """Map a model reference (uuid, exact name, or shown ordinal) to a real id."""
        s = str(raw or "").strip()
        if not s:
            return None
        if s in valid_ids:
            return s
        if s.lower() in id_by_name:
            return id_by_name[s.lower()]
        digits = re.sub(r"[^0-9]", "", s)
        if digits:
            idx = int(digits) - 1
            if 0 <= idx < len(shown_ids):
                return shown_ids[idx]
        return None

    intent = decision.get("intent")
    if intent not in _INTENTS:
        intent = "unrelated"

    categories = []
    for c in decision.get("categories") or []:
        if isinstance(c, str) and c.lower() in cats_by_lower:
            categories.append(cats_by_lower[c.lower()])

    items = []
    for it in decision.get("items") or []:
        pid = resolve(it.get("product_id")) if isinstance(it, dict) else None
        if pid:
            items.append({"product_id": pid, "quantity": _coerce_qty(it.get("quantity"))})

    quantity_updates = []
    for it in decision.get("quantity_updates") or []:
        pid = resolve(it.get("product_id")) if isinstance(it, dict) else None
        q = _coerce_qty(it.get("quantity")) if isinstance(it, dict) else None
        if pid and q is not None:
            quantity_updates.append({"product_id": pid, "quantity": q})

    removals = [pid for pid in (resolve(p) for p in (decision.get("removals") or [])) if pid]

    replacements = []
    for rp in decision.get("replacements") or []:
        from_id = resolve(rp.get("from_id")) if isinstance(rp, dict) else None
        to_id = resolve(rp.get("to_id")) if isinstance(rp, dict) else None
        if from_id and to_id:
            replacements.append({"from_id": from_id, "to_id": to_id, "quantity": _coerce_qty(rp.get("quantity"))})

    raw_target = decision.get("target_step")
    target_step = _TARGET_STEPS.get(raw_target) if isinstance(raw_target, str) else None
    answer = decision.get("answer") if isinstance(decision.get("answer"), str) else ""

    return {
        "intent": intent, "categories": categories, "items": items,
        "quantity_updates": quantity_updates, "removals": removals,
        "replacements": replacements, "target_step": target_step, "answer": answer,
    }


# Intents that already capture the customer's reference to a product — the
# deterministic safety net must NOT override these.
_NO_AUGMENT = {"answer_doubt", "unrelated", "cancel", "go_back", "remove", "change_quantity", "replace"}


def _keyword_items(message: str, shown: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shown products explicitly named (by name or ordinal) in the message —
    a deterministic backstop for when the model returns no items."""
    low = (message or "").lower()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in sorted(shown, key=lambda x: len(x.get("name") or ""), reverse=True):
        name = (r.get("name") or "").strip().lower()
        pid = str(r.get("id"))
        if name and name in low and pid not in seen:
            out.append({"product_id": pid, "quantity": None})
            seen.add(pid)
    for mt in re.finditer(r"(?:number|no\.?|#|item)\s*(\d+)|\b(\d+)(?:st|nd|rd|th)\b", low):
        n = mt.group(1) or mt.group(2)
        idx = int(n) - 1
        if 0 <= idx < len(shown):
            pid = str(shown[idx]["id"])
            if pid not in seen:
                out.append({"product_id": pid, "quantity": None})
                seen.add(pid)
    return out


def _keyword_categories(message: str) -> list[str]:
    """Active categories explicitly named in the message (longest first)."""
    low = (message or "").lower()
    out: list[str] = []
    for c in sorted(active_categories(), key=len, reverse=True):
        if c.lower() in low and c not in out:
            out.append(c)
    return out


def _augment(step: str, message: str, state: dict[str, Any],
             decision: dict[str, Any], shown: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic backstop for the common cases the model sometimes misses:
    naming a category, a bare quantity answering a pending question, and a
    named/numbered product. Only fires when the model returned nothing actionable;
    never overrides cancel/go_back, nor a genuine question (answer_doubt with '?')."""
    intent = decision["intent"]
    if intent in ("cancel", "go_back"):
        return decision
    is_question = intent == "answer_doubt" and "?" in (message or "")

    if step == "conv_categories":
        # Naming a category at the category step is navigation, not a doubt —
        # unless it's an actual question (has '?'). Honour it even if the model
        # mislabelled it (e.g. as answer_doubt).
        if not is_question:
            cats = decision["categories"] or _keyword_categories(message)
            if cats:
                decision["intent"] = "select"
                decision["categories"] = cats
        return decision

    if step not in ("conv_products", "conv_confirm"):
        return decision
    if intent in _NO_AUGMENT or decision["items"] or decision["quantity_updates"]:
        return decision

    pending = state.get("pending_quantity") or []
    if len(pending) == 1:
        num = _first_int(message)
        if num is not None:
            decision["intent"] = "set_quantity"
            decision["items"] = [{"product_id": pending[0]["product_id"], "quantity": num}]
            return decision

    found = _keyword_items(message, shown)
    if found:
        decision["intent"] = "select"
        decision["items"] = found
    return decision


async def interpret_turn(step: str, message: str, state: dict[str, Any],
                         conversation_data: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    """Map one in-flow message to a validated ``Decision`` (see module docstring)."""
    rows = catalog.fetch_active_rows()
    shown = _shown_rows(step, state)
    result = await _call_llm(_build_messages(step, message, state, conversation_data, cfg), effort="low")
    # Model unavailable / unparseable → unrelated, so the handler re-presents the
    # current step without losing state.
    decision = _validate(result or {"intent": "unrelated"}, rows, shown)
    return _augment(step, message, state, decision, shown)


# ── Address extraction (conv_address step) ───────────────────────────────────
async def extract_address(message: str, partial: dict[str, Any]) -> dict[str, str]:
    """Pull delivery fields (name / address / city / pincode) from free text,
    merging with whatever is already known. Returns only the fields it found."""
    known = ", ".join(f"{k}={v}" for k, v in (partial or {}).items() if v)
    system = prompt_config.get_prompt("sales_conv_address_prompt", _DEFAULT_ADDRESS_PROMPT)
    user = message if not known else f"Already known: {known}\nNew message: {message}"
    result = await _call_llm([{"role": "system", "content": system}, {"role": "user", "content": user}])
    if not isinstance(result, dict):
        return {}
    return {k: str(result.get(k) or "").strip() for k in ("name", "address", "city", "pincode")}
