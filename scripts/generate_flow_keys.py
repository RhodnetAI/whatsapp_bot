"""One-time setup helper for the "Open Store" data-exchange Flow (Path B).

Every dynamic WhatsApp Flow encrypts its data-exchange payloads with an RSA
keypair: you upload the **public** key to WhatsApp, and the backend keeps the
**private** key (``WHATSAPP_FLOW_PRIVATE_KEY``) to decrypt requests / encrypt
responses (see ``app/services/sales/flow_crypto.py``).

Usage (from the ``backend`` folder, with the venv active):

    # 1. Generate a keypair and print env + registration instructions:
    python scripts/generate_flow_keys.py

    # 2. Generate AND register the public key with WhatsApp in one go
    #    (needs META_ACCESS_TOKEN + PHONE_NUMBER_ID in the environment/.env):
    python scripts/generate_flow_keys.py --register

The private key is printed once. Copy it into ``backend/.env`` as a single line
with literal ``\\n`` between the PEM lines (the backend un-escapes it), e.g.

    WHATSAPP_FLOW_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\\nMIIE...\\n-----END PRIVATE KEY-----\\n"

This script does NOT write to .env for you — handling a private key is a manual,
deliberate step.
"""

from __future__ import annotations

import argparse
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def generate_keypair() -> tuple[str, str]:
    """Return ``(private_pem, public_pem)`` as PEM strings."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_pem, public_pem


def register_public_key(public_pem: str) -> None:
    """Upload the public key to ``/{phone-number-id}/whatsapp_business_encryption``."""
    import requests  # local import so key-gen works without the dep present

    token = os.getenv("META_ACCESS_TOKEN", "")
    phone_number_id = os.getenv("PHONE_NUMBER_ID", "")
    if not token or not phone_number_id:
        sys.exit("META_ACCESS_TOKEN and PHONE_NUMBER_ID must be set to --register.")

    url = f"https://graph.facebook.com/v25.0/{phone_number_id}/whatsapp_business_encryption"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        data={"business_public_key": public_pem},
        timeout=30,
    )
    print(f"\nRegistration response: {resp.status_code} {resp.text}")
    if resp.status_code >= 400:
        sys.exit("Public key registration failed — check the token/phone number id.")
    print("✅ Public key registered. Verify with:")
    print(
        f"  curl -s 'https://graph.facebook.com/v25.0/{phone_number_id}"
        f"/whatsapp_business_encryption' -H 'Authorization: Bearer <TOKEN>'"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate/register the Flow encryption keypair.")
    parser.add_argument("--register", action="store_true", help="Upload the public key to WhatsApp.")
    args = parser.parse_args()

    private_pem, public_pem = generate_keypair()

    print("=" * 70)
    print("PUBLIC KEY (upload this to WhatsApp):")
    print("=" * 70)
    print(public_pem)

    print("=" * 70)
    print("PRIVATE KEY — keep secret; put in backend/.env as WHATSAPP_FLOW_PRIVATE_KEY")
    print("(single line, with literal \\n between PEM lines):")
    print("=" * 70)
    print(repr(private_pem.replace("\n", "\\n")))
    print()

    if args.register:
        register_public_key(public_pem)
    else:
        print("Next: set WHATSAPP_FLOW_PRIVATE_KEY in .env, then register the public key:")
        print("  python scripts/generate_flow_keys.py --register")
        print("(or POST it manually to /{phone-number-id}/whatsapp_business_encryption)")


if __name__ == "__main__":
    main()
