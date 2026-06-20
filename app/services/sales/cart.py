"""Sales Bot fallback cart — a DB-backed cart used when no Meta Commerce Catalog
is connected (so there is no native WhatsApp cart). One active cart per sender."""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from app.db.supabase_client import first_row, supabase, supabase_admin
from app.services.sales import catalog

logger = logging.getLogger("whatsapp")

CARTS = "sales_carts"
CART_ITEMS = "sales_cart_items"


def _db():
    return supabase_admin if supabase_admin is not None else supabase


def _get_or_create_active_cart(sender: str) -> dict[str, Any]:
    existing = first_row(
        _db().table(CARTS).select("*").eq("sender", sender).eq("status", "active").execute()
    )
    if existing:
        return existing
    now = datetime.now(timezone.utc).isoformat()
    row = {"id": str(uuid.uuid4()), "sender": sender, "status": "active", "created_at": now, "updated_at": now}
    # Upsert on sender; if a non-active cart already exists for this sender we
    # reactivate it (one cart row per sender thanks to the unique constraint).
    _db().table(CARTS).upsert(
        {"id": row["id"], "sender": sender, "status": "active", "updated_at": now},
        on_conflict="sender",
    ).execute()
    return first_row(
        _db().table(CARTS).select("*").eq("sender", sender).execute()
    ) or row


def add_item(sender: str, product_id: str, quantity: int) -> bool:
    """Add (or increment) a product in the sender's active cart. Returns False if
    the product is missing/inactive."""
    product = catalog.get_row(product_id)
    if not product or not product.get("is_active", True):
        return False
    quantity = max(1, int(quantity))
    cart = _get_or_create_active_cart(sender)

    existing = first_row(
        _db()
        .table(CART_ITEMS)
        .select("*")
        .eq("cart_id", cart["id"])
        .eq("product_id", product_id)
        .execute()
    )
    if existing:
        _db().table(CART_ITEMS).update(
            {"quantity": int(existing.get("quantity") or 0) + quantity}
        ).eq("id", existing["id"]).execute()
    else:
        _db().table(CART_ITEMS).insert(
            {
                "id": str(uuid.uuid4()),
                "cart_id": cart["id"],
                "product_id": product_id,
                "quantity": quantity,
                # Discounted unit price when a discount applies (honours sale price).
                "unit_price_minor": catalog.effective_unit_price_minor(product),
            }
        ).execute()
    return True


def get_items(sender: str) -> list[dict[str, Any]]:
    """Return the active cart's items joined with current product data."""
    cart = first_row(
        _db().table(CARTS).select("id").eq("sender", sender).eq("status", "active").execute()
    )
    if not cart:
        return []
    items_res = _db().table(CART_ITEMS).select("*").eq("cart_id", cart["id"]).execute()
    items = [r for r in (getattr(items_res, "data", None) or []) if isinstance(r, dict)]
    if not items:
        return []
    products = {}
    ids = [i["product_id"] for i in items if i.get("product_id")]
    if ids:
        prod_res = _db().table(catalog.TABLE).select("*").in_("id", ids).execute()
        for p in getattr(prod_res, "data", None) or []:
            if isinstance(p, dict):
                products[p["id"]] = p

    out: list[dict[str, Any]] = []
    for item in items:
        product = products.get(item.get("product_id")) or {}
        out.append(
            {
                "product_id": item.get("product_id"),
                "retailer_id": product.get("retailer_id") or "",
                "name": product.get("name") or "Item",
                "quantity": int(item.get("quantity") or 1),
                # Re-derive from the live product so discount changes are reflected;
                # fall back to the stored price if the product row is gone.
                "unit_price_minor": (
                    catalog.effective_unit_price_minor(product)
                    if product else int(item.get("unit_price_minor") or 0)
                ),
                "image_url": product.get("image_url") or "",
                "stock_quantity": product.get("stock_quantity"),
            }
        )
    return out


def update_quantity(sender: str, product_id: str, delta: int) -> int:
    """Increment/decrement a cart line by ``delta``. Removes the line if the
    resulting quantity is 0 or less. Returns the resulting quantity (0 when the
    line was removed or did not exist)."""
    cart = first_row(
        _db().table(CARTS).select("id").eq("sender", sender).eq("status", "active").execute()
    )
    if not cart:
        return 0
    existing = first_row(
        _db()
        .table(CART_ITEMS)
        .select("*")
        .eq("cart_id", cart["id"])
        .eq("product_id", product_id)
        .execute()
    )
    if not existing:
        return 0
    new_qty = int(existing.get("quantity") or 0) + int(delta)
    if new_qty <= 0:
        _db().table(CART_ITEMS).delete().eq("id", existing["id"]).execute()
        return 0
    _db().table(CART_ITEMS).update({"quantity": new_qty}).eq("id", existing["id"]).execute()
    return new_qty


def remove_item(sender: str, product_id: str) -> None:
    """Remove a single product line from the sender's active cart."""
    cart = first_row(
        _db().table(CARTS).select("id").eq("sender", sender).eq("status", "active").execute()
    )
    if not cart:
        return
    _db().table(CART_ITEMS).delete().eq("cart_id", cart["id"]).eq("product_id", product_id).execute()


def clear_cart(sender: str) -> None:
    cart = first_row(
        _db().table(CARTS).select("id").eq("sender", sender).eq("status", "active").execute()
    )
    if not cart:
        return
    _db().table(CART_ITEMS).delete().eq("cart_id", cart["id"]).execute()


def mark_checked_out(sender: str) -> None:
    cart = first_row(
        _db().table(CARTS).select("id").eq("sender", sender).eq("status", "active").execute()
    )
    if not cart:
        return
    _db().table(CART_ITEMS).delete().eq("cart_id", cart["id"]).execute()
    _db().table(CARTS).update({"status": "checked_out"}).eq("id", cart["id"]).execute()
