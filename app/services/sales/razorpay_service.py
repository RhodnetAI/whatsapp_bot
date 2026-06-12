"""Razorpay integration for the Sales Bot — Payment Links + webhook signature
verification. Amounts are integer minor units (paise), matching our DB."""

import logging
from typing import Any

from app.core.config import settings

logger = logging.getLogger("whatsapp")

try:  # razorpay is an optional dependency until the seller configures payments
    import razorpay
except Exception:  # pragma: no cover
    razorpay = None  # type: ignore


def is_configured() -> bool:
    return bool(settings.razorpay_key_id and settings.razorpay_key_secret and razorpay is not None)


def _client():
    return razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))


def create_payment_link(order: dict[str, Any]) -> dict[str, Any]:
    """Create a Razorpay Payment Link for an order. Returns the raw Razorpay
    response (contains ``id`` and ``short_url``). Raises on failure."""
    client = _client()
    contact = order.get("customer_phone") or ""
    payload: dict[str, Any] = {
        "amount": int(order.get("total_minor") or 0),
        "currency": order.get("currency") or "INR",
        "accept_partial": False,
        "reference_id": order.get("order_number"),
        "description": f"Order {order.get('order_number')}",
        "customer": {
            "name": order.get("customer_name") or "Customer",
            "contact": contact,
        },
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "notes": {
            "order_number": str(order.get("order_number") or ""),
            "order_id": str(order.get("id") or ""),
            "sender": str(order.get("sender") or ""),
        },
    }
    if settings.public_base_url:
        payload["callback_url"] = f"{settings.public_base_url.rstrip('/')}/razorpay/callback"
        payload["callback_method"] = "get"

    return client.payment_link.create(payload)


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    """Verify the X-Razorpay-Signature header against the raw request body using
    the configured webhook secret."""
    if razorpay is None or not settings.razorpay_webhook_secret or not signature:
        return False
    try:
        client = razorpay.Client(
            auth=(settings.razorpay_key_id or "key", settings.razorpay_key_secret or "secret")
        )
        client.utility.verify_webhook_signature(
            body.decode("utf-8"), signature, settings.razorpay_webhook_secret
        )
        return True
    except Exception:
        logger.warning("Razorpay webhook signature verification failed")
        return False
