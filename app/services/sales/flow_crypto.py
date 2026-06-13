"""WhatsApp Flow data-exchange encryption for the Sales Bot "Open Store" Flow.

Every request to the dynamic-Flow data-exchange endpoint
(``POST /flows/store/data-exchange``) is end-to-end encrypted at the payload
level, *on top of* TLS. Meta encrypts the request with a one-time AES-128 key,
which is itself RSA-OAEP-encrypted with the public key we registered via
``POST /{phone-number-id}/whatsapp_business_encryption`` (see
``backend/scripts/generate_flow_keys.py``).

This module hand-rolls the scheme with ``cryptography`` (no extra dependency —
it is already pulled in transitively). The mechanics, per Meta's spec:

  * Incoming: RSA-OAEP(SHA-256) decrypt ``encrypted_aes_key`` → AES key, then
    AES-128-GCM decrypt ``encrypted_flow_data`` using ``initial_vector`` (the
    GCM tag is the trailing 16 bytes of the ciphertext).
  * Outgoing: AES-128-GCM encrypt the response JSON with the *same* AES key but
    a **flipped** IV (every bit inverted), then base64-encode. The HTTP body is
    that base64 string (``text/plain``), not a JSON envelope.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings

logger = logging.getLogger("whatsapp")

_TAG_LENGTH = 16  # AES-GCM authentication tag length in bytes


class FlowEncryptionError(Exception):
    """Raised when a request cannot be decrypted. The endpoint maps this to a
    421 so WhatsApp re-fetches the public key (per Meta's error handling)."""


def is_configured() -> bool:
    """True when a private key is available to decrypt Flow requests."""
    return bool(settings.whatsapp_flow_private_key.strip())


def _load_private_key():
    pem = settings.whatsapp_flow_private_key.strip()
    if not pem:
        raise FlowEncryptionError("WHATSAPP_FLOW_PRIVATE_KEY is not configured")
    passphrase = settings.whatsapp_flow_private_key_passphrase or None
    return serialization.load_pem_private_key(
        pem.encode("utf-8"),
        password=passphrase.encode("utf-8") if passphrase else None,
    )


def decrypt_request(body: dict[str, Any]) -> tuple[dict[str, Any], bytes, bytes]:
    """Decrypt an inbound Flow data-exchange request.

    Returns ``(decrypted_payload, aes_key, initial_vector)`` — the AES key and IV
    are needed to encrypt the matching response with :func:`encrypt_response`.
    """
    try:
        encrypted_aes_key = base64.b64decode(body["encrypted_aes_key"])
        encrypted_flow_data = base64.b64decode(body["encrypted_flow_data"])
        iv = base64.b64decode(body["initial_vector"])
    except (KeyError, ValueError, TypeError) as exc:
        raise FlowEncryptionError(f"Malformed encrypted request: {exc}") from exc

    try:
        private_key = _load_private_key()
        aes_key = private_key.decrypt(
            encrypted_aes_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
    except Exception as exc:  # bad/rotated key → ask WhatsApp to re-fetch it
        raise FlowEncryptionError(f"Could not decrypt AES key: {exc}") from exc

    try:
        # cryptography's AESGCM expects ciphertext||tag as a single blob.
        decrypted = AESGCM(aes_key).decrypt(iv, encrypted_flow_data, None)
        payload = json.loads(decrypted.decode("utf-8"))
    except Exception as exc:
        raise FlowEncryptionError(f"Could not decrypt flow data: {exc}") from exc

    if not isinstance(payload, dict):
        raise FlowEncryptionError("Decrypted payload was not a JSON object")
    return payload, aes_key, iv


def encrypt_response(response: dict[str, Any], aes_key: bytes, iv: bytes) -> str:
    """Encrypt a response object for WhatsApp. Returns the base64 string that is
    sent as the raw HTTP body."""
    flipped_iv = bytes(b ^ 0xFF for b in iv)
    plaintext = json.dumps(response).encode("utf-8")
    encrypted = AESGCM(aes_key).encrypt(flipped_iv, plaintext, None)
    return base64.b64encode(encrypted).decode("utf-8")
