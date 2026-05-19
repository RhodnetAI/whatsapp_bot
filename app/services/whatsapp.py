import requests

from app.core.config import settings


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
