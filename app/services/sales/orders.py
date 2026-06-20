"""Sales Bot orders — build orders from native-cart `order` items or the fallback
DB cart, always recomputing prices/totals server-side from ``sales_products``
(client/order-message prices are never trusted), and update payment status."""

import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db.supabase_client import first_row, supabase, supabase_admin
from app.services.sales import cart, catalog

logger = logging.getLogger("whatsapp")

ORDERS = "sales_orders"
ORDER_ITEMS = "sales_order_items"


def _db():
    return supabase_admin if supabase_admin is not None else supabase


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_order_number() -> str:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"ORD-{today}-{secrets.token_hex(3).upper()}"


def _compute_shipping(subtotal_minor: int, cfg: dict[str, Any]) -> int:
    if subtotal_minor <= 0:
        return 0
    flat = int(cfg.get("flat_shipping_minor") or 0)
    threshold = cfg.get("free_shipping_threshold_minor")
    if threshold is not None and subtotal_minor >= int(threshold):
        return 0
    return flat


def _build_lines_from_specs(specs: list[dict[str, Any]], cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """specs: list of {retailer_id?, product_id?, quantity}. Returns (lines, notes)
    where each line snapshots current price; out-of-stock items are dropped with a
    note and in-stock quantities are capped to available stock."""
    notes: list[str] = []
    lines: list[dict[str, Any]] = []

    retailer_ids = [s["retailer_id"] for s in specs if s.get("retailer_id")]
    by_retailer = catalog.fetch_rows_by_retailer_ids(retailer_ids) if retailer_ids else {}

    for spec in specs:
        product = None
        if spec.get("product_id"):
            product = catalog.get_row(spec["product_id"])
        elif spec.get("retailer_id"):
            product = by_retailer.get(spec["retailer_id"])
        if not product or not product.get("is_active", True):
            continue

        requested = max(1, int(spec.get("quantity") or 1))
        stock = product.get("stock_quantity")
        if stock is not None:
            if int(stock) <= 0:
                notes.append(f"• {product.get('name') or 'Item'} is out of stock and was removed.")
                continue
            if requested > int(stock):
                notes.append(
                    f"• Only {int(stock)} of {product.get('name') or 'Item'} left — quantity adjusted."
                )
                requested = int(stock)

        unit = catalog.effective_unit_price_minor(product)
        lines.append(
            {
                "product_id": product.get("id"),
                "retailer_id": product.get("retailer_id") or "",
                "name": product.get("name") or "Item",
                "quantity": requested,
                "unit_price_minor": unit,
                "line_total_minor": unit * requested,
            }
        )
    return lines, notes


def cleanup_stale_drafts(hours: int = 24) -> int:
    """Delete abandoned ``draft`` orders (never reached the address step) older
    than ``hours``, plus their line items, so the orders table doesn't fill with
    junk. Conservative on purpose: ``pending_payment`` orders are left alone (a
    payment link may still be paid). Returns how many orders were removed."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        res = (
            _db().table(ORDERS).select("id").eq("status", "draft").lt("created_at", cutoff).execute()
        )
        ids = [r["id"] for r in (getattr(res, "data", None) or []) if isinstance(r, dict) and r.get("id")]
        if not ids:
            return 0
        _db().table(ORDER_ITEMS).delete().in_("order_id", ids).execute()
        _db().table(ORDERS).delete().in_("id", ids).execute()
        return len(ids)
    except Exception:
        logger.exception("Failed to clean up stale draft orders")
        return 0


def create_order(sender: str, specs: list[dict[str, Any]], cfg: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """Create a draft order from item specs. Returns (order, notes); order is None
    if nothing orderable remained."""
    # Opportunistic housekeeping — prune old abandoned drafts on each checkout
    # (cheap, low volume) so no separate cron job is needed.
    cleanup_stale_drafts()
    lines, notes = _build_lines_from_specs(specs, cfg)
    if not lines:
        return None, notes

    subtotal = sum(l["line_total_minor"] for l in lines)
    shipping = _compute_shipping(subtotal, cfg)
    total = subtotal + shipping
    currency = cfg.get("currency") or "INR"

    order_id = str(uuid.uuid4())
    order_number = generate_order_number()
    now = _now()
    order_row = {
        "id": order_id,
        "order_number": order_number,
        "sender": sender,
        "status": "draft",
        "subtotal_minor": subtotal,
        "shipping_minor": shipping,
        "total_minor": total,
        "currency": currency,
        "customer_phone": sender,
        "payment_status": "created",
        "created_at": now,
        "updated_at": now,
    }
    _db().table(ORDERS).insert(order_row).execute()
    _db().table(ORDER_ITEMS).insert(
        [{"id": str(uuid.uuid4()), "order_id": order_id, **{k: l[k] for k in
            ("product_id", "retailer_id", "name", "quantity", "unit_price_minor", "line_total_minor")}}
         for l in lines]
    ).execute()

    order_row["items"] = lines
    return order_row, notes


def set_customer_details(order_id: str, name: str, phone: str, address: dict[str, Any]) -> None:
    _db().table(ORDERS).update(
        {
            "customer_name": name or "",
            "customer_phone": phone or "",
            "shipping_address": address or {},
            "status": "pending_payment",
        }
    ).eq("id", order_id).execute()


def attach_payment_link(order_id: str, link_id: str, link_url: str, razorpay_order_id: str | None) -> None:
    _db().table(ORDERS).update(
        {
            "razorpay_payment_link_id": link_id,
            "razorpay_payment_link_url": link_url,
            "razorpay_order_id": razorpay_order_id,
            "payment_status": "link_sent",
        }
    ).eq("id", order_id).execute()


def get_order(order_id: str) -> dict[str, Any] | None:
    return first_row(_db().table(ORDERS).select("*").eq("id", order_id).execute())


def get_order_by_number(order_number: str) -> dict[str, Any] | None:
    return first_row(
        _db().table(ORDERS).select("*").eq("order_number", order_number.strip().upper()).execute()
    )


def get_order_items(order_id: str) -> list[dict[str, Any]]:
    res = _db().table(ORDER_ITEMS).select("*").eq("order_id", order_id).execute()
    return [r for r in (getattr(res, "data", None) or []) if isinstance(r, dict)]


def latest_order_for_sender(sender: str) -> dict[str, Any] | None:
    res = (
        _db().table(ORDERS).select("*").eq("sender", sender).order("created_at", desc=True).limit(1).execute()
    )
    return first_row(res)


def last_shipping_for_sender(sender: str) -> dict[str, Any] | None:
    """Most recent saved delivery details for this sender, so the store Flow can
    pre-fill the ADDRESS form instead of asking again. Returns a flat dict with
    name/phone/address/city/state/pincode, or None if nothing on file."""
    res = (
        _db()
        .table(ORDERS)
        .select("customer_name, customer_phone, shipping_address")
        .eq("sender", sender)
        .not_.is_("shipping_address", "null")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    row = first_row(res)
    if not row:
        return None
    addr = row.get("shipping_address")
    if not isinstance(addr, dict) or not addr:
        return None
    return {
        "name": row.get("customer_name") or "",
        "phone": row.get("customer_phone") or "",
        "address": addr.get("address") or "",
        "city": addr.get("city") or "",
        "state": addr.get("state") or "",
        "pincode": str(addr.get("pincode") or ""),
    }


def mark_order_paid(order_id: str) -> dict[str, Any] | None:
    """Idempotency is enforced upstream by the unique sales_payments.event_id.
    Marks the order paid and decrements stock for its line items."""
    order = get_order(order_id)
    if not order:
        return None
    if order.get("status") == "paid":
        return order
    _db().table(ORDERS).update({"status": "paid", "payment_status": "paid"}).eq("id", order_id).execute()

    # Cart is emptied only now (on confirmed payment), not at checkout — so an
    # abandoned/unpaid checkout leaves the cart intact for the customer.
    try:
        cart.clear_cart(order.get("sender") or "")
    except Exception:
        logger.exception("Could not clear cart for sender after payment")

    for item in get_order_items(order_id):
        product_id = item.get("product_id")
        if not product_id:
            continue
        product = catalog.get_row(product_id)
        if not product:
            continue
        stock = product.get("stock_quantity")
        if stock is not None:
            new_stock = max(0, int(stock) - int(item.get("quantity") or 0))
            _db().table(catalog.TABLE).update({"stock_quantity": new_stock}).eq("id", product_id).execute()
    return get_order(order_id)


def mark_order_failed(order_id: str) -> None:
    _db().table(ORDERS).update({"payment_status": "failed"}).eq("id", order_id).execute()


def list_orders(limit: int = 200, include_drafts: bool = False) -> list[dict[str, Any]]:
    """Admin order list. Drafts (unsubmitted carts) are hidden by default so the
    dashboard shows real orders; pass ``include_drafts=True`` to see them."""
    query = _db().table(ORDERS).select("*")
    if not include_drafts:
        query = query.neq("status", "draft")
    res = query.order("created_at", desc=True).limit(limit).execute()
    return [r for r in (getattr(res, "data", None) or []) if isinstance(r, dict)]
