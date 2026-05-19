from typing import Any

from supabase import Client, create_client

from app.core.config import settings


# User client for REST queries (uses anon/user key)
supabase: Client = create_client(settings.supabase_url, settings.supabase_key)

# Admin client for storage operations (uses service role key if available)
supabase_admin: Client | None = None
if settings.supabase_service_role_key:
    supabase_admin = create_client(settings.supabase_url, settings.supabase_service_role_key)


def first_row(result: Any) -> dict[str, Any] | None:
    if result is None:
        return None
    data = getattr(result, "data", None)
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    return first if isinstance(first, dict) else None
