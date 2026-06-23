"""Semantic retrieval for Products & Services, mirroring the knowledge-document
RAG pattern in ``vectorizer.py`` (OpenAI embeddings + Qdrant). Qdrant is the
only vector store — there is no local-file fallback, by design: a fallback
that silently served stale/partial data was worse than surfacing "not
configured" loudly.

Each product/service row is embedded as a single point (no chunking — rows are
short structured records), keyed by its own ``id`` so re-saving an item upserts
its vector instead of duplicating it. This lets the Information Agent retrieve
only the items relevant to the user's query instead of injecting the entire
catalog into every prompt.
"""

import asyncio
import logging
import os
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

OPENAI_EMBEDDING_MODEL = settings.openai_embedding_model
OPENAI_EMBEDDING_DIM = settings.openai_embedding_dim
QDRANT_COLLECTION = settings.qdrant_product_collection
QDRANT_VECTOR_NAME = "dense"

# Dense search always returns the k-nearest vectors, however unrelated they
# actually are to the query (e.g. "hello i am harish" still has *some*
# closest products). A minimum cosine-similarity score keeps retrieval
# query-based without falling back to keyword matching: genuinely relevant
# matches clear this bar, small talk doesn't, so nothing gets injected.
PRODUCT_SEARCH_SCORE_THRESHOLD = float(os.getenv("PRODUCT_SEARCH_SCORE_THRESHOLD", "0.3"))

# Fields that carry an item's *meaning* for retrieval. Structured fields
# (price/status/counts) are kept in the payload for prompt formatting but
# don't meaningfully change what a semantic search should match on. "kind"
# is handled separately (see _build_embedding_text) since it's a tag, not
# free text.
_EMBED_FIELDS = ("name", "category", "short_description", "full_description")

_KIND_LABELS = {"product": "Product", "service": "Service"}

# Payload fields mirror information_bot_products_services columns 1:1 so the
# search result can be formatted by bot_chat.py the same way a DB row is.
# "kind" tags each point as a product or a service, so a generic query like
# "give me your services" can match on the tag itself rather than needing to
# resemble a specific item's description.
_PAYLOAD_FIELDS = (
    "kind",
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


def _build_embedding_text(fields: dict[str, Any]) -> str:
    kind_label = _KIND_LABELS.get(str(fields.get("kind") or "").strip().lower())
    parts = [f"Type: {kind_label}"] if kind_label else []
    parts.extend(str(fields.get(name) or "").strip() for name in _EMBED_FIELDS)
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
    # Use get_collections() rather than collection_exists() — the latter hits
    # GET /collections/{name}/exists, which some Qdrant deployments/proxies
    # don't implement and answer with a bare 404 instead of a JSON error.
    # get_collections() (GET /collections) is the long-stable listing endpoint.
    collections = await client.get_collections()
    existing_names = {c.name for c in collections.collections}
    if QDRANT_COLLECTION not in existing_names:
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
    """Embed one product/service row and upsert it into Qdrant. The point id
    is the row's own ``id``, so saving the same item again overwrites its
    vector in place instead of accumulating duplicates."""
    text = _build_embedding_text(fields)
    if not text:
        return

    embeddings = await _create_embeddings([text])
    if not embeddings:
        raise RuntimeError("Embeddings unavailable (OPENAI_KEY not configured)")
    embedding = embeddings[0]
    payload = _payload_for(fields)

    client = await _get_qdrant_client()
    if client is None or qmodels is None:
        raise RuntimeError("Qdrant unavailable (QDRANT_URL not configured)")

    await _ensure_qdrant_collection(client)
    await client.upsert(
        collection_name=QDRANT_COLLECTION,
        points=[qmodels.PointStruct(id=item_id, vector={QDRANT_VECTOR_NAME: embedding}, payload=payload)],
    )


async def delete_product_vector(item_id: str) -> None:
    client = await _get_qdrant_client()
    if client is None or qmodels is None:
        return
    await client.delete(
        collection_name=QDRANT_COLLECTION,
        points_selector=qmodels.PointIdsList(points=[item_id]),
    )


async def search_products(query: str, top_k: int = 6) -> list[dict[str, Any]]:
    """Semantic search over indexed product/service vectors in Qdrant, ranked
    by relevance to ``query``. Returns payload dicts (DB-column-shaped) for
    matches scoring at or above PRODUCT_SEARCH_SCORE_THRESHOLD. Returns an
    empty list whenever Qdrant has nothing to offer — unrelated queries
    (small talk, greetings), no embeddings configured, or Qdrant
    unavailable — there is no other store to fall back to."""
    embeddings = await _create_embeddings([query])
    if not embeddings:
        return []
    query_embedding = embeddings[0]

    client = await _get_qdrant_client()
    if client is None or qmodels is None:
        return []

    await _ensure_qdrant_collection(client)
    try:
        results = await client.query_points(
            collection_name=QDRANT_COLLECTION,
            query=query_embedding,
            using=QDRANT_VECTOR_NAME,
            limit=top_k,
            score_threshold=PRODUCT_SEARCH_SCORE_THRESHOLD,
            with_payload=True,
        )
        return [dict(point.payload) for point in results.points if point.payload]
    except Exception:
        logger.exception("Qdrant product/service search failed")
        return []
