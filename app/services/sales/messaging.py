"""WhatsApp Cloud API message builders for the Sales Bot.

Each builder returns a message object WITHOUT ``messaging_product``/``to`` — those
are injected by ``app.services.whatsapp.send_whatsapp_message``. The builders also
enforce WhatsApp's text-length limits so payloads are never rejected.
"""

from __future__ import annotations

from typing import Any

# ── Interactive reply IDs (referenced by handler.py) ─────────────────────────
MENU_BROWSE = "menu_browse"
MENU_CART = "menu_cart"
MENU_TRACK = "menu_track"
MENU_HUMAN = "menu_human"
CART_CHECKOUT = "cart_checkout"
CART_CLEAR = "cart_clear"
CAT_PREFIX = "cat:"        # cat:<category>
PROD_PREFIX = "prod:"      # prod:<product_id>
ADD_PREFIX = "add:"        # add:<product_id>
QTY_PREFIX = "qty:"        # qty:<product_id>:<n>

# WhatsApp limits
_LIST_ROW_TITLE = 24
_LIST_ROW_DESC = 72
_BTN_TITLE = 20
_SECTION_TITLE = 24
_HEADER_TEXT = 60
_BODY_TEXT = 1024


def _clip(text: Any, limit: int) -> str:
    s = str(text or "").strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def format_money(minor: int, currency: str = "INR") -> str:
    """Format integer minor units (paise) as a human price string."""
    minor = int(minor or 0)
    symbol = "₹" if (currency or "INR").upper() == "INR" else f"{currency} "
    if minor % 100 == 0:
        return f"{symbol}{minor // 100:,}"
    return f"{symbol}{minor / 100:,.2f}"


# ── Primitive builders ───────────────────────────────────────────────────────
def text(body: str) -> dict:
    return {"type": "text", "text": {"body": _clip(body, 4096)}}


def buttons(
    body: str,
    options: list[tuple[str, str]],
    header_text: str | None = None,
    header_image: str | None = None,
) -> dict:
    """Reply-button message. ``options`` is a list of (id, title); max 3."""
    interactive: dict[str, Any] = {
        "type": "button",
        "body": {"text": _clip(body, _BODY_TEXT)},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": bid, "title": _clip(title, _BTN_TITLE)}}
                for bid, title in options[:3]
            ]
        },
    }
    if header_image:
        interactive["header"] = {"type": "image", "image": {"link": header_image}}
    elif header_text:
        interactive["header"] = {"type": "text", "text": _clip(header_text, _HEADER_TEXT)}
    return {"type": "interactive", "interactive": interactive}


def list_message(
    body: str,
    button_text: str,
    sections: list[dict],
    header_text: str | None = None,
) -> dict:
    """Interactive list. NOTE: WhatsApp caps total rows across all sections at 10
    — the caller is responsible for paginating before calling this."""
    interactive: dict[str, Any] = {
        "type": "list",
        "body": {"text": _clip(body, _BODY_TEXT)},
        "action": {"button": _clip(button_text, _BTN_TITLE), "sections": sections},
    }
    if header_text:
        interactive["header"] = {"type": "text", "text": _clip(header_text, _HEADER_TEXT)}
    return {"type": "interactive", "interactive": interactive}


def list_row(row_id: str, title: str, description: str = "") -> dict:
    row: dict[str, Any] = {"id": row_id, "title": _clip(title, _LIST_ROW_TITLE)}
    desc = _clip(description, _LIST_ROW_DESC)
    if desc:
        row["description"] = desc
    return row


def section(title: str, rows: list[dict]) -> dict:
    return {"title": _clip(title, _SECTION_TITLE), "rows": rows}


def cta_url(body: str, display_text: str, url: str, header_text: str | None = None) -> dict:
    interactive: dict[str, Any] = {
        "type": "cta_url",
        "body": {"text": _clip(body, _BODY_TEXT)},
        "action": {
            "name": "cta_url",
            "parameters": {"display_text": _clip(display_text, _BTN_TITLE), "url": url},
        },
    }
    if header_text:
        interactive["header"] = {"type": "text", "text": _clip(header_text, _HEADER_TEXT)}
    return {"type": "interactive", "interactive": interactive}


def image(image_url: str, caption: str | None = None) -> dict:
    img: dict[str, Any] = {"link": image_url}
    if caption:
        img["caption"] = _clip(caption, _BODY_TEXT)
    return {"type": "image", "image": img}


# ── Catalog (Meta Commerce Catalog) builders ─────────────────────────────────
def product_list(catalog_id: str, header_text: str, body: str, sections: list[dict]) -> dict:
    """Multi-Product Message (MPM). ``sections`` is a list of
    {"title": str, "product_items": [{"product_retailer_id": str}, ...]}."""
    return {
        "type": "interactive",
        "interactive": {
            "type": "product_list",
            "header": {"type": "text", "text": _clip(header_text, _HEADER_TEXT)},
            "body": {"text": _clip(body, _BODY_TEXT)},
            "action": {"catalog_id": catalog_id, "sections": sections},
        },
    }


def single_product(catalog_id: str, retailer_id: str, body: str | None = None) -> dict:
    interactive: dict[str, Any] = {
        "type": "product",
        "action": {"catalog_id": catalog_id, "product_retailer_id": retailer_id},
    }
    if body:
        interactive["body"] = {"text": _clip(body, _BODY_TEXT)}
    return {"type": "interactive", "interactive": interactive}


# ── Flow (checkout address form) builder ─────────────────────────────────────
def flow(
    flow_id: str,
    flow_token: str,
    body: str,
    cta_text: str,
    header_text: str | None = None,
    screen: str = "DETAILS",
) -> dict:
    interactive: dict[str, Any] = {
        "type": "flow",
        "body": {"text": _clip(body, _BODY_TEXT)},
        "action": {
            "name": "flow",
            "parameters": {
                "flow_message_version": "3",
                "flow_token": flow_token,
                "flow_id": flow_id,
                "flow_cta": _clip(cta_text, _BTN_TITLE),
                "flow_action": "navigate",
                "flow_action_payload": {"screen": screen},
            },
        },
    }
    if header_text:
        interactive["header"] = {"type": "text", "text": _clip(header_text, _HEADER_TEXT)}
    return {"type": "interactive", "interactive": interactive}
