import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise RuntimeError(f"{name} environment variable is required")
    return value


def _flow_private_key() -> str:
    """Load the WhatsApp Flow RSA private key, tolerating the many ways a PEM can
    get mangled when pasted into a single-line env var (Render dashboard, .env):

    * surrounding single/double quotes,
    * literal ``\\n`` / ``\\r\\n`` escapes that were never turned into newlines,
    * a base64-encoded PEM (no ``BEGIN`` marker) — decoded transparently,
    * stray leading/trailing whitespace.

    The goal is that the same key works whether it was pasted as a real
    multi-line value or as an escaped one-liner."""
    raw = os.getenv("WHATSAPP_FLOW_PRIVATE_KEY", "")
    if not raw.strip():
        return ""
    raw = raw.strip()
    # Strip one layer of matching surrounding quotes.
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        raw = raw[1:-1]
    # Allow a base64-encoded PEM (avoids all newline/escaping issues entirely).
    if "BEGIN" not in raw:
        try:
            import base64

            decoded = base64.b64decode(raw, validate=False).decode("utf-8")
            if "BEGIN" in decoded:
                raw = decoded
        except Exception:
            pass
    # Turn literal escape sequences into real newlines (handles single- and
    # double-escaped values, e.g. "\\n" and "\\\\n").
    if "BEGIN" in raw and "\n" not in raw:
        raw = raw.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    # Drop any stray backslashes left clinging to line boundaries.
    raw = raw.replace("\\\n", "\n").replace("\n\\", "\n")
    return raw.strip() + "\n"


@dataclass(frozen=True)
class Settings:
    meta_access_token: str
    phone_number_id: str
    verify_token: str
    openai_api_key: str
    openai_embedding_model: str
    openai_embedding_dim: int
    qdrant_url: str
    qdrant_api_key: str
    qdrant_grpc_port: int | None
    qdrant_collection: str
    qdrant_product_collection: str
    unstructured_api_key: str
    unstructured_api_url: str
    groq_api_key: str
    voyage_api_key: str
    supabase_url: str
    supabase_key: str
    supabase_service_role_key: str | None
    admin_username: str
    admin_password: str
    admin_email: str
    resend_api_key: str
    resend_from_email: str
    google_meet_link: str
    google_service_account_json: str
    google_calendar_id: str
    google_calendar_timezone: str
    # Sales Bot — Razorpay + Meta Commerce Catalog + WhatsApp Flow
    razorpay_key_id: str
    razorpay_key_secret: str
    razorpay_webhook_secret: str
    meta_catalog_id: str
    whatsapp_checkout_flow_id: str
    public_base_url: str
    # Sales Bot — "Open Store" data-exchange Flow (Path B). The store Flow is a
    # single dynamic Flow that covers browse/cart/track/checkout; it requires an
    # RSA keypair registered with WhatsApp Business Encryption (see
    # backend/scripts/generate_flow_keys.py). Leaving these blank keeps the bot in
    # the chat-based browsing fallback (graceful degradation).
    whatsapp_store_flow_id: str
    whatsapp_flow_private_key: str
    whatsapp_flow_private_key_passphrase: str


settings = Settings(
    meta_access_token=_required_env("META_ACCESS_TOKEN"),
    phone_number_id=_required_env("PHONE_NUMBER_ID"),
    verify_token=_required_env("VERIFY_TOKEN"),
    openai_api_key=os.getenv("OPENAI_KEY", ""),
    openai_embedding_model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
    openai_embedding_dim=int(os.getenv("OPENAI_EMBEDDING_DIM", "1536")),
    qdrant_url=os.getenv("QDRANT_URL", ""),
    qdrant_api_key=os.getenv("QDRANT_API_KEY", ""),
    qdrant_grpc_port=int(os.getenv("QDRANT_GRPC_PORT", "0")) if os.getenv("QDRANT_GRPC_PORT") else None,
    qdrant_collection=os.getenv("QDRANT_COLLECTION", "agent_chunks_v2"),
    qdrant_product_collection=os.getenv("QDRANT_PRODUCT_COLLECTION", "product_chunks_v2"),
    unstructured_api_key=os.getenv("UNSTRUCTURED_API_KEY", ""),
    unstructured_api_url=os.getenv("UNSTRUCTURED_API_URL", ""),
    groq_api_key=os.getenv("GROQ_API_KEY", ""),
    voyage_api_key=os.getenv("VOYAGE_API_KEY", ""),
    supabase_url=_required_env("SUPABASE_URL"),
    supabase_key=_required_env("SUPABASE_KEY"),
    supabase_service_role_key=os.getenv("SUPABASE_SERVICE_ROLE_KEY"),
    admin_username=_required_env("ADMIN_USERNAME"),
    admin_password=_required_env("ADMIN_PASSWORD"),
    admin_email=os.getenv("ADMIN_EMAIL", ""),
    resend_api_key=os.getenv("RESEND_API_KEY", ""),
    resend_from_email=os.getenv("RESEND_FROM_EMAIL", "onboarding@resend.dev"),
    google_meet_link=os.getenv("GOOGLE_MEET_LINK", "https://meet.google.com/yry-mdbu-pju"),
    google_service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", ""),
    google_calendar_id=os.getenv("GOOGLE_CALENDAR_ID", "primary"),
    google_calendar_timezone=os.getenv("GOOGLE_CALENDAR_TIMEZONE", "Asia/Kolkata"),
    razorpay_key_id=os.getenv("RAZORPAY_KEY_ID", ""),
    razorpay_key_secret=os.getenv("RAZORPAY_KEY_SECRET", ""),
    razorpay_webhook_secret=os.getenv("RAZORPAY_WEBHOOK_SECRET", ""),
    meta_catalog_id=os.getenv("META_CATALOG_ID", ""),
    whatsapp_checkout_flow_id=os.getenv("WHATSAPP_CHECKOUT_FLOW_ID", ""),
    public_base_url=os.getenv("PUBLIC_BASE_URL", ""),
    whatsapp_store_flow_id=os.getenv("WHATSAPP_STORE_FLOW_ID", ""),
    # PEM string; normalised by _flow_private_key() to tolerate quoting, escaped
    # \n, base64, and stray backslashes from single-line env vars.
    whatsapp_flow_private_key=_flow_private_key(),
    whatsapp_flow_private_key_passphrase=os.getenv("WHATSAPP_FLOW_PRIVATE_KEY_PASSPHRASE", ""),
)
