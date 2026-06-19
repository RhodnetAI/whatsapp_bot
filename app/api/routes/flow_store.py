"""Data-exchange endpoint for the Sales Bot "Open Store" Flow (Path B).

This is the backend half of the single dynamic WhatsApp Flow that replaces the
old per-message browsing path. The customer taps **🏬 Open Store** in the main
menu (``handler._main_menu``); that opens one Flow "sheet" whose every tap calls
this endpoint behind the scenes — no chat messages are exchanged during
navigation. Only the final ``SUCCESS`` screen emits an ``nfm_reply`` back to
``/webhook``, which ``handler`` turns into the same Razorpay "Pay ₹X" hand-off
used by the chat path.

The endpoint is a **thin adapter**: it decrypts the request (see
``flow_crypto``), dispatches on ``screen`` + the in-screen ``data`` payload, and
calls the *same* ``catalog`` / ``cart`` / ``orders`` functions the chat handler
calls — then encrypts the next screen + data back. ``cart.py`` and ``orders.py``
are untouched; this is what keeps Path B from being a parallel system.

``flow_token`` is ``"store:<sender>"`` (set when the Flow message is sent), so
the sender — and therefore their cart / conversation row — is known on every
request without an extra lookup.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.core.config import settings
from app.services.sales import catalog, cart, orders
from app.services.sales import flow_crypto, flow_images
from app.services.sales import messaging as m
from app.services.sales.handler import load_sales_config

logger = logging.getLogger("whatsapp")

router = APIRouter(tags=["sales-flow"])

DATA_API_VERSION = "3.0"
_MAX_QTY = 10  # cap the quantity stepper / dropdown options
# Cap products per PRODUCTS screen — each carries a base64 thumbnail, and the
# whole Flow response payload must stay under WhatsApp's 1 MB limit.
_MAX_PRODUCTS = 10


# ── Screen-data helpers ──────────────────────────────────────────────────────
def _resp(screen: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"version": DATA_API_VERSION, "screen": screen, "data": data or {}}


def _sender_from_token(flow_token: str) -> str:
    """``"store:+91…"`` → ``"+91…"``. Tolerates a bare number too."""
    token = (flow_token or "").strip()
    if token.startswith("store:"):
        return token[len("store:"):]
    return token


def _qty_options(maximum: int) -> list[dict[str, str]]:
    top = max(1, min(int(maximum or _MAX_QTY), _MAX_QTY))
    return [{"id": str(n), "title": str(n)} for n in range(1, top + 1)]


def _price_str(row: dict[str, Any], cfg: dict[str, Any]) -> str:
    return m.format_money(row.get("price_minor") or 0, row.get("currency") or cfg["currency"])


def _stock_label(row: dict[str, Any]) -> str:
    stock = row.get("stock_quantity")
    if stock is None:
        return "In stock"
    return f"In stock: {int(stock)}" if int(stock) > 0 else "Out of stock"


def _clip(text: Any, limit: int) -> str:
    s = str(text or "").strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _nav_item(
    item_id: str,
    title: str,
    payload: dict[str, Any],
    *,
    metadata: str | None = None,
    description: str | None = None,
    image_b64: str | None = None,
) -> dict[str, Any]:
    """One ``NavigationList`` item. WhatsApp caps title=30, description=20,
    metadata=80 chars; ``start.image`` is a base64 thumbnail (≤100 KB) and is
    omitted when unavailable. Each item carries its own data_exchange action."""
    main: dict[str, Any] = {"title": _clip(title, 30)}
    if description:
        main["description"] = _clip(description, 20)
    if metadata:
        main["metadata"] = _clip(metadata, 80)
    item: dict[str, Any] = {
        "id": str(item_id),
        "main-content": main,
        "on-click-action": {"name": "data_exchange", "payload": payload},
    }
    if image_b64:
        item["start"] = {"image": image_b64, "alt-text": _clip(title, 30)}
    return item


# ── Screen builders ──────────────────────────────────────────────────────────
def _categories_screen(sender: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """CATEGORIES is a NavigationList-only screen (the component must occupy the
    screen alone), so the cart link is appended as a list item."""
    cats = catalog.get_categories()
    count = sum(i["quantity"] for i in cart.get_items(sender)) if sender else 0
    items = [_nav_item(c, c, {"category": c}) for c in cats]
    cart_title = f"🛒 View cart ({count})" if count else "🛒 View cart"
    items.append(_nav_item("__cart__", cart_title, {"nav": "cart"}, metadata="Review & checkout"))
    if not cats:
        items.insert(0, _nav_item("__none__", "No products yet", {"nav": "categories"}, metadata="Check back soon"))
    return _resp("CATEGORIES", {"categories": items})


def _products_screen(category: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """PRODUCTS is a NavigationList-only screen: product rows carry a base64
    thumbnail + price/stock, and cart / back links are appended as list items."""
    rows = catalog.get_products_by_category(category)[:_MAX_PRODUCTS]
    thumbs = flow_images.thumbnails_b64([r.get("image_url") or "" for r in rows])
    items: list[dict[str, Any]] = []
    for r in rows:
        meta = f"{_price_str(r, cfg)} · {_stock_label(r)}"
        items.append(
            _nav_item(
                str(r["id"]),
                r.get("name") or "Item",
                {"product_id": str(r["id"])},
                metadata=meta,
                image_b64=thumbs.get(r.get("image_url") or "") or None,
            )
        )
    if not rows:
        items.append(_nav_item("__empty__", "No products here", {"nav": "categories"}, metadata="Try another category"))
    items.append(_nav_item("__cart__", "🛒 View cart", {"nav": "cart"}, metadata="Review & checkout"))
    items.append(_nav_item("__cats__", "📋 Back to categories", {"nav": "categories"}, metadata="Browse other categories"))
    return _resp("PRODUCTS", {"products": items})


def _product_detail_screen(product_id: str, cfg: dict[str, Any]) -> dict[str, Any]:
    row = catalog.get_product(product_id)
    if not row:
        # Product vanished/inactive — bounce back to categories.
        return _categories_screen("", cfg)
    stock = row.get("stock_quantity")
    in_stock = stock is None or int(stock) > 0
    max_qty = _MAX_QTY if stock is None else int(stock)
    return _resp(
        "PRODUCT_DETAIL",
        {
            "product_id": str(row["id"]),
            "title": row.get("name") or "Item",
            "price": _price_str(row, cfg),
            "description": (row.get("description") or "").strip() or "—",
            "stock_label": _stock_label(row),
            "in_stock": in_stock,
            # Always a valid base64 JPEG (placeholder when the product has no image).
            "image": flow_images.detail_b64(row.get("image_url") or ""),
            "qty_options": _qty_options(max_qty) if in_stock else [{"id": "1", "title": "1"}],
        },
    )


def _cart_screen(sender: str, cfg: dict[str, Any], flash: str = "") -> dict[str, Any]:
    items = cart.get_items(sender)
    lines: list[dict[str, str]] = []
    text_lines: list[str] = []
    subtotal = 0
    for i in items:
        line_total = i["unit_price_minor"] * i["quantity"]
        subtotal += line_total
        text_lines.append(f"• {i['name']} ×{i['quantity']} — {m.format_money(line_total, cfg['currency'])}")
        lines.append({"id": str(i["product_id"]), "title": f"{i['name']} ×{i['quantity']}"[:30]})

    if items:
        shipping = orders._compute_shipping(subtotal, cfg)
        total = subtotal + shipping
        summary = "\n".join(text_lines)
        summary += f"\n\nSubtotal: {m.format_money(subtotal, cfg['currency'])}"
        if shipping:
            summary += f"\nShipping: {m.format_money(shipping, cfg['currency'])}"
        summary += f"\nTotal: {m.format_money(total, cfg['currency'])}"
    else:
        summary = "Your cart is empty. Tap *Continue shopping* to add items."

    if flash:
        summary = f"{flash}\n\n{summary}"

    # The cart's quantity dropdown leads with "0 — remove" so a single "Update /
    # remove" action covers both editing and deleting a line (the CART screen is
    # capped at 2 EmbeddedLinks, so remove can't be its own row). set_qty already
    # deletes the line when the new quantity is 0.
    cart_qty_options = [{"id": "0", "title": "0 — remove"}] + _qty_options(_MAX_QTY)
    return _resp(
        "CART",
        {
            "cart_text": summary,
            "has_items": bool(items),
            "lines": lines or [{"id": "", "title": "Cart is empty"}],
            "qty_options": cart_qty_options,
        },
    )


def _track_screen(order_number: str, cfg: dict[str, Any]) -> dict[str, Any]:
    order = orders.get_order_by_number(order_number) if order_number else None
    if not order_number:
        result = "Enter your order number above and tap *Check status*."
    elif not order:
        result = f"No order found for “{order_number}”. Double-check the number and try again."
    else:
        status_map = {
            "draft": "Awaiting checkout",
            "pending_payment": "Awaiting payment",
            "paid": "Paid ✅",
            "failed": "Payment failed",
            "cancelled": "Cancelled",
            "fulfilled": "Shipped / fulfilled 📦",
        }
        raw = str(order.get("status") or "")
        total = m.format_money(order.get("total_minor") or 0, order.get("currency") or cfg["currency"])
        result = f"Order {order['order_number']}\nStatus: {status_map.get(raw, raw)}\nTotal: {total}"
    return _resp("TRACK_ORDER", {"result": result, "order_number": order_number or ""})


async def _checkout_screen(sender: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Create a draft order from the cart and move to the ADDRESS screen — same
    server-side stock-check/totals logic the chat checkout uses."""
    items = cart.get_items(sender)
    if not items:
        return _cart_screen(sender, cfg, flash="Your cart is empty.")
    specs = [{"product_id": i["product_id"], "quantity": i["quantity"]} for i in items]
    order, notes = orders.create_order(sender, specs, cfg)
    if not order:
        return _cart_screen(sender, cfg, flash="None of your cart items are available anymore.")
    cart.mark_checked_out(sender)
    currency = order.get("currency") or cfg["currency"]
    summary = (
        f"Order {order['order_number']}\n"
        f"Total payable: {m.format_money(order['total_minor'], currency)}"
    )
    if notes:
        summary = "\n".join(notes) + "\n\n" + summary
    return _resp(
        "ADDRESS",
        {"order_number": order["order_number"], "summary": summary},
    )


def _success_screen(order_number: str, flow_token: str) -> dict[str, Any]:
    """Terminal screen. ``extension_message_response.params`` is delivered to
    ``/webhook`` as an ``nfm_reply`` — the chat handler creates the payment link
    from there, exactly as it does for the address-Flow submission today."""
    return {
        "version": DATA_API_VERSION,
        "screen": "SUCCESS",
        "data": {
            "extension_message_response": {
                "params": {"flow_token": flow_token, "order_number": order_number}
            }
        },
    }


# ── Dispatcher ───────────────────────────────────────────────────────────────
async def _dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    action = payload.get("action")
    screen = payload.get("screen") or "STORE_HOME"
    data = payload.get("data") or {}
    flow_token = payload.get("flow_token") or ""
    sender = _sender_from_token(flow_token)

    # Health check — Meta pings the endpoint periodically.
    if action == "ping":
        return {"data": {"status": "active"}}

    # Client error notification — acknowledge so the Flow doesn't break.
    if isinstance(data, dict) and (data.get("error") or data.get("error_message")):
        logger.warning("Store Flow client error: %s", data)
        return {"data": {"acknowledged": True}}

    cfg = await load_sales_config()

    # Shop turned off mid-session — send them home with an empty catalog.
    if not cfg["products_enabled"]:
        return _resp("CATEGORIES", {"categories": [], "has_categories": False, "cart_label": "🛒 View cart"})

    # First screen fetch (only fires if the Flow is opened with data_exchange).
    if action == "INIT":
        return _resp("STORE_HOME", {})

    if action == "BACK":
        # Defensive: client normally handles back-nav itself. Land them on the
        # category list rather than a stale screen.
        return _categories_screen(sender, cfg)

    # action == "data_exchange" (or anything else) → route by screen + payload.
    if screen == "STORE_HOME":
        intent = (data.get("intent") or "").strip().lower()
        if intent == "cart":
            return _cart_screen(sender, cfg)
        if intent == "track":
            return _track_screen("", cfg)
        return _categories_screen(sender, cfg)  # default: browse

    if screen == "CATEGORIES":
        if (data.get("nav") or "").lower() == "cart":
            return _cart_screen(sender, cfg)
        return _products_screen(data.get("category") or "", cfg)

    if screen == "PRODUCTS":
        if (data.get("nav") or "").lower() == "cart":
            return _cart_screen(sender, cfg)
        if (data.get("nav") or "").lower() == "categories":
            return _categories_screen(sender, cfg)
        return _product_detail_screen(data.get("product_id") or "", cfg)

    if screen == "PRODUCT_DETAIL":
        nav = (data.get("nav") or "").lower()
        if nav == "categories":
            return _categories_screen(sender, cfg)
        if nav == "cart":
            return _cart_screen(sender, cfg)
        # Add to cart.
        product_id = data.get("product_id") or ""
        try:
            qty = max(1, int(data.get("qty") or 1))
        except (TypeError, ValueError):
            qty = 1
        ok = cart.add_item(sender, product_id, qty)
        flash = "✓ Added to cart." if ok else "Sorry, that item isn't available."
        return _cart_screen(sender, cfg, flash=flash)

    if screen == "CART":
        cart_action = (data.get("cart_action") or "").strip().lower()
        product_id = data.get("product_id") or ""
        if cart_action == "remove" and product_id:
            cart.remove_item(sender, product_id)
            return _cart_screen(sender, cfg, flash="Item removed.")
        if cart_action == "set_qty" and product_id:
            try:
                new_qty = int(data.get("qty") or 0)
            except (TypeError, ValueError):
                new_qty = 0
            current = next((i["quantity"] for i in cart.get_items(sender) if i["product_id"] == product_id), 0)
            cart.update_quantity(sender, product_id, new_qty - current)
            flash = "Item removed." if new_qty <= 0 else "Quantity updated."
            return _cart_screen(sender, cfg, flash=flash)
        if cart_action == "continue":
            return _categories_screen(sender, cfg)
        if cart_action == "checkout":
            return await _checkout_screen(sender, cfg)
        return _cart_screen(sender, cfg)

    if screen == "TRACK_ORDER":
        return _track_screen((data.get("order_number") or "").strip(), cfg)

    if screen == "ADDRESS":
        order_number = (data.get("order_number") or "").strip()
        order = orders.get_order_by_number(order_number) if order_number else None
        if not order:
            order = orders.latest_order_for_sender(sender)
        if not order:
            # Nothing to attach to — acknowledge and end.
            return _success_screen(order_number, flow_token)
        name = (data.get("name") or "Customer").strip() or "Customer"
        address = {
            "address": (data.get("address") or "").strip(),
            "city": (data.get("city") or "").strip(),
            "pincode": str(data.get("pincode") or "").strip(),
        }
        orders.set_customer_details(order["id"], name, sender, address)
        return _success_screen(order["order_number"], flow_token)

    # Unknown screen — recover to the category list.
    return _categories_screen(sender, cfg)


# ── Route ────────────────────────────────────────────────────────────────────
@router.get("/flows/store/public-key")
async def store_public_key():
    """Diagnostic: returns the public key derived from the private key the server
    has actually loaded. Upload exactly this to WhatsApp
    (``POST /{phone-number-id}/whatsapp_business_encryption``) so the registered
    public key is guaranteed to match. Public keys are not secret."""
    if not flow_crypto.is_configured():
        return JSONResponse({"error": "WHATSAPP_FLOW_PRIVATE_KEY not configured"}, status_code=500)
    try:
        return PlainTextResponse(content=flow_crypto.public_key_pem(), status_code=200)
    except Exception:
        logger.exception("Store Flow: could not derive public key from private key")
        return JSONResponse({"error": "could not load private key (check the PEM)"}, status_code=500)


@router.post("/flows/store/data-exchange")
async def store_data_exchange(request: Request):
    """Encrypted WhatsApp Flow data-exchange endpoint (separate from ``/webhook``)."""
    if not flow_crypto.is_configured():
        logger.error("Store Flow request received but WHATSAPP_FLOW_PRIVATE_KEY is not configured")
        return JSONResponse({"error": "flow encryption not configured"}, status_code=500)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid body"}, status_code=400)

    try:
        payload, aes_key, iv = flow_crypto.decrypt_request(body)
    except flow_crypto.FlowEncryptionError:
        logger.exception("Store Flow: failed to decrypt request")
        # 421 tells WhatsApp to refresh our public key and retry.
        return JSONResponse({"error": "decryption failed"}, status_code=421)

    try:
        response = await _dispatch(payload)
    except Exception:
        logger.exception("Store Flow: dispatch failed for screen=%s", payload.get("screen"))
        # Keep the Flow alive: send the user back to a safe screen.
        response = _resp("CATEGORIES", {"categories": [], "has_categories": False, "cart_label": "🛒 View cart"})

    try:
        encrypted = flow_crypto.encrypt_response(response, aes_key, iv)
    except Exception:
        logger.exception("Store Flow: failed to encrypt response")
        return JSONResponse({"error": "encryption failed"}, status_code=500)

    return PlainTextResponse(content=encrypted, status_code=200)
