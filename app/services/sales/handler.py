"""Sales Bot conversation pipeline.

Entry point ``handle_sales_message`` is called by the webhook ONLY when
``sales_bot.is_selected`` is true. It owns its own inbound parsing, conversation
persistence (same ``whatsapp_conversations`` table + JSON-array pattern as the
Information Bot, with transient position stored under ``sales_state`` on the last
entry), and outbound interactive messaging. The Information Bot code path is never
touched from here.

Shopping is a single continuous flow: once a customer enters it (Browse / Search /
View cart from the menu) they stay in ``step="shopping"`` — every reply carries
navigation to any other part (categories, cart, search, main menu) and free text
is treated as a search. The flow only closes after a successful payment (the
Razorpay webhook resets the state to idle via ``_reset_sales_state``); from then
on the next message shows the main menu again. Tapping *Main menu* (or typing
``menu``) is always available as an escape.
"""

import asyncio
import collections
import copy
import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.db.supabase_client import first_row, supabase, supabase_admin
from app.services.whatsapp import send_whatsapp_message, send_whatsapp_text, send_whatsapp_typing_indicator
from app.services import email_service, prompt_config
from app.services.sales import ai_chat, messaging as m
from app.services.sales import cart, catalog, conversational, orders, razorpay_service

logger = logging.getLogger("whatsapp")

# Greetings open the menu when the customer is idle.
GREETINGS = {"hi", "hello", "hey", "menu", "start", "main menu", "help", "hi there"}
# Words that always exit the shopping flow back to the main menu.
MENU_EXIT_WORDS = {"menu", "main menu", "home", "exit", "cancel", "quit"}

WELCOME = "Welcome to Rhodnet AI, You can purchase all the products at any time."

# How many product cards to render per category/search screen.
_MAX_CARDS = 8

# Idempotency guard: WhatsApp re-delivers webhook events on retry, which would
# otherwise process a Flow completion (and re-send the payment link) twice. We
# remember recently handled message_ids and ignore duplicates.
_SEEN_MESSAGE_IDS: "collections.OrderedDict[str, None]" = collections.OrderedDict()
_SEEN_MESSAGE_IDS_MAX = 2000


def _already_handled(message_id: str | None) -> bool:
    """True if this message_id was already processed (duplicate delivery)."""
    if not isinstance(message_id, str) or not message_id:
        return False
    if message_id in _SEEN_MESSAGE_IDS:
        return True
    _SEEN_MESSAGE_IDS[message_id] = None
    while len(_SEEN_MESSAGE_IDS) > _SEEN_MESSAGE_IDS_MAX:
        _SEEN_MESSAGE_IDS.popitem(last=False)
    return False


def _db():
    return supabase_admin if supabase_admin is not None else supabase


def _idle() -> dict[str, Any]:
    return {"step": "idle"}


def _shopping() -> dict[str, Any]:
    return {"step": "shopping"}


def _cat_of(row: dict[str, Any]) -> str:
    return (row.get("category") or "").strip() or "Other"


def _short(text: str, limit: int) -> str:
    s = (text or "").strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


# ── Inbound parsing ──────────────────────────────────────────────────────────
def parse_incoming(message: dict[str, Any]) -> dict[str, Any]:
    """Normalise a WhatsApp inbound message into a small dict the router uses."""
    mtype = message.get("type")

    if mtype == "text":
        body = ((message.get("text") or {}).get("body") or "").strip()
        return {"kind": "text", "text": body, "display": body}

    if mtype == "interactive":
        inter = message.get("interactive") or {}
        itype = inter.get("type")
        if itype == "button_reply":
            r = inter.get("button_reply") or {}
            return {"kind": "reply", "reply_id": r.get("id") or "", "display": r.get("title") or ""}
        if itype == "list_reply":
            r = inter.get("list_reply") or {}
            return {"kind": "reply", "reply_id": r.get("id") or "", "display": r.get("title") or ""}
        if itype == "nfm_reply":
            raw = (inter.get("nfm_reply") or {}).get("response_json")
            try:
                data = json.loads(raw) if raw else {}
            except (ValueError, TypeError):
                data = {}
            return {"kind": "flow", "flow": data, "display": "[delivery details submitted]"}
        return {"kind": "unsupported", "display": f"[interactive:{itype}]"}

    if mtype == "order":
        order = message.get("order") or {}
        items = order.get("product_items") or []
        return {"kind": "order", "order_items": items, "display": f"[cart sent: {len(items)} item(s)]"}

    return {"kind": "unsupported", "display": f"[{mtype}]"}


def _summarize_outgoing(msgs: list[dict[str, Any]]) -> str:
    """A plain-text rendering of the outgoing messages for the dashboard view."""
    parts: list[str] = []
    for msg in msgs:
        t = msg.get("type")
        if t == "text":
            parts.append((msg.get("text") or {}).get("body", ""))
        elif t == "image":
            parts.append("[image] " + ((msg.get("image") or {}).get("caption") or ""))
        elif t == "interactive":
            inter = msg.get("interactive") or {}
            body = (inter.get("body") or {}).get("text", "")
            parts.append(body or f"[{inter.get('type')}]")
        else:
            parts.append(f"[{t}]")
    return "\n\n".join(p for p in parts if p).strip()


# ── Config ───────────────────────────────────────────────────────────────────
async def load_sales_config() -> dict[str, Any]:
    row = first_row(_db().table("sales_bot").select("*").eq("id", 1).execute()) or {}
    bot_name = (row.get("bot_name") or "").strip() or "our store"
    return {
        "bot_name": bot_name,
        "greeting": row.get("greeting") or "",
        "products_enabled": bool(row.get("sales_products_enabled")),
        "payment_enabled": bool(row.get("payment_enabled")),
        "currency": row.get("default_currency") or "INR",
        "flat_shipping_minor": int(row.get("flat_shipping_minor") or 0),
        "free_shipping_threshold_minor": row.get("free_shipping_threshold_minor"),
        "catalog_id": settings.meta_catalog_id or "",
        "flow_id": settings.whatsapp_checkout_flow_id or "",
        "store_flow_id": settings.whatsapp_store_flow_id or "",
        "payment_notify_whatsapp": bool(row.get("sales_payment_notify_whatsapp_enabled")),
        "payment_notify_email": bool(row.get("sales_payment_notify_email_enabled")),
    }


# ── Menu & navigation ────────────────────────────────────────────────────────
def _main_menu(cfg: dict[str, Any]) -> dict[str, Any]:
    # Path B: once the "Open Store" data-exchange Flow is configured, the menu
    # collapses to two rows — the Flow covers browse/cart/track/checkout in one
    # in-app sheet. Without it (no encryption keypair / Flow published yet) we
    # fall back to the chat-based browsing rows, so the bot still works fully.
    if cfg.get("store_flow_id"):
        rows = [
            m.list_row(m.MENU_STORE, "🏬 Open Store", "Browse, cart, track & checkout"),
            m.list_row(m.MENU_AI, "🤖 Talk to AI", "Ask about our products"),
        ]
    else:
        rows = [
            m.list_row(m.MENU_BROWSE, "🛍️ Browse products", "See what's available"),
            m.list_row(m.MENU_CART, "🛒 View cart", "Review items & checkout"),
            m.list_row(m.MENU_TRACK, "📦 Track order", "Check an order's status"),
            m.list_row(m.MENU_AI, "🤖 Talk to AI", "Ask about our products"),
        ]
    return m.list_message(
        body=WELCOME,
        button_text="Menu",
        sections=[m.section("Options", rows)],
    )


async def _open_store(sender: str, cfg: dict[str, Any]):
    """Send the single "Open Store" Flow message. The Flow itself (via the
    data-exchange endpoint) handles browse/cart/track/checkout; we only step back
    into chat at the very end for the Razorpay payment link."""
    if not cfg.get("store_flow_id"):
        # Shouldn't happen (the row is only shown when configured), but degrade
        # gracefully to the chat browse path.
        return await _browse_categories(sender, cfg)
    return [m.store_flow(flow_id=cfg["store_flow_id"], sender=sender)], _idle()


def _menu_messages(cfg: dict[str, Any], greet: bool) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    if greet and cfg["greeting"]:
        msgs.append(m.text(cfg["greeting"]))
    msgs.append(_main_menu(cfg))
    return msgs


def _nav_list(body: str = "What would you like to do next?") -> dict[str, Any]:
    """Reusable navigation appended to flow screens so the customer can always
    jump to categories, search, the cart, or back to the main menu."""
    rows = [
        m.list_row(m.MENU_BROWSE, "📋 All categories", "Browse by category"),
        m.list_row(m.FLOW_SEARCH, "🔍 Search", "Find a product or category"),
        m.list_row(m.MENU_CART, "🛒 View cart", "Review & checkout"),
        m.list_row(m.FLOW_EXIT, "🏠 Main menu", "Leave shopping"),
    ]
    return m.list_message(body=body, button_text="Options", sections=[m.section("Navigate", rows)])


# ── Browsing ─────────────────────────────────────────────────────────────────
async def _browse_categories(sender: str, cfg: dict[str, Any]):
    rows = catalog.fetch_active_rows()
    if not rows:
        return [m.text("No products are available right now. Please check back soon."), _main_menu(cfg)], _idle()
    categories = catalog.distinct_active_categories(rows)
    # WhatsApp caps a list at 10 rows total: up to 7 categories + 3 nav rows.
    category_rows = [m.list_row(f"{m.CAT_PREFIX}{c}", c, "") for c in categories[:7]]
    nav_rows = [
        m.list_row(m.FLOW_SEARCH, "🔍 Search", "Find a product or category"),
        m.list_row(m.MENU_CART, "🛒 View cart", "Review & checkout"),
        m.list_row(m.FLOW_EXIT, "🏠 Main menu", "Leave shopping"),
    ]
    msg = m.list_message(
        body=(
            "🛍️ Browse our products.\n\nPick a category below, tap *Search*, or just "
            "type what you're looking for."
        ),
        button_text="Categories",
        sections=[m.section("Categories", category_rows), m.section("More", nav_rows)],
    )
    return [msg], _shopping()


def _rows_in_category(category: str) -> list[dict[str, Any]]:
    return [r for r in catalog.fetch_active_rows() if _cat_of(r) == category]


def _product_card(row: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """A single product 'card': image header + title/price/short description and a
    *Add to cart* (+) button alongside *Details*."""
    price = m.format_money(row.get("price_minor") or 0, row.get("currency") or cfg["currency"])
    name = row.get("name") or "Item"
    desc = (row.get("description") or "").strip()
    stock = row.get("stock_quantity")
    out_of_stock = stock is not None and int(stock) <= 0

    body = f"*{name}*\n{price}"
    if desc:
        body += f"\n\n{_short(desc, 160)}"
    if out_of_stock:
        body += "\n\n⚠️ Out of stock"
        opts = [(f"{m.PROD_PREFIX}{row['id']}", "ℹ️ Details")]
    else:
        opts = [
            (f"{m.ADD_PREFIX}{row['id']}", "➕ Add to cart"),
            (f"{m.PROD_PREFIX}{row['id']}", "ℹ️ Details"),
        ]
    return m.buttons(body, opts, header_image=row.get("image_url") or None)


async def _browse_category(sender: str, category: str, cfg: dict[str, Any]):
    rows = _rows_in_category(category)
    if not rows:
        return [m.text("No products in this category yet."), _nav_list()], _shopping()
    cards = [_product_card(r, cfg) for r in rows[:_MAX_CARDS]]
    nav_body = f"*{category}* — tap *➕ Add to cart* on any item, or *ℹ️ Details* for more."
    extra = len(rows) - _MAX_CARDS
    if extra > 0:
        nav_body += f"\n\n{extra} more item(s) — use 🔍 Search to narrow down."
    cards.append(_nav_list(nav_body))
    return cards, _shopping()


async def _product_detail(sender: str, product_id: str, cfg: dict[str, Any]):
    row = catalog.get_row(product_id)
    if not row or not row.get("is_active", True):
        return [m.text("That product isn't available."), _nav_list()], _shopping()
    price = m.format_money(row.get("price_minor") or 0, row.get("currency") or cfg["currency"])
    stock = row.get("stock_quantity")
    if stock is None:
        stock_line = ""
    elif int(stock) > 0:
        stock_line = f"\nIn stock: {int(stock)}"
    else:
        stock_line = "\n⚠️ Out of stock"
    body = f"*{row.get('name')}*\n{price}{stock_line}\n\n{row.get('description') or ''}".strip()
    out_of_stock = stock is not None and int(stock) <= 0

    msgs: list[dict[str, Any]] = []
    if row.get("image_url"):
        msgs.append(m.image(row["image_url"]))
    if out_of_stock:
        opts = [
            (m.MENU_BROWSE, "📋 Categories"),
            (m.MENU_CART, "🛒 View cart"),
            (m.FLOW_SEARCH, "🔍 Search"),
        ]
    else:
        opts = [
            (f"{m.ADD_PREFIX}{product_id}", "➕ Add to cart"),
            (m.MENU_CART, "🛒 View cart"),
            (m.MENU_BROWSE, "📋 Categories"),
        ]
    msgs.append(m.buttons(body, opts))
    return msgs, _shopping()


async def _add_to_cart(sender: str, product_id: str, cfg: dict[str, Any]):
    if not cart.add_item(sender, product_id, 1):
        return [m.text("Sorry, that product isn't available."), _nav_list()], _shopping()
    count = sum(i["quantity"] for i in cart.get_items(sender))
    opts = [
        (m.MENU_CART, "🛒 View cart"),
        (m.MENU_BROWSE, "📋 Keep shopping"),
        (m.CART_CHECKOUT, "✅ Checkout"),
    ]
    return [m.buttons(f"Added! 🛒 Your cart now has {count} item(s).", opts)], _shopping()


# ── Cart (custom DB cart — used for everyone) ────────────────────────────────
async def _view_cart(sender: str, cfg: dict[str, Any]):
    items = cart.get_items(sender)
    if not items:
        return [
            m.text("🛒 Your cart is empty."),
            _nav_list("Browse our products or search to add something."),
        ], _shopping()

    lines: list[str] = []
    subtotal = 0
    item_rows: list[dict[str, Any]] = []
    for i in items:
        line_total = i["unit_price_minor"] * i["quantity"]
        subtotal += line_total
        lines.append(f"• {i['name']} ×{i['quantity']} — {m.format_money(line_total, cfg['currency'])}")
        item_rows.append(
            m.list_row(
                f"{m.CITEM_PREFIX}{i['product_id']}",
                f"{i['name']} ×{i['quantity']}",
                "Tap to change qty or remove",
            )
        )
    body = (
        "🛒 *Your cart*\n"
        + "\n".join(lines)
        + f"\n\nSubtotal: {m.format_money(subtotal, cfg['currency'])}"
    )
    action_rows = [
        m.list_row(m.CART_CHECKOUT, "✅ Checkout", "Proceed to payment"),
        m.list_row(m.MENU_BROWSE, "📋 Keep shopping", "Back to categories"),
        m.list_row(m.CART_CLEAR, "🗑️ Clear cart", "Empty the cart"),
        m.list_row(m.FLOW_EXIT, "🏠 Main menu", "Leave shopping"),
    ]
    # 10-row list cap: actions + up to (10 - actions) editable item rows.
    max_items = 10 - len(action_rows)
    sections = [
        m.section("Items (tap to edit)", item_rows[:max_items]),
        m.section("Actions", action_rows),
    ]
    return [m.list_message(body=body, button_text="Manage cart", sections=sections)], _shopping()


async def _cart_item(sender: str, product_id: str, cfg: dict[str, Any]):
    item = next((i for i in cart.get_items(sender) if i["product_id"] == product_id), None)
    if not item:
        return await _view_cart(sender, cfg)
    line_total = item["unit_price_minor"] * item["quantity"]
    body = (
        f"*{item['name']}*\n"
        f"Quantity: {item['quantity']}\n"
        f"Line total: {m.format_money(line_total, cfg['currency'])}\n\n"
        "Adjust below:"
    )
    opts = [
        (f"{m.INC_PREFIX}{product_id}", "➕ Add one"),
        (f"{m.DEC_PREFIX}{product_id}", "➖ Remove one"),
        (f"{m.RM_PREFIX}{product_id}", "🗑️ Remove item"),
    ]
    return [m.buttons(body, opts)], _shopping()


async def _cart_inc(sender: str, product_id: str, cfg: dict[str, Any]):
    cart.update_quantity(sender, product_id, 1)
    return await _view_cart(sender, cfg)


async def _cart_dec(sender: str, product_id: str, cfg: dict[str, Any]):
    cart.update_quantity(sender, product_id, -1)
    return await _view_cart(sender, cfg)


async def _cart_remove(sender: str, product_id: str, cfg: dict[str, Any]):
    cart.remove_item(sender, product_id)
    return await _view_cart(sender, cfg)


async def _clear_cart(sender: str, cfg: dict[str, Any]):
    cart.clear_cart(sender)
    return [m.text("Your cart has been cleared."), _nav_list()], _shopping()


# ── Search ───────────────────────────────────────────────────────────────────
async def _search_prompt(sender: str, cfg: dict[str, Any]):
    return [m.text("🔍 Type the name of a product or category you're looking for.")], _shopping()


async def _do_search(sender: str, query: str, cfg: dict[str, Any]):
    raw = (query or "").strip()
    q = raw.lower()
    if not q:
        return await _search_prompt(sender, cfg)
    rows = catalog.fetch_active_rows()

    # An exact category-name match jumps straight into that category.
    categories = {_cat_of(r).lower(): _cat_of(r) for r in rows}
    if q in categories:
        return await _browse_category(sender, categories[q], cfg)

    matches = [
        r for r in rows
        if q in f"{r.get('name', '')} {r.get('category', '')} {r.get('description', '')}".lower()
    ]
    if not matches:
        return [
            m.text(f"No products matched “{raw}”."),
            _nav_list("Try another search or browse by category."),
        ], _shopping()

    cards = [_product_card(r, cfg) for r in matches[:_MAX_CARDS]]
    nav_body = "Tap *➕ Add to cart* or *ℹ️ Details*."
    extra = len(matches) - _MAX_CARDS
    if extra > 0:
        nav_body += f"\n\n{extra} more match(es) — refine your search."
    return [m.text(f"🔍 Results for “{raw}”:"), *cards, _nav_list(nav_body)], _shopping()


async def _flow_exit(sender: str, cfg: dict[str, Any]):
    return _menu_messages(cfg, greet=False), _idle()


# ══ Path C — fully conversational, text-only purchase flow ═══════════════════
# Every message here is plain text (no buttons/lists/Flow); the single exception
# is the payment hand-off, which reuses _send_payment_link's CTA-URL. State (cart,
# selected categories, pending quantities, address, order number) lives on
# sales_state — no DB cart; an order is created only at confirmation. The LLM logic
# (entry intent + the per-turn interpreter) lives in conversational.py. See
# docs/Conversational Sales Flow (Path C).md (§14).

CONV_STEPS = {"conv_categories", "conv_products", "conv_confirm", "conv_address", "conv_payment"}
_CONV_MAX_PRODUCTS = 40  # also matches conversational._shown_block numbering


# ── Text renderers (plain text only) ─────────────────────────────────────────
def _render_categories_msgs(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    cats = conversational.active_categories()
    if not cats:
        return [m.text("No products are available right now. Please check back soon.")]
    lines = [f"{i + 1}. {c}" for i, c in enumerate(cats)]
    body = (
        "🛍️ Here's what we sell. Tell me what you're interested in — you can name one or several:\n\n"
        + "\n".join(lines)
        + "\n\nFor example: \"I'm looking for footwear and something electronic.\""
    )
    return [m.text(body)]


def _render_products_msgs(state: dict[str, Any], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    rows = conversational.products_in_categories(state.get("selected_categories") or [])
    if not rows:
        return [m.text(
            "I couldn't find products in that category. Tell me another category, "
            "or say \"show me everything\"."
        )]
    shown = rows[:_CONV_MAX_PRODUCTS]
    lines = ["Great — here's what we have:"]
    current = None
    for i, r in enumerate(shown):
        cat = _cat_of(r)
        if cat != current:
            lines.append(f"\n*{cat}*")
            current = cat
        price = m.format_money(r.get("price_minor") or 0, r.get("currency") or cfg["currency"])
        stock = r.get("stock_quantity")
        avail = " — out of stock" if (stock is not None and int(stock) <= 0) else ""
        lines.append(f"{i + 1}. {r.get('name')} — {price}{avail}")
    if len(rows) > _CONV_MAX_PRODUCTS:
        lines.append(f"\n…and {len(rows) - _CONV_MAX_PRODUCTS} more — narrow down by category if you like.")
    lines.append(
        "\nTell me which ones and how many — e.g. \"2 of number 1 and one speaker\". "
        "You can also ask me anything about them."
    )
    return [m.text("\n".join(lines))]


def _cart_summary_msg(state: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    cart = state.get("cart") or []
    if not cart:
        return m.text("Your selection is empty. Tell me what you'd like, or say \"show me the products\".")
    lines = ["🛒 Here's your order so far:"]
    subtotal = 0
    for it in cart:
        line_total = int(it["unit_price_minor"]) * int(it["quantity"])
        subtotal += line_total
        lines.append(f"• {it['name']} ×{it['quantity']} — {m.format_money(line_total, cfg['currency'])}")
    lines.append(f"\nSubtotal: {m.format_money(subtotal, cfg['currency'])}")
    lines.append(
        "\nReply *confirm* to continue to delivery, or tell me what to change "
        "(e.g. \"make the shoes 1\", \"remove the earbuds\", \"add a speaker\")."
    )
    return m.text("\n".join(lines))


def _ask_quantity_msg(state: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    pending = state.get("pending_quantity") or []
    if len(pending) == 1:
        return m.text(f"How many *{pending[0]['name']}* would you like?")
    names = ", ".join(p.get("name") or "" for p in pending) or "those items"
    return m.text(f"How many of each would you like? ({names})")


def _ask_address_msg(state: dict[str, Any]) -> dict[str, Any]:
    addr = state.get("address") or {}
    missing = [f for f in ("name", "address", "city", "pincode") if not addr.get(f)]
    if addr and 0 < len(missing) < 4:
        labels = {"name": "full name", "address": "address", "city": "city", "pincode": "pincode"}
        return m.text("Thanks! I still need your " + ", ".join(labels[x] for x in missing) + ".")
    return m.text("Almost there! Please send your delivery details:\nName, full address, city, and pincode.")


async def _represent_step(sender: str, state: dict[str, Any], cfg: dict[str, Any]):
    """Re-present the current step from state (used after a doubt / unrelated
    message so nothing is ever lost)."""
    step = state.get("step")
    if step == "conv_categories":
        return _render_categories_msgs(cfg), state
    if step == "conv_products":
        if state.get("pending_quantity"):
            return [_ask_quantity_msg(state, cfg)], state
        return _render_products_msgs(state, cfg), state
    if step == "conv_confirm":
        return [_cart_summary_msg(state, cfg)], state
    if step == "conv_address":
        return [_ask_address_msg(state)], state
    if step == "conv_payment":
        msgs, st = await _resend_payment(sender, state, cfg)
        # Keep conv_payment unless the order is paid/gone (then _resend_payment
        # already returns an idle/menu state — propagate it).
        return msgs, (state if st.get("step") == "awaiting_payment" else st)
    return _menu_messages(cfg, greet=False), _idle()


# ── Session cart helpers (held in sales_state, not the DB) ───────────────────
def _cart_add(state: dict[str, Any], product_id: str, qty: int, cfg: dict[str, Any]) -> None:
    row = catalog.get_row(product_id)
    if not row or not row.get("is_active", True):
        return
    cart = state.setdefault("cart", [])
    for it in cart:
        if it["product_id"] == product_id:
            it["quantity"] = int(it["quantity"]) + int(qty)
            return
    cart.append({
        "product_id": product_id,
        "name": row.get("name") or "Item",
        "quantity": int(qty),
        "unit_price_minor": catalog.effective_unit_price_minor(row),
    })


def _cart_set_qty(state: dict[str, Any], product_id: str, qty: int) -> None:
    for it in state.get("cart") or []:
        if it["product_id"] == product_id:
            if int(qty) <= 0:
                _sess_cart_remove(state, product_id)
            else:
                it["quantity"] = int(qty)
            return


def _sess_cart_remove(state: dict[str, Any], product_id: str) -> None:
    state["cart"] = [it for it in (state.get("cart") or []) if it["product_id"] != product_id]


def _cart_put(state: dict[str, Any], product_id: str, qty: int, cfg: dict[str, Any]) -> None:
    """SET a product's line quantity to ``qty`` (add the line if absent). Used for
    selections so the model re-stating an existing cart item is a no-op rather than
    doubling it — the LLM tends to echo the whole cart on edit/confirm turns."""
    for it in state.get("cart") or []:
        if it["product_id"] == product_id:
            it["quantity"] = int(qty)
            return
    _cart_add(state, product_id, int(qty), cfg)


def _cart_snapshot(state: dict[str, Any]) -> list[tuple[str, int]]:
    """Order-independent view of the cart, to tell whether a turn changed it."""
    return sorted((i["product_id"], int(i["quantity"])) for i in state.get("cart") or [])


def _queue_quantity(state: dict[str, Any], product_id: str) -> None:
    row = catalog.get_row(product_id)
    if not row:
        return
    pending = state.setdefault("pending_quantity", [])
    if not any(p["product_id"] == product_id for p in pending):
        pending.append({"product_id": product_id, "name": row.get("name") or "Item"})


def _ingest_items(state: dict[str, Any], items: list[dict[str, Any]], cfg: dict[str, Any]) -> None:
    """Add items with a stated quantity to the cart; queue those without one for a
    quantity question (§14.3)."""
    for it in items:
        pid = it.get("product_id")
        qty = it.get("quantity")
        if not pid:
            continue
        if qty:
            _cart_put(state, pid, int(qty), cfg)
            state["pending_quantity"] = [p for p in state.get("pending_quantity", []) if p["product_id"] != pid]
        else:
            _queue_quantity(state, pid)


def _apply_cart_edits(decision: dict[str, Any], state: dict[str, Any], cfg: dict[str, Any]) -> None:
    for qu in decision.get("quantity_updates") or []:
        _cart_set_qty(state, qu["product_id"], qu["quantity"])
    for pid in decision.get("removals") or []:
        _sess_cart_remove(state, pid)
    for rp in decision.get("replacements") or []:
        _sess_cart_remove(state, rp["from_id"])
        if rp.get("quantity"):
            _cart_add(state, rp["to_id"], int(rp["quantity"]), cfg)
        else:
            _queue_quantity(state, rp["to_id"])


def _merge_categories(state: dict[str, Any], categories: list[str]) -> None:
    merged = state.setdefault("selected_categories", [])
    for c in categories:
        if c not in merged:
            merged.append(c)


def _after_mutation(state: dict[str, Any], cfg: dict[str, Any]):
    """Decide where the customer lands after a cart change: ask for a pending
    quantity, go back to products if the cart emptied, else show the summary."""
    if state.get("pending_quantity"):
        state["step"] = "conv_products"
        return [_ask_quantity_msg(state, cfg)], state
    if not state.get("cart"):
        state["step"] = "conv_products"
        return _render_products_msgs(state, cfg), state
    state["step"] = "conv_confirm"
    return [_cart_summary_msg(state, cfg)], state


# ── Entry (idle free text → buy vs unrelated) ────────────────────────────────
def _seed_categories_from_items(state: dict[str, Any], items: list[dict[str, Any]]) -> None:
    """Adopt the categories of any directly-named products as the selected
    categories, so the products view stays coherent if the customer later
    browses or goes back."""
    cats: list[str] = []
    for it in items:
        row = catalog.get_row(it.get("product_id"))
        if row:
            cat = _cat_of(row)
            if cat not in cats:
                cats.append(cat)
    _merge_categories(state, cats)


async def _conv_entry(sender: str, text: str, cfg: dict[str, Any], conversation_data: list[dict[str, Any]]):
    intent = await conversational.classify_entry(text, conversation_data, cfg)
    if intent != "buy":
        return [m.text(conversational.SORRY_MESSAGE)], _idle()
    if not conversational.active_categories():
        return [m.text("No products are available right now. Please check back soon."), _main_menu(cfg)], _idle()
    state = {"step": "conv_categories", "selected_categories": [], "cart": [], "pending_quantity": []}

    # If the very first message already names specific products (with or without a
    # quantity) or a category, honour it and jump straight to the right step —
    # don't restart the customer at the category list. The same interpreter and
    # appliers that own in-flow turns decide what they asked for.
    decision = await conversational.interpret_turn("conv_categories", text, state, conversation_data, cfg)
    if decision.get("intent") not in ("cancel", "unrelated", "answer_doubt", "go_back"):
        if decision.get("items"):
            _seed_categories_from_items(state, decision["items"])
        cart_before = _cart_snapshot(state)
        pending_before = list(state.get("pending_quantity") or [])
        _apply_cart_mutations(decision, state, cfg)
        if decision.get("categories"):
            _merge_categories(state, decision["categories"])
        cart_changed = _cart_snapshot(state) != cart_before
        pending_changed = (state.get("pending_quantity") or []) != pending_before
        if cart_changed or pending_changed:
            # Named a product (→ cart, or → ask its quantity).
            return _after_mutation(state, cfg)
        if state.get("selected_categories"):
            # Named only a category → show its products.
            state["step"] = "conv_products"
            return _render_products_msgs(state, cfg), state

    return _render_categories_msgs(cfg), state


# ── go back ──────────────────────────────────────────────────────────────────
def _go_back(target: str, state: dict[str, Any], cfg: dict[str, Any]):
    if target == "conv_categories":
        state["step"] = "conv_categories"
        return _render_categories_msgs(cfg), state
    if target == "conv_products":
        state["step"] = "conv_products"
        return _render_products_msgs(state, cfg), state
    if target == "conv_confirm":
        state["step"] = "conv_confirm"
        return [_cart_summary_msg(state, cfg)], state
    return None


# ── Per-step appliers ────────────────────────────────────────────────────────
def _apply_categories(decision: dict[str, Any], state: dict[str, Any], cfg: dict[str, Any]):
    if decision["intent"] in ("select", "add_more", "confirm") and decision.get("categories"):
        _merge_categories(state, decision["categories"])
        state["step"] = "conv_products"
        return _render_products_msgs(state, cfg), state
    return [m.text("Tell me which category you'd like — name one or more from the list.")] \
        + _render_categories_msgs(cfg), state


def _apply_cart_mutations(decision: dict[str, Any], state: dict[str, Any], cfg: dict[str, Any]) -> bool:
    """Apply any item additions / quantity changes / removals / replacements from
    the decision. Returns True if the cart (or pending list) changed."""
    mutated = False
    if decision.get("items"):
        _ingest_items(state, decision["items"], cfg)
        mutated = True
    if decision.get("quantity_updates") or decision.get("removals") or decision.get("replacements"):
        _apply_cart_edits(decision, state, cfg)
        mutated = True
    return mutated


_AFFIRMATIONS = {
    "confirm", "confirmed", "yes", "yep", "yeah", "ya", "yup", "y", "ok", "okay", "k",
    "sure", "proceed", "checkout", "check out", "place order", "place the order",
    "go ahead", "done", "that's all", "thats all", "finalize", "pay", "buy", "buy now",
}


def _is_affirmation(message: str) -> bool:
    """A clear, short confirmation to proceed — used so a plain 'confirm'/'yes'
    creates the order even when the model spuriously emits edits on that turn.
    Deliberately conservative: longer messages (which may contain real edits) are
    left to the LLM/edit path."""
    t = (message or "").strip().lower().strip(".!?, ")
    if not t:
        return False
    if t in _AFFIRMATIONS:
        return True
    words = t.split()
    return len(words) <= 2 and words[0] in _AFFIRMATIONS


def _apply_changes(decision: dict[str, Any], state: dict[str, Any], cfg: dict[str, Any]) -> tuple[bool, bool]:
    """Apply item/cart edits and any newly-named categories. Returns
    ``(cart_changed, categories_changed)`` measured against real before/after
    snapshots — so an echoed (unchanged) cart doesn't count as a change."""
    cart_before = _cart_snapshot(state)
    _apply_cart_mutations(decision, state, cfg)
    cart_changed = _cart_snapshot(state) != cart_before

    cats_before = list(state.get("selected_categories") or [])
    if decision.get("categories"):
        _merge_categories(state, decision["categories"])
    cats_changed = list(state.get("selected_categories") or []) != cats_before
    return cart_changed, cats_changed


def _apply_products(decision: dict[str, Any], state: dict[str, Any], cfg: dict[str, Any]):
    cart_changed, cats_changed = _apply_changes(decision, state, cfg)
    if cart_changed:
        return _after_mutation(state, cfg)
    if cats_changed:
        return _render_products_msgs(state, cfg), state
    if decision["intent"] == "confirm" and state.get("cart"):
        state["step"] = "conv_confirm"
        return [_cart_summary_msg(state, cfg)], state
    return _render_products_msgs(state, cfg), state


async def _apply_confirm(decision: dict[str, Any], state: dict[str, Any], cfg: dict[str, Any],
                         sender: str, message: str):
    # A clear "confirm"/"yes" creates the order from the current cart, ignoring any
    # spurious edits the model emits on that turn (it sometimes removes/echoes items).
    if _is_affirmation(message) and state.get("cart"):
        return await _conv_create_order(sender, state, cfg)
    cart_changed, cats_changed = _apply_changes(decision, state, cfg)
    if cart_changed:
        return _after_mutation(state, cfg)
    if cats_changed:
        state["step"] = "conv_products"
        return _render_products_msgs(state, cfg), state
    # Non-keyword confirmation with no real change (e.g. "looks good, let's do it").
    if decision["intent"] == "confirm" and state.get("cart"):
        return await _conv_create_order(sender, state, cfg)
    return [_cart_summary_msg(state, cfg)], state


async def _conv_create_order(sender: str, state: dict[str, Any], cfg: dict[str, Any]):
    cart = state.get("cart") or []
    if not cart:
        state["step"] = "conv_products"
        return _render_products_msgs(state, cfg), state
    specs = [{"product_id": i["product_id"], "quantity": i["quantity"]} for i in cart]
    order, notes = orders.create_order(sender, specs, cfg)
    if not order:
        state["step"] = "conv_products"
        return [m.text("Those items aren't available anymore. Let's pick again.")] \
            + _render_products_msgs(state, cfg), state
    state["order_number"] = order["order_number"]
    state.setdefault("address", {})
    state["step"] = "conv_address"
    msgs: list[dict[str, Any]] = []
    if notes:
        msgs.append(m.text("\n".join(notes)))
    msgs.append(m.text(_order_summary_text(order, cfg)))
    msgs.append(_ask_address_msg(state))
    return msgs, state


async def _apply_address(decision: dict[str, Any], state: dict[str, Any], cfg: dict[str, Any], sender: str, text: str):
    extracted = await conversational.extract_address(text, state.get("address") or {})
    addr = state.setdefault("address", {})
    for k in ("name", "address", "city", "pincode"):
        v = (extracted.get(k) or "").strip()
        if v:
            addr[k] = v
    if [k for k in ("name", "address", "city", "pincode") if not addr.get(k)]:
        return [_ask_address_msg(state)], state
    return await _conv_send_payment(sender, state, cfg)


async def _conv_send_payment(sender: str, state: dict[str, Any], cfg: dict[str, Any]):
    order = orders.get_order_by_number(state.get("order_number", "")) or orders.latest_order_for_sender(sender)
    if not order:
        return await _conv_create_order(sender, state, cfg)
    addr = state.get("address") or {}
    orders.set_customer_details(
        order["id"], addr.get("name") or "Customer", sender,
        {"address": addr.get("address", ""), "city": addr.get("city", ""), "pincode": addr.get("pincode", "")},
    )
    order = orders.get_order(order["id"]) or order
    order["items"] = orders.get_order_items(order["id"])
    pay_msgs, pay_state = await _send_payment_link(sender, order, cfg)
    if pay_state.get("step") == "awaiting_payment":
        state["step"] = "conv_payment"
        return pay_msgs, state
    # Payments disabled / link failed → existing graceful message, flow closes.
    return pay_msgs, _idle()


async def _apply_payment(decision: dict[str, Any], state: dict[str, Any], cfg: dict[str, Any], sender: str):
    intent = decision["intent"]
    has_edit = bool(
        decision.get("items") or decision.get("quantity_updates")
        or decision.get("removals") or decision.get("replacements") or decision.get("categories")
    )
    if intent in ("change_quantity", "remove", "replace", "add_more", "select") and has_edit:
        if decision.get("categories"):
            _merge_categories(state, decision["categories"])
        if decision.get("items"):
            _ingest_items(state, decision["items"], cfg)
        _apply_cart_edits(decision, state, cfg)
        return _after_mutation(state, cfg)
    return await _represent_step(sender, state, cfg)


# ── The unified per-turn dispatcher ──────────────────────────────────────────
async def _handle_conv_turn(sender: str, text: str, prev_state: dict[str, Any],
                            cfg: dict[str, Any], conversation_data: list[dict[str, Any]]):
    state = copy.deepcopy(prev_state)
    step = state.get("step") or ""
    decision = await conversational.interpret_turn(step, text, state, conversation_data, cfg)
    intent = decision.get("intent")

    if intent == "cancel":
        return [m.text("No problem — I've stopped that. Type *menu* whenever you'd like to start again.")], _idle()
    if intent == "answer_doubt":
        answer = (decision.get("answer") or "").strip() or "Happy to help — could you say a bit more?"
        rep, st = await _represent_step(sender, state, cfg)
        return [m.text(answer), *rep], st
    if intent == "go_back" and decision.get("target_step"):
        back = _go_back(decision["target_step"], state, cfg)
        if back is not None:
            return back

    # The address step treats any non-(doubt/cancel/go_back) message as address
    # input, so a typed address is never mistaken for an "unrelated" message.
    if step == "conv_address":
        return await _apply_address(decision, state, cfg, sender, text)

    if intent == "unrelated":
        rep, st = await _represent_step(sender, state, cfg)
        return [m.text(conversational.SORRY_MESSAGE), *rep], st

    if step == "conv_categories":
        return _apply_categories(decision, state, cfg)
    if step == "conv_products":
        return _apply_products(decision, state, cfg)
    if step == "conv_confirm":
        return await _apply_confirm(decision, state, cfg, sender, text)
    if step == "conv_payment":
        return await _apply_payment(decision, state, cfg, sender)

    rep, st = await _represent_step(sender, state, cfg)
    return rep, st


# ── Talk to AI (LLM Q&A over the catalog) ────────────────────────────────────
def _ai_nav_buttons() -> list[tuple[str, str]]:
    return [(m.MENU_BROWSE, "🛍️ Browse products"), (m.FLOW_EXIT, "🏠 Main menu")]


async def _ai_intro(sender: str, cfg: dict[str, Any]):
    body = (
        "🤖 You're chatting with our AI assistant. Ask me anything about our "
        "products, prices, or availability.\n\nTap *🏠 Main menu* anytime to go back."
    )
    return [m.buttons(body, _ai_nav_buttons())], {"step": "ai_qa"}


# Fallback for the Talk-to-AI catalog framing; live text in app_config
# (sales_ai_catalog_prompt). [[BOT_NAME]]/[[CATALOG]] are filled in at runtime.
_DEFAULT_CATALOG_PROMPT = (
    "You are also the AI shopping assistant for [[BOT_NAME]], a WhatsApp store. "
    "For questions about products, prices, or availability, answer using ONLY the "
    "product catalog below. Be concise and friendly (2-4 short sentences, suitable "
    "for WhatsApp). If something isn't in the catalog, say you don't carry it and "
    "suggest browsing. Prices are final; never invent products or prices.\n\n"
    "CATALOG:\n[[CATALOG]]"
)


def _build_catalog_section(cfg: dict[str, Any]) -> str:
    rows = catalog.fetch_active_rows()
    lines: list[str] = []
    for r in rows[:60]:
        price = m.format_money(r.get("price_minor") or 0, r.get("currency") or cfg["currency"])
        stock = r.get("stock_quantity")
        avail = "out of stock" if (stock is not None and int(stock) <= 0) else "available"
        desc = (r.get("description") or "").strip()
        line = f"- {r.get('name')} ({_cat_of(r)}) — {price}, {avail}."
        if desc:
            line += f" {_short(desc, 120)}"
        lines.append(line)
    catalog_text = "\n".join(lines) if lines else "(no products available)"

    return (
        prompt_config.get_prompt("sales_ai_catalog_prompt", _DEFAULT_CATALOG_PROMPT)
        .replace("[[BOT_NAME]]", cfg["bot_name"])
        .replace("[[CATALOG]]", catalog_text)
    )


async def _ai_answer(sender: str, question: str, cfg: dict[str, Any], conversation_data: list[dict[str, Any]]):
    catalog_section = _build_catalog_section(cfg)
    try:
        reply = await ai_chat.generate_sales_ai_reply(question.strip(), conversation_data, extra_sections=[catalog_section])
    except Exception:
        logger.exception("Sales AI Q&A failed")
        reply = ""
    answer = (reply or "").strip() or "Sorry, I didn't catch that. Could you rephrase, or tap 🏠 Main menu?"
    return [m.buttons(answer, _ai_nav_buttons())], {"step": "ai_qa"}


async def _ai_only_reply(
    sender: str, question: str, cfg: dict[str, Any], conversation_data: list[dict[str, Any]]
):
    """Storefront-disabled mode: the Sales Bot still answers as a pure AI
    assistant driven by its always-on Instructions (and Company Info) section.
    No product catalog is injected and no store navigation is shown — used when
    sales_products_enabled is off, so the bot keeps responding to messages."""
    try:
        reply = await ai_chat.generate_sales_ai_reply(question.strip(), conversation_data)
    except Exception:
        logger.exception("Sales AI (storefront disabled) reply failed")
        reply = ""
    answer = (reply or "").strip() or "Sorry, I didn't catch that. Could you please rephrase?"
    return [m.text(answer)], _idle()


# ── Checkout & payment ───────────────────────────────────────────────────────
def _order_summary_text(order: dict[str, Any], cfg: dict[str, Any]) -> str:
    currency = order.get("currency") or cfg["currency"]
    lines = [f"🧾 *Order {order['order_number']}*"]
    for it in order.get("items", []):
        lines.append(f"• {it['name']} ×{it['quantity']} — {m.format_money(it['line_total_minor'], currency)}")
    lines.append(f"\nSubtotal: {m.format_money(order['subtotal_minor'], currency)}")
    if order.get("shipping_minor"):
        lines.append(f"Shipping: {m.format_money(order['shipping_minor'], currency)}")
    lines.append(f"*Total: {m.format_money(order['total_minor'], currency)}*")
    return "\n".join(lines)


async def _begin_checkout_from_order(sender: str, order_items: list[dict[str, Any]], cfg: dict[str, Any]):
    specs = [
        {"retailer_id": it.get("product_retailer_id"), "quantity": it.get("quantity")}
        for it in order_items
        if it.get("product_retailer_id")
    ]
    order, notes = orders.create_order(sender, specs, cfg)
    if not order:
        return [m.text("We couldn't match those items to our catalog. Type *menu* to try again.")], _idle()
    return await _request_delivery_details(sender, order, cfg, notes)


async def _checkout_from_cart(sender: str, cfg: dict[str, Any]):
    items = cart.get_items(sender)
    if not items:
        return await _view_cart(sender, cfg)
    specs = [{"product_id": i["product_id"], "quantity": i["quantity"]} for i in items]
    order, notes = orders.create_order(sender, specs, cfg)
    if not order:
        return [m.text("None of your cart items are available anymore."), _nav_list()], _shopping()
    cart.mark_checked_out(sender)
    return await _request_delivery_details(sender, order, cfg, notes)


async def _request_delivery_details(sender: str, order: dict[str, Any], cfg: dict[str, Any], notes: list[str]):
    msgs: list[dict[str, Any]] = []
    if notes:
        msgs.append(m.text("\n".join(notes)))
    msgs.append(m.text(_order_summary_text(order, cfg)))

    # Primary: a WhatsApp Flow form (flow_token carries the order number back).
    if cfg["flow_id"]:
        msgs.append(
            m.flow(
                flow_id=cfg["flow_id"],
                flow_token=order["order_number"],
                body="Please enter your delivery details to continue to payment.",
                cta_text="Enter details",
                header_text="Delivery details",
            )
        )
        # The submission arrives as an nfm_reply (handled regardless of state);
        # stay in the flow so the customer can keep browsing if they pause here.
        return msgs, _shopping()

    # Fallback (Flow not configured yet): collect details conversationally.
    msgs.append(
        m.text(
            "Please reply with your delivery details in this format:\n\n"
            "Name: <your name>\nAddress: <house, street>\nCity: <city>\nPincode: <pincode>"
        )
    )
    return msgs, {"step": "awaiting_address", "order_number": order["order_number"]}


def _parse_address_lines(text: str) -> dict[str, str]:
    aliases = {
        "name": "name", "address": "address", "city": "city",
        "pincode": "pincode", "pin": "pincode", "pin code": "pincode", "zip": "pincode",
    }
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            mapped = aliases.get(key.strip().lower())
            if mapped:
                out[mapped] = value.strip()
    return out


async def _handle_address_flow(sender: str, flow_data: dict[str, Any], cfg: dict[str, Any]):
    """Handle a completed Flow submission (``nfm_reply``).

    Two Flows submit here:
      * The "Open Store" data-exchange Flow (Path B) — its SUCCESS screen sends
        ``{flow_token: "store:…", order_number: "ORD-…"}``. Delivery details were
        already captured *inside* the Flow (the ADDRESS screen called
        ``orders.set_customer_details``), so we go straight to the payment link.
      * The standalone address Flow (fallback) — sends name/address/city/pincode
        with ``flow_token`` = the order number; we set the details here first.
    """
    token = str(flow_data.get("flow_token") or "")
    order_number = str(flow_data.get("order_number") or "")

    # Path B store Flow completion: order already at pending_payment with details.
    if token.startswith("store:") or (order_number and not flow_data.get("address")):
        order = orders.get_order_by_number(order_number) if order_number else None
        if not order:
            order = orders.latest_order_for_sender(sender)
        if not order:
            return [m.text("We couldn't find your order. Type *menu* to start again.")], _idle()
        order["items"] = orders.get_order_items(order["id"])
        return await _send_payment_link(sender, order, cfg)

    # Standalone address Flow: flow_token carries the order number.
    order = orders.get_order_by_number(token) if token else None
    if not order:
        order = orders.latest_order_for_sender(sender)
    if not order:
        return [m.text("We couldn't find your order. Type *menu* to start again.")], _idle()
    name = flow_data.get("name") or flow_data.get("full_name") or "Customer"
    address = {
        "address": flow_data.get("address") or "",
        "city": flow_data.get("city") or "",
        "pincode": flow_data.get("pincode") or flow_data.get("pin") or "",
    }
    return await _finalize_payment(sender, order, name, sender, address, cfg)


async def _handle_address_text(sender: str, text: str, state: dict[str, Any], cfg: dict[str, Any]):
    order = orders.get_order_by_number(state.get("order_number", "")) or orders.latest_order_for_sender(sender)
    if not order:
        return [m.text("We couldn't find your order. Type *menu* to start again.")], _idle()
    parsed = _parse_address_lines(text)
    if not parsed.get("address"):
        return [
            m.text(
                "Please include at least an Address line. Format:\n"
                "Name: ...\nAddress: ...\nCity: ...\nPincode: ..."
            )
        ], state
    name = parsed.get("name") or "Customer"
    address = {"address": parsed.get("address", ""), "city": parsed.get("city", ""), "pincode": parsed.get("pincode", "")}
    return await _finalize_payment(sender, order, name, sender, address, cfg)


async def _finalize_payment(sender, order, name, phone, address, cfg):
    """Save the delivery details (status → pending_payment), then send the
    payment link. Used by the chat/address-Flow path where details are collected
    here. (The Path B store Flow saves details inside the Flow and calls
    :func:`_send_payment_link` directly.)"""
    orders.set_customer_details(order["id"], name, phone, address)
    order = orders.get_order(order["id"]) or order
    order["items"] = orders.get_order_items(order["id"])
    return await _send_payment_link(sender, order, cfg)


async def _send_payment_link(sender, order, cfg):
    """Create + send the Razorpay "Pay ₹X" CTA for an order whose customer
    details are already saved (status pending_payment). Shared by the chat path
    and the Path B store Flow completion."""
    name = order.get("customer_name") or "there"
    currency = order.get("currency") or cfg["currency"]

    if not cfg["payment_enabled"] or not razorpay_service.is_configured():
        return [
            m.text(
                f"Thanks {name}! Your order *{order['order_number']}* is recorded. "
                "Our team will contact you shortly to arrange payment and delivery."
            )
        ], _idle()

    try:
        link = await asyncio.to_thread(razorpay_service.create_payment_link, order)
    except Exception:
        logger.exception("Failed to create Razorpay payment link for order=%s", order.get("order_number"))
        return [m.text("We couldn't generate a payment link right now. Our team will reach out to complete your order.")], _idle()

    orders.attach_payment_link(order["id"], str(link.get("id") or ""), str(link.get("short_url") or ""), link.get("order_id"))
    amount = m.format_money(order["total_minor"], currency)
    body = (
        f"Almost done, {name}! 🧾\n"
        f"Order *{order['order_number']}*\n"
        f"Amount due: *{amount}*\n\n"
        "Tap below to pay securely. You'll get a confirmation here as soon as your payment succeeds."
    )
    # Keep the flow open until payment is confirmed (the webhook resets to idle).
    return [m.cta_url(body, display_text=f"Pay {amount}", url=str(link.get("short_url") or ""), header_text="Payment")], {
        "step": "awaiting_payment",
        "order_number": order["order_number"],
    }


async def _resend_payment(sender: str, state: dict[str, Any], cfg: dict[str, Any]):
    """Customer messaged while a payment is outstanding — re-show the pay link
    (or the menu once it's paid/gone)."""
    order = orders.get_order_by_number(state.get("order_number", "")) or orders.latest_order_for_sender(sender)
    if not order:
        return _menu_messages(cfg, greet=False), _idle()
    if order.get("status") == "paid":
        return [m.text("✅ This order is already paid. Thank you!"), _main_menu(cfg)], _idle()
    url = order.get("razorpay_payment_link_url")
    if not url:
        return [m.text("Your order is recorded. Our team will reach out to arrange payment.")], _idle()
    amount = m.format_money(order.get("total_minor") or 0, order.get("currency") or cfg["currency"])
    body = (
        f"Your order *{order['order_number']}* is awaiting payment.\n"
        f"Amount due: *{amount}*\n\nTap below to pay securely."
    )
    return [m.cta_url(body, display_text=f"Pay {amount}", url=url, header_text="Payment")], {
        "step": "awaiting_payment",
        "order_number": order["order_number"],
    }


# ── Track order ──────────────────────────────────────────────────────────────
def _ask_track():
    return [m.text("Please reply with your order number (e.g. ORD-20260612-AB12CD).")], {"step": "awaiting_track"}


async def _handle_track(sender: str, text: str, cfg: dict[str, Any]):
    order = orders.get_order_by_number(text.strip())
    if not order:
        return [m.text("I couldn't find an order with that number. Double-check it and try again, or type *menu*.")], _idle()
    status_map = {
        "draft": "Awaiting checkout",
        "pending_payment": "Awaiting payment",
        "paid": "Paid ✅",
        "failed": "Payment failed",
        "cancelled": "Cancelled",
        "fulfilled": "Shipped / fulfilled 📦",
    }
    raw_status = str(order.get("status") or "")
    status = status_map.get(raw_status, raw_status)
    total = m.format_money(order.get("total_minor") or 0, order.get("currency") or cfg["currency"])
    return [m.text(f"📦 *Order {order['order_number']}*\nStatus: {status}\nTotal: {total}")], _idle()


# ── Reply dispatch ───────────────────────────────────────────────────────────
async def _handle_reply(sender: str, rid: str, state: dict[str, Any], cfg: dict[str, Any]):
    if rid == m.MENU_STORE:
        return await _open_store(sender, cfg)
    if rid == m.MENU_BROWSE:
        return await _browse_categories(sender, cfg)
    if rid == m.MENU_CART:
        return await _view_cart(sender, cfg)
    if rid == m.MENU_TRACK:
        return _ask_track()
    if rid == m.MENU_AI:
        return await _ai_intro(sender, cfg)
    if rid == m.FLOW_SEARCH:
        return await _search_prompt(sender, cfg)
    if rid == m.FLOW_EXIT:
        return await _flow_exit(sender, cfg)
    if rid == m.CART_CHECKOUT:
        return await _checkout_from_cart(sender, cfg)
    if rid == m.CART_CLEAR:
        return await _clear_cart(sender, cfg)
    if rid.startswith(m.CAT_PREFIX):
        return await _browse_category(sender, rid[len(m.CAT_PREFIX):], cfg)
    if rid.startswith(m.PROD_PREFIX):
        return await _product_detail(sender, rid[len(m.PROD_PREFIX):], cfg)
    if rid.startswith(m.ADD_PREFIX):
        return await _add_to_cart(sender, rid[len(m.ADD_PREFIX):], cfg)
    if rid.startswith(m.CITEM_PREFIX):
        return await _cart_item(sender, rid[len(m.CITEM_PREFIX):], cfg)
    if rid.startswith(m.INC_PREFIX):
        return await _cart_inc(sender, rid[len(m.INC_PREFIX):], cfg)
    if rid.startswith(m.DEC_PREFIX):
        return await _cart_dec(sender, rid[len(m.DEC_PREFIX):], cfg)
    if rid.startswith(m.RM_PREFIX):
        return await _cart_remove(sender, rid[len(m.RM_PREFIX):], cfg)
    # Unknown id: stay in the flow if shopping, otherwise show the menu.
    if state.get("step") == "shopping":
        return await _browse_categories(sender, cfg)
    return _menu_messages(cfg, greet=False), _idle()


# ── Router ───────────────────────────────────────────────────────────────────
async def _route(sender: str, parsed: dict[str, Any], state: dict[str, Any], conversation_data: list[dict[str, Any]]):
    cfg = await load_sales_config()

    # Storefront disabled → no menu/cart/checkout, but the Sales Bot still runs
    # as a pure AI assistant off its always-on Instructions section. Route every
    # message straight to the AI instead of replying that the shop is closed.
    if not cfg["products_enabled"]:
        question = (parsed.get("text") or parsed.get("display") or "").strip()
        return await _ai_only_reply(sender, question, cfg, conversation_data)

    kind = parsed["kind"]
    if kind == "order":
        return await _begin_checkout_from_order(sender, parsed.get("order_items") or [], cfg)
    if kind == "flow":
        return await _handle_address_flow(sender, parsed.get("flow") or {}, cfg)
    if kind == "reply":
        return await _handle_reply(sender, parsed.get("reply_id") or "", state, cfg)
    if kind == "text":
        raw = parsed["text"].strip()
        t = raw.lower()
        step = state.get("step")

        # An explicit "menu"/"exit" always leaves the flow.
        if t in MENU_EXIT_WORDS:
            return _menu_messages(cfg, greet=False), _idle()
        if step == "shopping":
            return await _do_search(sender, raw, cfg)
        if step == "ai_qa":
            return await _ai_answer(sender, raw, cfg, conversation_data)
        if step == "awaiting_address":
            return await _handle_address_text(sender, parsed["text"], state, cfg)
        if step == "awaiting_track":
            return await _handle_track(sender, parsed["text"], cfg)
        if step == "awaiting_payment":
            return await _resend_payment(sender, state, cfg)
        # Path C (text-only conversational flow): an in-flow turn is owned by the
        # unified interpreter — even greeting-like words are interpreted, not
        # treated as a menu escape (only MENU_EXIT_WORDS escapes, handled above).
        if step in CONV_STEPS:
            return await _handle_conv_turn(sender, raw, state, cfg, conversation_data)
        if t in GREETINGS:
            return _menu_messages(cfg, greet=True), _idle()
        # Free-typed message that isn't a greeting/command and isn't inside an
        # existing step → Path C entry (intent check). Everything above (menu,
        # Open Store, Talk to AI, the existing steps) is left exactly as it was.
        return await _conv_entry(sender, raw, cfg, conversation_data)

    return _menu_messages(cfg, greet=False), _idle()


# ── State reset (called after a confirmed payment) ───────────────────────────
def _reset_sales_state(sender: str) -> None:
    """Close the shopping flow for a sender by setting their last-entry
    ``sales_state`` to idle, so the next message shows the main menu again."""
    db = _db()
    try:
        existing = (
            db.table("whatsapp_conversations").select("id, conversation").eq("sender", sender).execute()
        )
        row = first_row(existing)
        if not row:
            return
        conv = row.get("conversation") or []
        if isinstance(conv, list) and conv and isinstance(conv[-1], dict):
            conv[-1]["sales_state"] = _idle()
            db.table("whatsapp_conversations").update({"conversation": conv}).eq("id", row["id"]).execute()
    except Exception:
        logger.exception("Sales: failed to reset state for sender=%s", sender)


# ── Public entry point ───────────────────────────────────────────────────────
async def handle_sales_message(sender: str, message: dict[str, Any], message_id: str | None) -> None:
    # Drop duplicate webhook deliveries (Meta retries) so a single Flow
    # completion can't send the payment link twice.
    if _already_handled(message_id):
        logger.info("Sales: duplicate message_id=%s ignored for sender=%s", message_id, sender)
        return

    db = _db()

    try:
        existing = (
            db.table("whatsapp_conversations")
            .select("id, conversation, ai_disabled, blocked")
            .eq("sender", sender)
            .execute()
        )
    except Exception:
        logger.exception("Sales: failed to load conversation for sender=%s", sender)
        return

    row = first_row(existing)
    conversation_data = (row.get("conversation") if row else None) or []
    if not isinstance(conversation_data, list):
        conversation_data = []
    record_id = row.get("id") if row else None
    if row and bool(row.get("blocked")):
        return
    ai_disabled = bool(row.get("ai_disabled")) if row else False

    parsed = parse_incoming(message)

    prev_state: dict[str, Any] = {}
    if conversation_data and isinstance(conversation_data[-1], dict):
        candidate = conversation_data[-1].get("sales_state")
        if isinstance(candidate, dict):
            prev_state = candidate

    now_iso = datetime.now(timezone.utc).isoformat()
    conversation_data.append({"query": parsed["display"], "response": "", "time": now_iso, "sales_state": prev_state})

    try:
        upsert = (
            db.table("whatsapp_conversations")
            .upsert(
                {
                    "sender": sender,
                    "client_name": sender,
                    "conversation": conversation_data,
                    "updated_at": now_iso,
                    "unread": True,
                },
                on_conflict="sender",
            )
            .execute()
        )
        up = first_row(upsert)
        if up:
            record_id = up.get("id")
    except Exception:
        logger.exception("Sales: failed to persist inbound for sender=%s", sender)

    # Human takeover from the dashboard — record the message, send nothing.
    if ai_disabled:
        logger.info("Sales: AI disabled for sender=%s; skipping automated response", sender)
        return

    if isinstance(message_id, str) and message_id:
        try:
            send_whatsapp_typing_indicator(message_id)
        except Exception:
            logger.exception("Sales: typing indicator failed for sender=%s", sender)

    try:
        outgoing, new_state = await _route(sender, parsed, prev_state, conversation_data)
    except Exception:
        logger.exception("Sales: routing failed for sender=%s", sender)
        outgoing, new_state = [m.text("Sorry, something went wrong. Type *menu* to start over.")], _idle()

    conversation_data[-1]["response"] = _summarize_outgoing(outgoing)
    conversation_data[-1]["sales_state"] = new_state
    try:
        if record_id:
            db.table("whatsapp_conversations").update(
                {"conversation": conversation_data, "updated_at": datetime.now(timezone.utc).isoformat()}
            ).eq("id", record_id).execute()
        else:
            db.table("whatsapp_conversations").update({"conversation": conversation_data}).eq("sender", sender).execute()
    except Exception:
        logger.exception("Sales: failed to persist response for sender=%s", sender)

    for msg in outgoing:
        if not isinstance(msg, dict):
            continue
        try:
            resp = send_whatsapp_message(sender, msg)
            if resp.status_code >= 400:
                logger.error("Sales send error %s: %s", resp.status_code, resp.text)
        except Exception:
            logger.exception("Sales: send failure for sender=%s", sender)


async def notify_order_paid(order: dict[str, Any]) -> None:
    """Send the post-payment confirmation message. Called by the Razorpay webhook
    after the order has been marked paid (idempotently). Also notifies the admin
    via WhatsApp and/or Email (independently, per the Sales Bot's payment
    notification toggles — both fire if both are enabled), and closes the
    shopping flow so the customer's next message shows the main menu again."""
    cfg = await load_sales_config()
    order["items"] = orders.get_order_items(order["id"])
    currency = order.get("currency") or cfg["currency"]
    lines = ["✅ *Payment received!*", f"Order *{order['order_number']}* is confirmed.", ""]
    for it in order.get("items", []):
        lines.append(f"• {it['name']} ×{it['quantity']}")
    lines.append(f"\nTotal paid: *{m.format_money(order.get('total_minor') or 0, currency)}*")
    lines.append("\nWe'll start preparing your order. Type *menu* anytime to track it. 🙏")
    try:
        send_whatsapp_message(order["sender"], m.text("\n".join(lines)))
    except Exception:
        logger.exception("Failed to send paid confirmation for order=%s", order.get("order_number"))

    # ── Admin notifications (Sales Bot payment toggles) ─────────────────────
    item_lines = [f"{it['name']} ×{it['quantity']}" for it in order.get("items", [])]
    total_label = m.format_money(order.get("total_minor") or 0, currency)
    customer_name = order.get("customer_name") or "Customer"
    sender = order.get("sender") or ""

    if cfg["payment_notify_whatsapp"] and settings.admin_whatsapp_number:
        try:
            admin_body = email_service.build_order_paid_body(
                order["order_number"], customer_name, sender, item_lines, total_label
            )
            resp = send_whatsapp_text(settings.admin_whatsapp_number, admin_body)
            if resp.status_code >= 400:
                logger.error(
                    "Admin WhatsApp payment notification failed %s: %s", resp.status_code, resp.text,
                )
            else:
                logger.info("Admin WhatsApp payment notification sent to %s", settings.admin_whatsapp_number)
        except Exception:
            logger.exception("Failed to send admin WhatsApp payment notification for order=%s", order.get("order_number"))

    if cfg["payment_notify_email"]:
        try:
            await asyncio.to_thread(
                email_service.send_order_paid_email,
                order["order_number"], customer_name, sender, item_lines, total_label,
                email_enabled=True,
            )
        except Exception:
            logger.exception("Failed to send admin email payment notification for order=%s", order.get("order_number"))

    # Payment complete → close the flow for this sender.
    _reset_sales_state(order.get("sender") or "")
