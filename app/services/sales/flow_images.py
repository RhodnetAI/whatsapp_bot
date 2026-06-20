"""Image helper for the "Open Store" Flow's rich UI.

WhatsApp Flow ``Image`` and ``NavigationList`` thumbnails accept **base64**
image data only (not URLs), with tight size budgets:

* ``NavigationList`` item ``start.image`` — base64 string **≤ 100 KB**.
* ``Image`` ``src`` — JPEG/PNG, recommended ≤ 300 KB; total screen payload
  must stay under 1 MB.

So for each product we download its ``image_url`` once, downscale + JPEG-compress
it to fit those budgets, base64-encode it, and **cache** the result in memory
(keyed by url + size). Downloads are parallelised and time-boxed so a slow image
host can't stall the data-exchange response; any failure degrades gracefully
(thumbnail omitted; a neutral placeholder used on the detail screen)."""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image

logger = logging.getLogger("whatsapp")

# base64-length budgets (the spec limits are on the encoded string).
_THUMB_MAX_PX = 160
_THUMB_MAX_B64 = 70_000          # well under the 100 KB NavigationList cap
_DETAIL_MAX_PX = 640
_DETAIL_MAX_B64 = 260_000        # under the ~300 KB Image recommendation
_FETCH_TIMEOUT = 5               # seconds per image download
_MAX_CACHE = 512

_cache: dict[tuple[str, str], str] = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="flow-img")
_placeholder: str | None = None

# Persistent (on-disk) cache layer. The in-memory ``_cache`` is per-process and
# lost on restart / not shared across workers, so the first browse of each
# category would otherwise re-download + re-encode every image (risking the
# ~12 s endpoint budget). We additionally persist each *successful* encoded
# base64 string to a small file keyed by url+kind, so a new worker or a restart
# re-reads it instantly instead of refetching. Failures are not persisted (so a
# transient host outage is retried later). Override the location with
# ``WHATSAPP_FLOW_IMG_CACHE_DIR``.
_DISK_DIR = os.environ.get("WHATSAPP_FLOW_IMG_CACHE_DIR") or os.path.join(
    tempfile.gettempdir(), "wa_flow_imgcache"
)


def _disk_path(key: tuple[str, str]) -> str:
    digest = hashlib.sha1(f"{key[0]}|{key[1]}".encode("utf-8")).hexdigest()
    return os.path.join(_DISK_DIR, f"{digest}.b64")


def _disk_read(key: tuple[str, str]) -> str | None:
    try:
        with open(_disk_path(key), "r", encoding="ascii") as fh:
            return fh.read()
    except FileNotFoundError:
        return None
    except Exception:
        logger.info("Store Flow: image disk-cache read failed")
        return None


def _disk_write(key: tuple[str, str], value: str) -> None:
    if not value:  # never persist a failed/empty result — let it retry later
        return
    try:
        os.makedirs(_DISK_DIR, exist_ok=True)
        path = _disk_path(key)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="ascii") as fh:
            fh.write(value)
        os.replace(tmp, path)  # atomic swap so readers never see a partial file
    except Exception:
        logger.info("Store Flow: image disk-cache write failed")


def _download(url: str) -> bytes | None:
    try:
        resp = requests.get(url, timeout=_FETCH_TIMEOUT)
        if resp.status_code >= 400 or not resp.content:
            return None
        return resp.content
    except Exception:
        logger.info("Store Flow: image download failed for %s", url)
        return None


def _encode(raw: bytes, max_px: int, max_b64: int) -> str | None:
    """Downscale to ``max_px`` and JPEG-compress until the base64 string fits
    ``max_b64``; returns None if it can't be made small enough."""
    try:
        img = Image.open(io.BytesIO(raw))
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((max_px, max_px))
        for quality in (80, 70, 60, 50, 40, 30, 20):
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            if len(b64) <= max_b64:
                return b64
    except Exception:
        logger.info("Store Flow: image encode failed")
    return None


def _get(url: str, kind: str, max_px: int, max_b64: int) -> str:
    if not url or not url.lower().startswith(("http://", "https://")):
        return ""
    key = (url, kind)
    with _lock:
        if key in _cache:
            return _cache[key]
    # Persistent disk cache — survives restarts and is shared across workers.
    disk = _disk_read(key)
    if disk is not None:
        with _lock:
            if len(_cache) >= _MAX_CACHE:
                _cache.clear()
            _cache[key] = disk
        return disk
    raw = _download(url)
    value = _encode(raw, max_px, max_b64) if raw else None
    value = value or ""
    with _lock:
        if len(_cache) >= _MAX_CACHE:
            _cache.clear()
        _cache[key] = value
    _disk_write(key, value)  # persists only non-empty (successful) results
    return value


def thumbnail_b64(url: str) -> str:
    """Small base64 JPEG for a NavigationList thumbnail, or "" if unavailable."""
    return _get(url, "thumb", _THUMB_MAX_PX, _THUMB_MAX_B64)


def thumbnails_b64(urls: list[str]) -> dict[str, str]:
    """Fetch many thumbnails in parallel (cache-aware). Maps url -> base64/""."""
    uniq = [u for u in dict.fromkeys(urls) if u]
    if not uniq:
        return {}
    results = list(_pool.map(thumbnail_b64, uniq))
    return dict(zip(uniq, results))


def detail_b64(url: str) -> str:
    """Larger base64 JPEG for the product-detail Image; falls back to a neutral
    placeholder so the Image component always has valid ``src``."""
    b64 = _get(url, "detail", _DETAIL_MAX_PX, _DETAIL_MAX_B64)
    return b64 or _placeholder_b64()


def _placeholder_b64() -> str:
    """A small neutral 'no image' panel, generated once and cached."""
    global _placeholder
    if _placeholder is None:
        img = Image.new("RGB", (600, 400), (235, 236, 238))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70, optimize=True)
        _placeholder = base64.b64encode(buf.getvalue()).decode("ascii")
    return _placeholder
