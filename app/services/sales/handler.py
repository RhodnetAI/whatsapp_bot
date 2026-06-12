"""Sales Bot conversation pipeline.

Entry point ``handle_sales_message`` is called by the webhook ONLY when
``sales_bot.is_selected`` is true. It owns its own inbound parsing, conversation
persistence (same ``whatsapp_conversations`` table + JSON-array pattern as the
Information Bot, with transient position stored under ``sales_state`` on the last
entry), and outbound interactive messaging. The Information Bot code path is never
touched from here.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.db.supabase_client import first_row, supabase, supabase_admin
from app.services.whatsapp import send_whatsapp_message, send_whatsapp_typing_indicator
from app.services.sales import messaging as m
from app.services.sales import cart, catalog, orders, razorpay_service

logger = logging.getLogger("whatsapp")

GREETINGS = {"hi", "hello", "hey", "menu", "start", "main menu", "help", "hi there"}


def _db():
    return supabase_admin if supabase_admin is not None else supabase


def _idle() -> dict[str, Any]:
    return {"step": "idle"}


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
    }


# ── Menu ─────────────────────────────────────────────────────────────────────
def _main_menu(cfg: dict[str, Any]) -> dict[str, Any]:
    rows = [
        m.list_row(m.MENU_BROWSE, "🛍️ Browse products", "See what's available"),
        m.list_row(m.MENU_CART, "🛒 View cart", "Review items & checkout"),
        m.list_row(m.MENU_TRACK, "📦 Track order", "Check an order's status"),
        m.list_row(m.MENU_HUMAN, "💬 Talk to a human", "Hand off to our team"),
    ]
    return m.list_message(
        body=f"Welcome to {cfg['bot_name']}! How can I help you today?",
        button_text="Menu",
        sections=[m.section("Options", rows)],
    )


def _menu_messages(cfg: dict[str, Any], greet: bool) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    if greet and cfg["greeting"]:
        msgs.append(m.text(cfg["greeting"]))
    msgs.append(_main_menu(cfg))
    return msgs


# ── Browsing ─────────────────────────────────────────────────────────────────
async def _browse_categories(sender: str, cfg: dict[str, Any]):
    rows = catalog.fetch_active_rows()
    if not rows:
        return [m.text("No products are available right now. Please check back soon."), _main_menu(cfg)], _idle()
    categories = catalog.distinct_active_categories(rows)
    section_rows = [m.list_row(f"{m.CAT_PREFIX}{c}", c, "") for c in categories[:10]]
    msg = m.list_message(
        body="Browse our products by category:",
        button_text="Categories",
        sections=[m.section("Categories", section_rows)],
    )
    return [msg], _idle()


def _rows_in_category(category: str) -> list[dict[str, Any]]:
    out = []
    for r in catalog.fetch_active_rows():
        cat = (r.get("category") or "").strip() or "Other"
        if cat == category:
            out.append(r)
    return out


async def _browse_category(sender: str, category: str, cfg: dict[str, Any]):
    rows = _rows_in_category(category)
    if not rows:
        return [m.text("No products in this category yet."), _main_menu(cfg)], _idle()

    # Native path: Multi-Product Message from the connected Meta catalog.
    if cfg["catalog_id"]:
        product_items = [
            {"product_retailer_id": r["retailer_id"]} for r in rows[:30] if r.get("retailer_id")
        ]
        if product_items:
            msg = m.product_list(
                catalog_id=cfg["catalog_id"],
                header_text=category,
                body="Tap a product to view it, add what you want to your cart, then send the cart to check out.",
                sections=[{"title": category[:24], "product_items": product_items}],
            )
            return [msg], _idle()

    # Fallback path: a list linking to our own product-detail cards.
    section_rows = [
        m.list_row(
            f"{m.PROD_PREFIX}{r['id']}",
            r.get("name") or "Item",
            m.format_money(r.get("price_minor") or 0, r.get("currency") or cfg["currency"]),
        )
        for r in rows[:10]
    ]
    msg = m.list_message(
        body=f"{category} — tap a product to see details:",
        button_text="Products",
        sections=[m.section(category, section_rows)],
    )
    return [msg], _idle()


async def _product_detail(sender: str, product_id: str, cfg: dict[str, Any]):
    row = catalog.get_row(product_id)
    if not row or not row.get("is_active", True):
        return [m.text("That product isn't available."), _main_menu(cfg)], _idle()
    price = m.format_money(row.get("price_minor") or 0, row.get("currency") or cfg["currency"])
    stock = row.get("stock_quantity")
    if stock is None:
        stock_line = ""
    elif int(stock) > 0:
        stock_line = f"\nIn stock: {int(stock)}"
    else:
        stock_line = "\n⚠️ Out of stock"
    body = f"*{row.get('name')}*\n{price}{stock_line}\n\n{row.get('description') or ''}".strip()
    opts = [
        (f"{m.ADD_PREFIX}{product_id}", "Add to cart"),
        (m.MENU_CART, "View cart"),
        (m.MENU_BROWSE, "Browse more"),
    ]
    msgs: list[dict[str, Any]] = []
    if row.get("image_url"):
        msgs.append(m.image(row["image_url"]))
    msgs.append(m.buttons(body, opts))
    return msgs, _idle()


async def _ask_quantity(sender: str, product_id: str, cfg: dict[str, Any]):
    row = catalog.get_row(product_id)
    if not row:
        return [m.text("That product isn't available.")], _idle()
    opts = [
        (f"{m.QTY_PREFIX}{product_id}:1", "1"),
        (f"{m.QTY_PREFIX}{product_id}:2", "2"),
        (f"{m.QTY_PREFIX}{product_id}:3", "3"),
    ]
    return [m.buttons(f"How many *{row.get('name')}* would you like?", opts)], _idle()


async def _add_to_cart(sender: str, product_id: str, qty: int, cfg: dict[str, Any]):
    if not cart.add_item(sender, product_id, qty):
        return [m.text("Sorry, that product isn't available.")], _idle()
    count = sum(i["quantity"] for i in cart.get_items(sender))
    opts = [(m.CART_CHECKOUT, "Checkout"), (m.MENU_BROWSE, "Keep shopping"), (m.MENU_CART, "View cart")]
    return [m.buttons(f"Added! 🛒 Your cart now has {count} item(s).", opts)], _idle()


# ── Cart ─────────────────────────────────────────────────────────────────────
async def _view_cart(sender: str, cfg: dict[str, Any]):
    if cfg["catalog_id"]:
        return [
            m.text(
                "Your cart lives inside WhatsApp 🛒\n\nTap the *cart* on any product message to "
                "review items and adjust quantities, then tap *Send* to place your order — "
                "I'll take it from there."
            ),
            _main_menu(cfg),
        ], _idle()

    items = cart.get_items(sender)
    if not items:
        return [m.text("Your cart is empty. Tap *Browse products* to add something."), _main_menu(cfg)], _idle()
    lines = []
    subtotal = 0
    for i in items:
        line_total = i["unit_price_minor"] * i["quantity"]
        subtotal += line_total
        lines.append(f"• {i['name']} ×{i['quantity']} — {m.format_money(line_total, cfg['currency'])}")
    body = "🛒 *Your cart*\n" + "\n".join(lines) + f"\n\nSubtotal: {m.format_money(subtotal, cfg['currency'])}"
    opts = [(m.CART_CHECKOUT, "Checkout"), (m.CART_CLEAR, "Clear cart"), (m.MENU_BROWSE, "Keep shopping")]
    return [m.buttons(body, opts)], _idle()


async def _clear_cart(sender: str, cfg: dict[str, Any]):
    cart.clear_cart(sender)
    return [m.text("Your cart has been cleared."), _main_menu(cfg)], _idle()


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
        return [m.text("Your cart is empty. Add a product first."), _main_menu(cfg)], _idle()
    specs = [{"product_id": i["product_id"], "quantity": i["quantity"]} for i in items]
    order, notes = orders.create_order(sender, specs, cfg)
    if not order:
        return [m.text("None of your cart items are available anymore."), _main_menu(cfg)], _idle()
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
        return msgs, _idle()

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
    order_number = str(flow_data.get("flow_token") or "")
    order = orders.get_order_by_number(order_number) if order_number else None
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
    orders.set_customer_details(order["id"], name, phone, address)
    order = orders.get_order(order["id"]) or order
    order["items"] = orders.get_order_items(order["id"])
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
    return [m.cta_url(body, display_text=f"Pay {amount}", url=str(link.get("short_url") or ""), header_text="Payment")], _idle()


# ── Track order & human handoff ──────────────────────────────────────────────
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


async def _human_handoff(sender: str, cfg: dict[str, Any]):
    try:
        _db().table("whatsapp_conversations").update({"ai_disabled": True, "unread": True}).eq("sender", sender).execute()
    except Exception:
        logger.exception("Failed to set human handoff for sender=%s", sender)
    return [m.text("Got it — I've let our team know. A human will reply here shortly. 🙌")], _idle()


# ── Reply dispatch ───────────────────────────────────────────────────────────
async def _handle_reply(sender: str, rid: str, state: dict[str, Any], cfg: dict[str, Any]):
    if rid == m.MENU_BROWSE:
        return await _browse_categories(sender, cfg)
    if rid == m.MENU_CART:
        return await _view_cart(sender, cfg)
    if rid == m.MENU_TRACK:
        return _ask_track()
    if rid == m.MENU_HUMAN:
        return await _human_handoff(sender, cfg)
    if rid == m.CART_CHECKOUT:
        return await _checkout_from_cart(sender, cfg)
    if rid == m.CART_CLEAR:
        return await _clear_cart(sender, cfg)
    if rid.startswith(m.CAT_PREFIX):
        return await _browse_category(sender, rid[len(m.CAT_PREFIX):], cfg)
    if rid.startswith(m.PROD_PREFIX):
        return await _product_detail(sender, rid[len(m.PROD_PREFIX):], cfg)
    if rid.startswith(m.ADD_PREFIX):
        return await _ask_quantity(sender, rid[len(m.ADD_PREFIX):], cfg)
    if rid.startswith(m.QTY_PREFIX):
        pid, _, n = rid[len(m.QTY_PREFIX):].rpartition(":")
        return await _add_to_cart(sender, pid, int(n) if n.isdigit() else 1, cfg)
    return _menu_messages(cfg, greet=False), _idle()


# ── Router ───────────────────────────────────────────────────────────────────
async def _route(sender: str, parsed: dict[str, Any], state: dict[str, Any]):
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
        t = parsed["text"].strip().lower()
        if t in GREETINGS:
            return _menu_messages(cfg, greet=True), _idle()
        step = state.get("step")
        if step == "awaiting_address":
            return await _handle_address_text(sender, parsed["text"], state, cfg)
        if step == "awaiting_track":
            return await _handle_track(sender, parsed["text"], cfg)
        return _menu_messages(cfg, greet=False), _idle()

    return _menu_messages(cfg, greet=False), _idle()


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
        outgoing, new_state = await _route(sender, parsed, prev_state)
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
    after the order has been marked paid (idempotently)."""
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
