"""Expo push notifications for the React Native receiver app.

Device tokens are stored in the ``push_subscriptions`` table (see migration
037). When a new inbound WhatsApp message lands, the webhook calls
``send_push_to_all`` which delivers an Expo push to every registered device —
so the admin is notified even when the app is closed. Tokens that Expo reports
as unregistered (DeviceNotRegistered) are pruned.
"""

import logging
from typing import Any

import requests

from app.db.supabase_client import supabase_admin, supabase

logger = logging.getLogger("whatsapp")

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def _client() -> Any:
    return supabase_admin if supabase_admin is not None else supabase


def register_token(token: str, platform: str | None) -> None:
    """Insert or refresh a device token (idempotent on the unique token column)."""
    _client().table("push_subscriptions").upsert(
        {"token": token, "platform": platform},
        on_conflict="token",
    ).execute()


def unregister_token(token: str) -> None:
    _client().table("push_subscriptions").delete().eq("token", token).execute()


def _list_tokens() -> list[str]:
    try:
        res = _client().table("push_subscriptions").select("token").execute()
        return [
            row["token"]
            for row in (res.data or [])
            if isinstance(row, dict) and isinstance(row.get("token"), str)
        ]
    except Exception:
        logger.exception("Failed to list push tokens")
        return []


def _prune_tokens(tokens: list[str]) -> None:
    for token in tokens:
        try:
            unregister_token(token)
        except Exception:
            logger.exception("Failed to prune push token")


def send_push_to_all(title: str, body: str, sender: str) -> None:
    """Send a push notification to every registered device. Best-effort and
    synchronous — callers should run this off the request path (e.g. via
    ``asyncio.to_thread``)."""
    tokens = _list_tokens()
    if not tokens:
        return

    messages = [
        {
            "to": token,
            "title": title,
            "body": body,
            "sound": "default",
            "data": {"sender": sender},
            "channelId": "default",
        }
        for token in tokens
    ]

    try:
        resp = requests.post(
            EXPO_PUSH_URL,
            json=messages,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=15,
        )
    except Exception:
        logger.exception("Expo push request failed")
        return

    if resp.status_code >= 400:
        logger.error("Expo push error %s: %s", resp.status_code, resp.text)
        return

    # Expo returns a per-message ticket array; prune tokens it rejects.
    try:
        tickets = resp.json().get("data", [])
    except Exception:
        return

    dead: list[str] = []
    for token, ticket in zip(tokens, tickets):
        if not isinstance(ticket, dict):
            continue
        if ticket.get("status") == "error":
            details = ticket.get("details") or {}
            if details.get("error") == "DeviceNotRegistered":
                dead.append(token)
    if dead:
        _prune_tokens(dead)
