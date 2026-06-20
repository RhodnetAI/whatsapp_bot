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
# NavigationList screens load **cumulatively**: each "Show more" tap re-renders
# the SAME screen with the list grown from the start (old items kept + new ones
# appended), not a replacing window. A NavigationList holds at most ~20 items and
# each PRODUCTS row carries a base64 thumbnail (the whole response must stay under
# WhatsApp's 1 MB limit), so growth is capped per screen; beyond the cap the list
# can't grow further (a platform limit) and the customer narrows down by
# category/search. The ``offset`` in a "Show more" payload is the *target number
# of items to show* (not a window start). Re-fetching thumbnails for the whole
# visible set on each tap is cheap thanks to ``flow_images``' disk cache.
_PRODUCTS_PAGE = 8      # how many more products each "Show more" reveals
_PRODUCTS_MAX = 12      # max products kept on one screen (payload / list-size safe)
_CATEGORIES_PAGE = 8    # how many more categories each "Show more" reveals
_CATEGORIES_MAX = 17    # max categories on one screen (+ "Show more" + 2 shortcuts ≤ 20)


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


def _to_int(value: Any, default: int = 0) -> int:
    """Parse a pagination ``offset`` (sent back as a string in the click payload)."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _price_str(row: dict[str, Any], cfg: dict[str, Any]) -> str:
    """Effective (discounted) unit price — used in list rows and cart lines."""
    return m.format_money(catalog.effective_unit_price_minor(row), row.get("currency") or cfg["currency"])


def _price_detail(row: dict[str, Any], cfg: dict[str, Any]) -> str:
    """Detail-screen price line: the sale price, plus the original M.R.P. and the
    percent off when the product is discounted."""
    currency = row.get("currency") or cfg["currency"]
    effective = catalog.effective_unit_price_minor(row)
    base = int(row.get("price_minor") or 0)
    discount = float(row.get("discount_percentage") or 0)
    if discount > 0 and effective < base:
        return f"{m.format_money(effective, currency)}  (M.R.P. {m.format_money(base, currency)}, {discount:.0f}% off)"
    return m.format_money(effective, currency)


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
# Browsing screens are NavigationList-only; the cart / home / track shortcuts are
# appended as list items (the header can't hold them), and "back" also works via
# WhatsApp's native header arrow.
def _rating_str(row: dict[str, Any]) -> str:
    avg = row.get("rating_average")
    if avg is None:
        return ""
    try:
        avg = float(avg)
    except (TypeError, ValueError):
        return ""
    count = row.get("rating_count")
    return f"⭐ {avg:.1f}" + (f" ({int(count)})" if count else "")


def _product_meta(row: dict[str, Any], cfg: dict[str, Any]) -> str:
    rating = _rating_str(row)
    tail = rating or _stock_label(row)
    return f"{_price_str(row, cfg)} · {tail}"


def _list_nav_items() -> list[dict[str, Any]]:
    """Cart / Track shortcuts appended to the CATEGORIES NavigationList. These are
    forward routes in the routing_model (CATEGORIES → CART / TRACK_ORDER). There is
    no "Home" shortcut: returning to a previous screen is a backward route, which
    WhatsApp forbids in the routing_model — the device's native back arrow does it."""
    return [
        _nav_item("__cart__", "🛒 Cart", {"nav": "cart"}, metadata="Review & checkout"),
        _nav_item("__track__", "📦 Track order", {"nav": "track"}, metadata="Check an order"),
    ]


def _categories_screen(sender: str, cfg: dict[str, Any], offset: int = 0) -> dict[str, Any]:
    """Category cards with a product count, plus the cart/track shortcuts.
    Cumulative: "Show more categories" grows the list from the start (old + new)
    until the per-screen cap, then stops (a NavigationList holds ~20 items)."""
    rows = catalog.fetch_active_rows()
    counts: dict[str, int] = {}
    for r in rows:
        c = (r.get("category") or "").strip() or "Other"
        counts[c] = counts.get(c, 0) + 1
    all_categories = catalog.get_categories()
    total = len(all_categories)
    # Cumulative count to show (always from the start), clamped to the cap.
    shown = max(_CATEGORIES_PAGE, min(offset or _CATEGORIES_PAGE, _CATEGORIES_MAX, total)) if total else 0
    items = [
        _nav_item(c, c, {"category": c}, metadata=f"{counts.get(c, 0)} product(s)")
        for c in all_categories[:shown]
    ]
    if not all_categories:
        items.append(_nav_item("__none__", "No products yet", {"category": ""}, metadata="Check back soon"))
    capped = min(total, _CATEGORIES_MAX)
    if shown < capped:
        items.append(_nav_item(
            "__more_cats__", "⬇️ Show more categories",
            {"nav": "more", "offset": str(min(shown + _CATEGORIES_PAGE, _CATEGORIES_MAX))},
            metadata=f"{capped - shown} more",
        ))
    elif total > _CATEGORIES_MAX:
        # Hit the per-screen cap but the catalog has more — can't grow further.
        items.append(_nav_item(
            "__cats_capped__", f"Showing first {_CATEGORIES_MAX} categories",
            {"nav": "more", "offset": str(shown)},
            metadata="Tap a category to narrow down",
        ))
    items += _list_nav_items()
    return _resp("CATEGORIES", {"categories": items})


def _product_list_screen(
    rows: list[dict[str, Any]],
    cfg: dict[str, Any],
    empty_hint: str,
    more_base: dict[str, Any] | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """Render a product NavigationList (image + name + price/rating) from rows.
    Cumulative: shows products from the start up to ``offset`` (clamped to
    ``_PRODUCTS_MAX``); "Show more" grows the list in place (old items kept + new
    appended). Beyond the cap the list can't grow (NavigationList ~20-item / 1 MB
    payload limit) — an info row suggests narrowing by category/search. The
    PRODUCTS screen has no cart/home/track shortcuts; the customer taps a product,
    "Show more", or the native back arrow. Shared by category, new-arrivals,
    best-sellers (and search) views."""
    total = len(rows)
    # Cumulative count to show (always from the start), clamped to the cap.
    shown = max(_PRODUCTS_PAGE, min(offset or _PRODUCTS_PAGE, _PRODUCTS_MAX, total)) if total else 0
    visible = rows[:shown]
    thumbs = flow_images.thumbnails_b64([r.get("image_url") or "" for r in visible])
    items: list[dict[str, Any]] = [
        _nav_item(
            str(r["id"]),
            r.get("name") or "Item",
            {"product_id": str(r["id"])},
            metadata=_product_meta(r, cfg),
            image_b64=thumbs.get(r.get("image_url") or "") or None,
        )
        for r in visible
    ]
    if not items:
        # No products → a self-refreshing placeholder (empty payload). PRODUCTS has
        # no backward route to CATEGORIES; the customer taps ← to go back.
        items.append(_nav_item("__empty__", "No products here", {}, metadata=empty_hint + " · tap ← to go back"))
    elif more_base is not None:
        capped = min(total, _PRODUCTS_MAX)
        if shown < capped:
            items.append(_nav_item(
                "__more__", "⬇️ Show more",
                {**more_base, "more": "1", "offset": str(min(shown + _PRODUCTS_PAGE, _PRODUCTS_MAX))},
                metadata=f"{capped - shown} more product(s)",
            ))
        elif total > _PRODUCTS_MAX:
            # Hit the per-screen cap but more exist — can't grow further.
            items.append(_nav_item(
                "__capped__", f"Showing first {_PRODUCTS_MAX}",
                {**more_base, "more": "1", "offset": str(shown)},
                metadata="Open a category or search to narrow down",
            ))
    return _resp("PRODUCTS", {"products": items})


def _products_screen(category: str, cfg: dict[str, Any], offset: int = 0) -> dict[str, Any]:
    return _product_list_screen(
        catalog.get_products_by_category(category), cfg, "Try another category",
        more_base={"category": category}, offset=offset,
    )


def _featured_screen(kind: str, cfg: dict[str, Any], offset: int = 0) -> dict[str, Any]:
    """Home shortcuts: 'new' = newest by created_at, 'best' = highest rating."""
    rows = catalog.fetch_active_rows()
    if kind == "new":
        rows = sorted(rows, key=lambda r: str(r.get("created_at") or ""), reverse=True)
    else:  # "best" / featured
        rows = sorted(rows, key=lambda r: float(r.get("rating_average") or -1), reverse=True)
    return _product_list_screen(rows, cfg, "Nothing here yet", more_base={"featured": kind}, offset=offset)


def _product_detail_screen(product_id: str, cfg: dict[str, Any]) -> dict[str, Any]:
    row = catalog.get_product(product_id)
    if not row:
        # Product vanished/inactive. Show a placeholder on the SAME screen instead
        # of bouncing to CATEGORIES (which would be a forbidden backward route);
        # the customer taps ← to go back.
        return _resp(
            "PRODUCT_DETAIL",
            {
                "product_id": "",
                "title": "Item unavailable",
                "price": "—",
                "rating": "",
                "description": "This item is no longer available.",
                "stock_label": "Out of stock",
                "in_stock": False,
                "qty_options": _qty_options(1),
                "image": flow_images.detail_b64(""),
            },
        )
    stock = row.get("stock_quantity")
    in_stock = stock is None or int(stock) > 0
    max_qty = _MAX_QTY if stock is None else int(stock)
    return _resp(
        "PRODUCT_DETAIL",
        {
            "product_id": str(row["id"]),
            "title": row.get("name") or "Item",
            "price": _price_detail(row, cfg),
            "rating": _rating_str(row) or "Not yet rated",
            "description": (row.get("description") or "").strip() or "—",
            "stock_label": _stock_label(row),
            "in_stock": in_stock,
            # Quantity dropdown options, capped to available stock (1..min(stock,_MAX_QTY)).
            "qty_options": _qty_options(max_qty),
            # Always a valid base64 JPEG (placeholder when the product has no image).
            "image": flow_images.detail_b64(row.get("image_url") or ""),
        },
    )


# Per-item edit controls add 2 rows per line (add-one + remove-one); reserve room
# for the Checkout + Clear rows so the NavigationList stays within its ~20-item cap.
_CART_EDIT_ITEMS = 9


def _cart_screen(sender: str, cfg: dict[str, Any], flash: str = "") -> dict[str, Any]:
    """Render the cart as an editable NavigationList. Each line gets two rows:
    tap the item to **add one**, tap the ➖ row to **remove one** (which removes
    the line at qty 1) — both re-render the cart (a self-update, allowed even
    though edits aren't in the routing_model). A **Clear cart** and **Checkout**
    row follow. To keep shopping the customer uses the device's native back arrow
    ("continue shopping" would be a backward route the routing_model forbids).
    All edits reuse ``cart.update_quantity`` / ``cart.remove_item``."""
    items = cart.get_items(sender)
    editable = items[:_CART_EDIT_ITEMS]
    thumbs = flow_images.thumbnails_b64([i.get("image_url") or "" for i in editable])
    cart_items: list[dict[str, Any]] = []
    subtotal = sum(i["unit_price_minor"] * i["quantity"] for i in items)

    for i in editable:
        pid = str(i.get("product_id") or "")
        name = i.get("name") or "Item"
        line_total = i["unit_price_minor"] * i["quantity"]
        # Row 1 — tap to add one of this item.
        row: dict[str, Any] = {
            "id": pid,
            "main-content": {
                "title": name,
                "description": f"Qty {i.get('quantity')}",
                "metadata": _clip(f"{m.format_money(line_total, cfg['currency'])} · tap ➕ to add one", 80),
            },
            "on-click-action": {"name": "data_exchange", "payload": {"item_action": "inc", "product_id": pid}},
        }
        thumb = thumbs.get(i.get("image_url") or "")
        if thumb:
            row["start"] = {"image": thumb, "alt-text": name}
        cart_items.append(row)
        # Row 2 — tap to remove one (removes the line entirely at qty 1).
        cart_items.append(_nav_item(
            f"dec_{pid}", f"➖ Remove one — {name}",
            {"item_action": "dec", "product_id": pid},
            metadata="Removes the item when qty reaches 0",
        ))

    if items:
        shipping = orders._compute_shipping(subtotal, cfg)
        total = subtotal + shipping
        summary = "\n".join(
            f"• {i['name']} ×{i['quantity']} — {m.format_money(i['unit_price_minor'] * i['quantity'], cfg['currency'])}"
            for i in items
        )
        if len(items) > _CART_EDIT_ITEMS:
            summary += f"\n(Showing controls for the first {_CART_EDIT_ITEMS} items.)"
        summary += f"\n\nSubtotal: {m.format_money(subtotal, cfg['currency'])}"
        if shipping:
            summary += f"\nShipping: {m.format_money(shipping, cfg['currency'])}"
        summary += f"\n*Total: {m.format_money(total, cfg['currency'])}*"
        cart_items.append(_nav_item("__checkout__", "✅ Checkout", {"cart_action": "checkout"}, metadata="Continue to delivery"))
        cart_items.append(_nav_item("__clear__", "🧹 Clear cart", {"cart_action": "clear"}, metadata="Remove all items"))
    else:
        summary = "Your cart is empty. Tap ← to keep shopping."
        # A NavigationList needs at least one row; this one self-refreshes.
        cart_items = [_nav_item("__empty__", "🛒 Cart is empty", {}, metadata="Tap ← to keep shopping")]

    if flash:
        summary = f"{flash}\n\n{summary}"
    return _resp("CART", {"summary": summary, "cart_items": cart_items})


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


def _address_screen(order: dict[str, Any], cfg: dict[str, Any], notes: list[str]) -> dict[str, Any]:
    currency = order.get("currency") or cfg["currency"]
    summary = f"Order {order['order_number']}\nTotal payable: {m.format_money(order['total_minor'], currency)}"
    if notes:
        summary = "\n".join(notes) + "\n\n" + summary
    return _resp("ADDRESS", {"order_number": order["order_number"], "summary": summary})


def _checkout_or_address(order: dict[str, Any], cfg: dict[str, Any], notes: list[str], flow_token: str) -> dict[str, Any]:
    """After an order is created, decide where to send the customer:

    * If we already have a saved delivery address for this sender, re-use it
      (apply it to the order server-side) and skip straight to SUCCESS — the
      customer never re-enters details for repeat orders.
    * Otherwise show the ADDRESS form to collect them.
    """
    saved = orders.last_shipping_for_sender(order.get("sender") or "")
    if saved:
        orders.set_customer_details(
            order["id"],
            saved.get("name") or "Customer",
            saved.get("phone") or (order.get("sender") or ""),
            {
                "address": saved.get("address", ""),
                "city": saved.get("city", ""),
                "state": saved.get("state", ""),
                "pincode": saved.get("pincode", ""),
            },
        )
        return _success_screen(order, order["order_number"], flow_token, cfg)
    return _address_screen(order, cfg, notes)


async def _checkout_screen(sender: str, cfg: dict[str, Any], flow_token: str) -> dict[str, Any]:
    """Create a draft order from the cart and move to delivery — same
    server-side stock-check/totals logic the chat checkout uses. Returning
    customers with a saved address skip the form (see ``_checkout_or_address``)."""
    items = cart.get_items(sender)
    if not items:
        return _cart_screen(sender, cfg, flash="Your cart is empty.")
    specs = [{"product_id": i["product_id"], "quantity": i["quantity"]} for i in items]
    order, notes = orders.create_order(sender, specs, cfg)
    if not order:
        return _cart_screen(sender, cfg, flash="None of your cart items are available anymore.")
    # Cart is NOT emptied here — it's cleared on confirmed payment
    # (orders.mark_order_paid), so an abandoned checkout keeps the cart intact.
    return _checkout_or_address(order, cfg, notes, flow_token)


def _buy_now_screen(sender: str, product_id: str, qty: int, cfg: dict[str, Any], flow_token: str) -> dict[str, Any]:
    """'Buy Now' — create a single-item order and go straight to delivery,
    bypassing the cart (the cart is left untouched). Returning customers with a
    saved address skip the form (see ``_checkout_or_address``)."""
    order, notes = orders.create_order(sender, [{"product_id": product_id, "quantity": qty}], cfg)
    if not order:
        return _product_detail_screen(product_id, cfg)
    return _checkout_or_address(order, cfg, notes, flow_token)


def _success_screen(order: dict[str, Any] | None, order_number: str, flow_token: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Terminal screen. ``extension_message_response.params`` is delivered to
    ``/webhook`` as an ``nfm_reply`` — the chat handler creates the payment link
    from there, exactly as it does for the address-Flow submission today. The
    other fields are display-only (shown on the confirmation screen)."""
    amount = "—"
    if order:
        amount = m.format_money(order.get("total_minor") or 0, order.get("currency") or cfg["currency"])
    return {
        "version": DATA_API_VERSION,
        "screen": "SUCCESS",
        "data": {
            "order_number": order_number or "—",
            "amount": amount,
            "delivery": "3–5 business days",
            "extension_message_response": {
                "params": {"flow_token": flow_token, "order_number": order_number}
            },
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
        return _resp("CATEGORIES", {"categories": []})

    # First screen fetch (only fires if the Flow is opened with data_exchange).
    if action == "INIT":
        return _resp("STORE_HOME", {})

    if action == "BACK":
        # Defensive: client normally handles back-nav itself.
        return _categories_screen(sender, cfg)

    # A "nav" shortcut from the CATEGORIES list — only forward routes
    # (CATEGORIES → CART / TRACK_ORDER). Backward shortcuts (home/categories)
    # aren't offered; the device back arrow handles going back.
    nav = (data.get("nav") or "").strip().lower()
    if nav:
        if nav == "cart":
            return _cart_screen(sender, cfg)
        if nav == "track":
            return _track_screen("", cfg)
        if nav == "more":  # "Show more categories" — next CATEGORIES page
            return _categories_screen(sender, cfg, _to_int(data.get("offset")))

    # action == "data_exchange" (or anything else) → route by screen + payload.
    if screen == "STORE_HOME":
        intent = (data.get("intent") or "").strip().lower()
        if intent == "cart":
            return _cart_screen(sender, cfg)
        if intent == "track":
            return _track_screen("", cfg)
        if intent in ("new", "best"):
            return _featured_screen(intent, cfg)
        return _categories_screen(sender, cfg)  # default: browse / categories

    if screen == "CATEGORIES":
        return _products_screen(data.get("category") or "", cfg)

    if screen == "PRODUCTS":
        if data.get("more"):  # "Show more" — next page of the same list (stays on PRODUCTS)
            offset = _to_int(data.get("offset"))
            featured = (data.get("featured") or "").strip().lower()
            if featured:
                return _featured_screen(featured, cfg, offset)
            return _products_screen(data.get("category") or "", cfg, offset)
        return _product_detail_screen(data.get("product_id") or "", cfg)

    if screen == "PRODUCT_DETAIL":
        product_id = data.get("product_id") or ""
        try:
            qty = max(1, int(data.get("qty") or 1))
        except (TypeError, ValueError):
            qty = 1
        if (data.get("action") or "").strip().lower() == "buy_now":
            return _buy_now_screen(sender, product_id, qty, cfg, flow_token)
        ok = cart.add_item(sender, product_id, qty)
        flash = "✓ Added to cart." if ok else "Sorry, that item isn't available."
        return _cart_screen(sender, cfg, flash=flash)

    if screen == "CART":
        cart_action = (data.get("cart_action") or "").strip().lower()
        if cart_action == "checkout":
            return await _checkout_screen(sender, cfg, flow_token)
        if cart_action == "clear":
            cart.clear_cart(sender)
            return _cart_screen(sender, cfg, flash="Cart cleared.")
        # Per-line edits — increase/decrease quantity or remove (self-update).
        item_action = (data.get("item_action") or "").strip().lower()
        product_id = (data.get("product_id") or "").strip()
        if item_action and product_id:
            if item_action == "inc":
                cart.update_quantity(sender, product_id, 1)
            elif item_action == "dec":
                cart.update_quantity(sender, product_id, -1)
            elif item_action == "remove":
                cart.remove_item(sender, product_id)
            return _cart_screen(sender, cfg)
        # Any other CART tap (empty placeholder) just refreshes the cart — there
        # are no backward routes out of CART (only ADDRESS/SUCCESS).
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
            return _success_screen(None, order_number, flow_token, cfg)
        name = (data.get("name") or "Customer").strip() or "Customer"
        phone = (data.get("phone") or "").strip() or sender
        address = {
            "address": (data.get("address") or "").strip(),
            "city": (data.get("city") or "").strip(),
            "state": (data.get("state") or "").strip(),
            "pincode": str(data.get("pincode") or "").strip(),
        }
        orders.set_customer_details(order["id"], name, phone, address)
        return _success_screen(order, order["order_number"], flow_token, cfg)

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
