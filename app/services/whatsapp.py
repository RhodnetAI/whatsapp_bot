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


def send_whatsapp_typing_indicator(sender: str) -> requests.Response:
    """
    Send typing indicator to WhatsApp to show the user that the service agent is processing.
    The typing indicator automatically dismisses after 25 seconds or when a response is sent.
    Reference: https://developers.facebook.com/documentation/business-messaging/whatsapp/typing-indicators
    """
    url = f"https://graph.facebook.com/v25.0/{settings.phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {settings.meta_access_token}",
        "Content-Type": "application/json",
    }
    wa_id = sender[1:] if sender.startswith("+") else sender
    payload = {
        "messaging_product": "whatsapp",
        "to": wa_id,
        "type": "typing",
    }
    logger.debug("Sending typing indicator with payload: %s", payload)
    response = requests.post(url, headers=headers, json=payload, timeout=20)
    logger.debug("Typing indicator API response: status=%s, body=%s", response.status_code, response.text)
    return response
