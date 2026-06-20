"""Best-effort sync of ``sales_products`` into the Meta Commerce Catalog via the
Graph API, so products can be shown as native WhatsApp product messages (SPM/MPM)
and added to the native WhatsApp cart.

Requires ``META_CATALOG_ID`` (the catalog connected to your WhatsApp Business
Account) and reuses ``META_ACCESS_TOKEN``. Each product is matched by its
``retailer_id``. Failures are captured per-product in ``sync_status``/``sync_error``
so the seller can still fall back to the DB-driven catalog. Sellers who prefer it
can instead add products directly in Commerce Manager — see the setup guide.
"""

import logging
from typing import Any

import requests

from app.core.config import settings
from app.db.supabase_client import supabase, supabase_admin
from app.services.sales import catalog

logger = logging.getLogger("whatsapp")

GRAPH = "https://graph.facebook.com/v25.0"


def _db():
    return supabase_admin if supabase_admin is not None else supabase


def is_configured() -> bool:
    return bool(settings.meta_catalog_id and settings.meta_access_token)


def _product_payload(row: dict[str, Any]) -> dict[str, Any]:
    currency = (row.get("currency") or "INR").upper()
    # The /{catalog_id}/products edge expects price as an integer in the
    # currency's minor units (e.g. paise/cents) with currency sent separately.
    price_minor = int(row.get("price_minor") or 0)
    in_stock = bool(row.get("is_active", True)) and (
        row.get("stock_quantity") is None or int(row.get("stock_quantity") or 0) > 0
    )
    url = settings.public_base_url.rstrip("/") if settings.public_base_url else "https://wa.me"
    payload: dict[str, Any] = {
        "retailer_id": row.get("retailer_id"),
        "name": row.get("name") or "Product",
        "description": row.get("description") or row.get("name") or "Product",
        "price": price_minor,
        "currency": currency,
        "image_url": row.get("image_url") or "",
        "url": url,
        "availability": "in stock" if in_stock else "out of stock",
        "condition": "new",
        "brand": "Store",
    }

    # Sale price — only set when an actual discount applies (Meta requires
    # sale_price < price).
    discount_percentage = float(row.get("discount_percentage") or 0)
    sale_price_minor = int(row.get("sale_price_minor") or 0)
    if discount_percentage > 0 and 0 < sale_price_minor < price_minor:
        payload["sale_price"] = sale_price_minor
        payload["sale_price_currency"] = currency

    # Extra catalog attributes — mapped to their Meta Commerce Catalog fields
    # where one exists. Fields with no Meta equivalent (delivery date, ratings,
    # discount percentage itself) are intentionally not sent.
    if row.get("google_product_category"):
        payload["google_product_category"] = row["google_product_category"]
    if row.get("size"):
        payload["size"] = row["size"]
    if row.get("color"):
        payload["color"] = row["color"]
    if row.get("model"):
        payload["mpn"] = row["model"]
    # Inventory shown in Commerce Manager — reflect the real remaining stock so it
    # drops after each sale. Prefer ``stock_quantity`` (the shopping-flow stock);
    # fall back to ``quantity_to_sell``.
    inventory = row.get("stock_quantity")
    if inventory is None:
        inventory = row.get("quantity_to_sell")
    if inventory is not None:
        payload["inventory"] = max(0, int(inventory))

    return payload


def _sync_one(row: dict[str, Any], access_token: str, catalog_id: str) -> tuple[bool, str, str | None]:
    """Create or update one product in the catalog. Returns (ok, error, meta_id)."""
    payload = _product_payload(row)
    if not payload["image_url"]:
        return False, "Product has no image_url (required by Meta Catalog).", None
    try:
        resp = requests.post(
            f"{GRAPH}/{catalog_id}/products",
            params={"access_token": access_token},
            json=payload,
            timeout=20,
        )
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400:
            err = data.get("error", {}).get("message") if isinstance(data, dict) else resp.text
            return False, str(err or f"HTTP {resp.status_code}"), None
        return True, "", str(data.get("id")) if isinstance(data, dict) else None
    except Exception as exc:
        logger.exception("Catalog sync failed for retailer_id=%s", row.get("retailer_id"))
        return False, str(exc), None


def _sync_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Push the given product rows to the Meta catalog and persist each one's
    ``sync_status``/``sync_error`` (and ``meta_catalog_id`` when returned).
    Per-product failures are recorded and don't stop the others. Returns counts."""
    synced = 0
    failed = 0
    for row in rows:
        ok, err, meta_id = _sync_one(row, settings.meta_access_token, settings.meta_catalog_id)
        update: dict[str, Any] = {
            "sync_status": "synced" if ok else "failed",
            "sync_error": "" if ok else err[:500],
        }
        if meta_id:
            update["meta_catalog_id"] = meta_id
        _db().table(catalog.TABLE).update(update).eq("id", row["id"]).execute()
        synced += 1 if ok else 0
        failed += 0 if ok else 1
    return {"synced": synced, "failed": failed}


def _needs_sync(row: dict[str, Any]) -> bool:
    """A product needs (re)syncing when it was never successfully synced or has
    changed since: ``catalog.update_product`` flips a ``synced`` product back to
    ``pending`` on edit, and new products start as ``pending``. ``failed`` rows are
    retried. Already-``synced`` (unchanged) rows are skipped."""
    return (row.get("sync_status") or "pending") in ("pending", "failed")


def sync_products(product_ids: list[str]) -> dict[str, Any]:
    """Sync specific products (by id) to the Meta catalog — used after a sale to
    push the reduced stock so Commerce Manager reflects it. Best-effort: no-op when
    the catalog isn't configured."""
    if not is_configured() or not product_ids:
        return {"synced": 0, "failed": 0}
    rows = [row for row in (catalog.get_row(pid) for pid in product_ids) if row]
    return _sync_rows(rows)


def sync_changed() -> dict[str, Any]:
    """Sync only the products that need it — never-synced or edited-since-last-sync
    (``sync_status`` in ``pending``/``failed``). Already-synced products are
    skipped, so repeated clicks are cheap and don't re-push unchanged items. This
    is the default for the admin "Sync" button. Returns a summary dict including
    ``skipped`` (count of up-to-date products that were not re-sent)."""
    if not is_configured():
        return {"synced": 0, "failed": 0, "skipped": 0, "error": "Meta Catalog not configured", "items": []}
    all_rows = catalog.fetch_active_rows()
    to_sync = [row for row in all_rows if _needs_sync(row)]
    result: dict[str, Any] = _sync_rows(to_sync)
    result["skipped"] = len(all_rows) - len(to_sync)
    result["items"] = [i.model_dump() for i in catalog.list_products()]
    return result


def sync_all() -> dict[str, Any]:
    """Sync **every** active product (full re-sync), regardless of ``sync_status``.
    Use this to force a complete re-push; the default button uses
    :func:`sync_changed`. Returns a summary dict."""
    if not is_configured():
        return {"synced": 0, "failed": 0, "skipped": 0, "error": "Meta Catalog not configured", "items": []}
    rows = catalog.fetch_active_rows()
    result: dict[str, Any] = _sync_rows(rows)
    result["skipped"] = 0
    result["items"] = [i.model_dump() for i in catalog.list_products()]
    return result
