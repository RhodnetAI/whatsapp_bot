import logging
import requests

from app.core.config import settings

logger = logging.getLogger("whatsapp")


def send_whatsapp_text(sender: str, message: str) -> requests.Response:
    url = f"https://graph.facebook.com/v25.0/{settings.phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {settings.meta_access_token}",
        "Content-Type": "application/json",
    }
    wa_id = sender[1:] if sender.startswith("+") else sender
    payload = {
        "messaging_product": "whatsapp",
        "to": wa_id,
        "text": {"body": message},
    }
    return requests.post(url, headers=headers, json=payload, timeout=20)


def send_whatsapp_typing_indicator(message_id: str) -> requests.Response:
    """
    Send a WhatsApp typing indicator for a received message.
    The typing indicator uses the received webhook message_id and is dismissed once a response is sent, or after 25 seconds.
    Reference: https://developers.facebook.com/documentation/business-messaging/whatsapp/typing-indicators
    """
    url = f"https://graph.facebook.com/v25.0/{settings.phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {settings.meta_access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
        "typing_indicator": {"type": "typing"},
    }
    logger.debug("Sending typing indicator with payload: %s", payload)
    response = requests.post(url, headers=headers, json=payload, timeout=20)
    logger.debug("Typing indicator API response: status=%s, body=%s", response.status_code, response.text)
    return response
