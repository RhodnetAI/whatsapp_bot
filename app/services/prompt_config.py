"""Central access to LLM prompts stored in the ``app_config`` table.

Prompts live as ``app_config(key, value)`` rows so they can be edited without a
code deploy. ``get_prompt(key, default)`` returns the stored value, falling back
to ``default`` (the prior hardcoded text) when the row is missing or the database
is unreachable — so the bot always has a working prompt. A short in-process cache
avoids a database round-trip on every LLM call.

Prompts that need runtime data use ``[[TOKEN]]`` placeholders (chosen so they
don't clash with the literal ``{ }`` of JSON examples inside the prompts); callers
substitute them with ``str.replace`` after fetching.
"""

import logging
import time

from app.db.supabase_client import first_row, supabase, supabase_admin

logger = logging.getLogger("whatsapp")

_CACHE: dict[str, tuple[float, str]] = {}
_TTL_SECONDS = 60.0


def _db():
    return supabase_admin if supabase_admin is not None else supabase


def get_prompt(key: str, default: str = "") -> str:
    """Return the prompt stored under ``key`` in ``app_config``; fall back to
    ``default`` if it's absent or the lookup fails. Cached for ``_TTL_SECONDS``."""
    cached = _CACHE.get(key)
    now = time.time()
    if cached and now - cached[0] < _TTL_SECONDS:
        return cached[1]
    try:
        row = first_row(_db().table("app_config").select("value").eq("key", key).limit(1).execute())
        if row and row.get("value"):
            value = str(row["value"])
            _CACHE[key] = (now, value)
            return value
    except Exception:
        logger.exception("Failed to load prompt '%s' from app_config; using default", key)
        return default
    # Row missing / empty → use the default (don't cache, so a later seed is picked up).
    return default


def clear_cache() -> None:
    """Drop the in-process cache (e.g. after editing prompts)."""
    _CACHE.clear()
