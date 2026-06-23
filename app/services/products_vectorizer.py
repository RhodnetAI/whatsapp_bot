"""Semantic retrieval for Products & Services, mirroring the knowledge-document
RAG pattern in ``vectorizer.py`` (OpenAI embeddings + Qdrant, with a local JSON
fallback when Qdrant isn't configured).

Each product/service row is embedded as a single point (no chunking — rows are
short structured records), keyed by its own ``id`` so re-saving an item upserts
its vector instead of duplicating it. This lets the Information Agent retrieve
only the items relevant to the user's query instead of injecting the entire
catalog into every prompt.
"""

import asyncio
import json
import logging
import math
from pathlib import Path
from typing import Any

from openai import OpenAI

from app.core.config import settings

logger = logging.getLogger("whatsapp")

try:
    from qdrant_client import AsyncQdrantClient  # type: ignore[import]
    from qdrant_client.http import models as qmodels  # type: ignore[import]
except ImportError:  # pragma: no cover
    AsyncQdrantClient = None
    qmodels = None

VECTOR_STORE_DIR = Path(__file__).resolve().parents[1] / "data"
VECTOR_STORE_PATH = VECTOR_STORE_DIR / "product_vectors.json"
OPENAI_EMBEDDING_MODEL = settings.openai_embedding_model
OPENAI_EMBEDDING_DIM = settings.openai_embedding_dim
QDRANT_COLLECTION = settings.qdrant_product_collection
QDRANT_VECTOR_NAME = "dense"

# Fields that carry an item's *meaning* for retrieval. Structured fields
# (price/status/counts) are kept in the payload for prompt formatting but
# don't meaningfully change what a semantic search should match on.
_EMBED_FIELDS = ("name", "category", "short_description", "full_description")

# Payload fields mirror information_bot_products_services columns 1:1 so the
# search result can be formatted by bot_chat.py the same way a DB row is.
_PAYLOAD_FIELDS = (
    "name",
    "short_description",
    "full_description",
    "category",
    "price",
    "discount_price",
    "status",
    "images",
    "rating",
    "reviews_count",
    "purchased_count",
)

_qdrant_client_instance: Any = None


def _ensure_vector_store_dir() -> None:
    VECTOR_STORE_DIR.mkdir(parents=True, exist_ok=True)


def _load_vector_store() -> list[dict[str, Any]]:
    _ensure_vector_store_dir()
    if not VECTOR_STORE_PATH.exists():
        return []
    try:
        raw = VECTOR_STORE_PATH.read_text(encoding="utf-8")
        return json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError:
        return []


def _save_vector_store(items: list[dict[str, Any]]) -> None:
    _ensure_vector_store_dir()
    VECTOR_STORE_PATH.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _build_embedding_text(fields: dict[str, Any]) -> str:
    parts = [str(fields.get(name) or "").strip() for name in _EMBED_FIELDS]
    return "\n".join(part for part in parts if part)


def _payload_for(fields: dict[str, Any]) -> dict[str, Any]:
    return {name: fields.get(name) or "" for name in _PAYLOAD_FIELDS}


async def _create_embeddings(texts: list[str]) -> list[list[float]]:
    if settings.openai_api_key.strip() == "":
        return []

    sanitized = [t for t in texts if isinstance(t, str) and t.strip()]
    if not sanitized:
        return []

    client = OpenAI(api_key=settings.openai_api_key)
    try:
        response = await asyncio.to_thread(
            client.embeddings.create,
            model=OPENAI_EMBEDDING_MODEL,
            input=sanitized,
        )
    except Exception as exc:
        raise RuntimeError(
            f"Embeddings API call failed (model={OPENAI_EMBEDDING_MODEL}, inputs={len(sanitized)}): {exc}"
        ) from exc

    embeddings: list[list[float]] = []
    for item in getattr(response, "data", []) or []:
        embedding = getattr(item, "embedding", None)
        if isinstance(embedding, list):
            embeddings.append(embedding)
    return embeddings


async def _get_qdrant_client() -> Any:
    global _qdrant_client_instance
    if _qdrant_client_instance is not None:
        return _qdrant_client_instance

    if AsyncQdrantClient is None or not settings.qdrant_url.strip():
        return None

    client_kwargs: dict[str, Any] = {
        "url": settings.qdrant_url,
        "api_key": settings.qdrant_api_key,
    }
    if settings.qdrant_grpc_port:
        client_kwargs["grpc_port"] = settings.qdrant_grpc_port

    _qdrant_client_instance = AsyncQdrantClient(**client_kwargs)
    return _qdrant_client_instance


async def _ensure_qdrant_collection(client: Any) -> None:
    if qmodels is None:
        return
    exists = await client.collection_exists(collection_name=QDRANT_COLLECTION)
    if not exists:
        await client.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config={
                QDRANT_VECTOR_NAME: qmodels.VectorParams(
                    size=OPENAI_EMBEDDING_DIM,
                    distance=qmodels.Distance.COSINE,
                )
            },
        )


async def store_product_vector(item_id: str, fields: dict[str, Any]) -> None:
    """Embed one product/service row and upsert it. The Qdrant point id is the
    row's own ``id``, so saving the same item again overwrites its vector
    in place instead of accumulating duplicates."""
    text = _build_embedding_text(fields)
    if not text:
        return

    embeddings = await _create_embeddings([text])
    if not embeddings:
        raise RuntimeError("Embeddings unavailable (OPENAI_KEY not configured)")
    embedding = embeddings[0]
    payload = _payload_for(fields)

    stored_remote = False
    client = await _get_qdrant_client()
    if client is not None and qmodels is not None:
        await _ensure_qdrant_collection(client)
        await client.upsert(
            collection_name=QDRANT_COLLECTION,
            points=[qmodels.PointStruct(id=item_id, vector={QDRANT_VECTOR_NAME: embedding}, payload=payload)],
        )
        stored_remote = True

    items = _load_vector_store()
    items = [item for item in items if str(item.get("id")) != str(item_id)]
    items.append({**payload, "id": item_id, "embedding": embedding})
    _save_vector_store(items)

    if not stored_remote:
        logger.warning("Qdrant unavailable; product/service %s vector stored to local fallback only", item_id)


async def delete_product_vector(item_id: str) -> None:
    client = await _get_qdrant_client()
    if client is not None and qmodels is not None:
        try:
            await client.delete(
                collection_name=QDRANT_COLLECTION,
                points_selector=qmodels.PointIdsList(points=[item_id]),
            )
        except Exception:
            logger.warning("Failed to delete product/service vector %s from Qdrant", item_id)

    items = _load_vector_store()
    next_items = [item for item in items if str(item.get("id")) != str(item_id)]
    if len(next_items) != len(items):
        _save_vector_store(next_items)


async def search_products(query: str, top_k: int = 6) -> list[dict[str, Any]]:
    """Semantic search over indexed product/service vectors, ranked by
    relevance to ``query``. Returns payload dicts (DB-column-shaped) for the
    top matches, or an empty list if embeddings are unavailable or nothing
    has been indexed yet — callers should fall back to a bounded query in
    that case rather than treating it as "no products exist"."""
    embeddings = await _create_embeddings([query])
    if not embeddings:
        return []
    query_embedding = embeddings[0]

    client = await _get_qdrant_client()
    if client is not None and qmodels is not None:
        await _ensure_qdrant_collection(client)
        try:
            results = await client.query_points(
                collection_name=QDRANT_COLLECTION,
                query=query_embedding,
                using=QDRANT_VECTOR_NAME,
                limit=top_k,
                with_payload=True,
            )
            matches = [dict(point.payload) for point in results.points if point.payload]
            if matches:
                return matches
        except Exception:
            logger.exception("Qdrant product/service search failed")

    items = _load_vector_store()
    scored: list[tuple[float, dict[str, Any]]] = []
    for item in items:
        embedding = item.get("embedding")
        if not isinstance(embedding, list):
            continue
        score = _cosine_similarity(query_embedding, embedding)
        scored.append((score, item))
    scored.sort(key=lambda entry: entry[0], reverse=True)
    return [item for _, item in scored[:top_k]]
