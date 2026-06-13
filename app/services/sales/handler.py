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
import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.db.supabase_client import first_row, supabase, supabase_admin
from app.services.whatsapp import send_whatsapp_message, send_whatsapp_typing_indicator
from app.services.sales import ai_chat, messaging as m
from app.services.sales import cart, catalog, orders, razorpay_service

logger = logging.getLogger("whatsapp")

# Greetings open the menu when the customer is idle.
GREETINGS = {"hi", "hello", "hey", "menu", "start", "main menu", "help", "hi there"}
# Words that always exit the shopping flow back to the main menu.
MENU_EXIT_WORDS = {"menu", "main menu", "home", "exit", "cancel", "quit"}

WELCOME = "Welcome to Rhodnet AI, You can purchase all the products at any time."

# How many product cards to render per category/search screen.
_MAX_CARDS = 8


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


# ── Talk to AI (LLM Q&A over the catalog) ────────────────────────────────────
def _ai_nav_buttons() -> list[tuple[str, str]]:
    return [(m.MENU_BROWSE, "🛍️ Browse products"), (m.FLOW_EXIT, "🏠 Main menu")]


async def _ai_intro(sender: str, cfg: dict[str, Any]):
    body = (
        "🤖 You're chatting with our AI assistant. Ask me anything about our "
        "products, prices, or availability.\n\nTap *🏠 Main menu* anytime to go back."
    )
    return [m.buttons(body, _ai_nav_buttons())], {"step": "ai_qa"}


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
        f"You are also the AI shopping assistant for {cfg['bot_name']}, a WhatsApp store. "
        "For questions about products, prices, or availability, answer using ONLY the "
        "product catalog below. Be concise and friendly (2-4 short sentences, suitable "
        "for WhatsApp). If something isn't in the catalog, say you don't carry it and "
        "suggest browsing. Prices are final; never invent products or prices.\n\n"
        f"CATALOG:\n{catalog_text}"
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

    if not cfg["products_enabled"]:
        return [m.text("Our shop isn't open right now. Please check back soon.")], _idle()

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
        if t in GREETINGS:
            return _menu_messages(cfg, greet=True), _idle()
        return _menu_messages(cfg, greet=False), _idle()

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
    after the order has been marked paid (idempotently). Also closes the shopping
    flow so the customer's next message shows the main menu again."""
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
    # Payment complete → close the flow for this sender.
    _reset_sales_state(order.get("sender") or "")
