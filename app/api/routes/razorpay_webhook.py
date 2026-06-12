"""Razorpay webhook + payment callback for the Sales Bot.

Security: every webhook is verified against X-Razorpay-Signature (HMAC-SHA256 of
the raw body with the configured webhook secret). Idempotency: each event is
recorded in sales_payments keyed on the unique X-Razorpay-Event-Id, so a replayed
or duplicate delivery is a no-op (the order is marked paid / stock decremented
only on the first successful insert).
"""

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.db.supabase_client import first_row, supabase, supabase_admin
from app.services.sales import handler, orders, razorpay_service

logger = logging.getLogger("whatsapp")

router = APIRouter(tags=["razorpay"])

PAYMENTS = "sales_payments"


def _db():
    return supabase_admin if supabase_admin is not None else supabase


def _extract_link_entity(payload: dict[str, Any]) -> dict[str, Any]:
    return (((payload.get("payload") or {}).get("payment_link") or {}).get("entity")) or {}


def _extract_payment_entity(payload: dict[str, Any]) -> dict[str, Any]:
    return (((payload.get("payload") or {}).get("payment") or {}).get("entity")) or {}


@router.post("/razorpay/webhook")
async def razorpay_webhook(request: Request) -> JSONResponse:
    raw = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    event_id = request.headers.get("X-Razorpay-Event-Id", "")

    if not razorpay_service.verify_webhook_signature(raw, signature):
        logger.warning("Razorpay webhook rejected: bad signature")
        return JSONResponse({"status": "invalid signature"}, status_code=400)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"status": "bad payload"}, status_code=400)

    event = payload.get("event") or ""
    logger.info("Razorpay webhook event=%s id=%s", event, event_id)

    link_entity = _extract_link_entity(payload)
    payment_entity = _extract_payment_entity(payload)

    # Resolve the order via reference_id (= our order_number) on the payment link,
    # falling back to the payment notes.
    order_number = link_entity.get("reference_id") or (payment_entity.get("notes") or {}).get("order_number")
    order = orders.get_order_by_number(order_number) if order_number else None

    # ── Idempotency: record this event; a duplicate event_id conflicts and stops.
    if not event_id:
        event_id = f"{event}:{link_entity.get('id') or payment_entity.get('id') or 'unknown'}"
    already_processed = first_row(
        _db().table(PAYMENTS).select("id").eq("razorpay_event_id", event_id).execute()
    )
    if already_processed:
        return JSONResponse({"status": "duplicate ignored"})

    amount = int(payment_entity.get("amount") or link_entity.get("amount_paid") or 0)
    try:
        _db().table(PAYMENTS).insert(
            {
                "order_id": order.get("id") if order else None,
                "razorpay_event_id": event_id,
                "razorpay_payment_id": payment_entity.get("id"),
                "razorpay_order_id": payment_entity.get("order_id"),
                "razorpay_payment_link_id": link_entity.get("id"),
                "event_type": event,
                "amount_minor": amount,
                "currency": payment_entity.get("currency") or link_entity.get("currency") or "INR",
                "status": payment_entity.get("status") or link_entity.get("status") or "",
                "raw_payload": payload,
            }
        ).execute()
    except Exception:
        # Unique-constraint conflict on a racing duplicate — safe to ignore.
        logger.info("Razorpay event %s already recorded (race); ignoring", event_id)
        return JSONResponse({"status": "duplicate ignored"})

    paid_events = {"payment_link.paid", "payment.captured", "order.paid"}
    if event in paid_events and order:
        updated = orders.mark_order_paid(order["id"])
        if updated:
            await handler.notify_order_paid(updated)
    elif event == "payment.failed" and order:
        orders.mark_order_failed(order["id"])

    return JSONResponse({"status": "ok"})


@router.get("/razorpay/callback")
async def razorpay_callback() -> HTMLResponse:
    """Where Razorpay redirects the buyer after payment. The order is confirmed by
    the webhook (source of truth); this is just a friendly browser landing page."""
    return HTMLResponse(
        "<html><body style='font-family:sans-serif;text-align:center;padding:48px'>"
        "<h2>Thank you! 🎉</h2>"
        "<p>Your payment is being confirmed. You can return to WhatsApp — "
        "we'll message you the moment it's done.</p></body></html>"
    )
