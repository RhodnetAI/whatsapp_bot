import asyncio
import json
import math
import os
import uuid
from pathlib import Path
from typing import Any

from openai import OpenAI

from app.core.config import settings

try:
    from qdrant_client import AsyncQdrantClient  # type: ignore[import]
    from qdrant_client.http import models as qmodels  # type: ignore[import]
except ImportError:  # pragma: no cover
    AsyncQdrantClient = None
    qmodels = None

try:
    from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter  # type: ignore[import]
except ImportError:  # pragma: no cover
    MarkdownHeaderTextSplitter = None
    RecursiveCharacterTextSplitter = None

VECTOR_STORE_DIR = Path(__file__).resolve().parents[1] / "data"
VECTOR_STORE_PATH = VECTOR_STORE_DIR / "knowledge_vectors.json"
OPENAI_EMBEDDING_MODEL = settings.openai_embedding_model
OPENAI_EMBEDDING_DIM = settings.openai_embedding_dim
CHUNK_SIZE = int(os.getenv("EMBEDDING_CHUNK_SIZE", "800"))
CHUNK_OVERLAP = int(os.getenv("EMBEDDING_CHUNK_OVERLAP", "100"))
MAX_CHUNKS = int(os.getenv("MAX_EMBEDDING_CHUNKS", "20"))
QDRANT_COLLECTION = settings.qdrant_collection or "agent_chunks_v2"
QDRANT_VECTOR_NAME = "dense"

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


def _normalize_text(text: str) -> str:
    return "\n".join(
        line.strip()
        for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
        if line.strip()
    )


def _split_text_into_chunks(text: str) -> list[str]:
    normalized = _normalize_text(text)
    if not normalized:
        return []

    if MarkdownHeaderTextSplitter is not None and RecursiveCharacterTextSplitter is not None:
        markdown_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=[
                ("#", "Header 1"),
                ("##", "Header 2"),
                ("###", "Header 3"),
                ("####", "Header 4"),
            ],
            strip_headers=False,
        )
        recursive_splitter = RecursiveCharacterTextSplitter(
            chunk_size=CHUNK_SIZE,
            chunk_overlap=CHUNK_OVERLAP,
            separators=["\n\n", "\n", ".", " ", ""],
        )

        markdown_chunks = markdown_splitter.split_text(normalized)
        if len(markdown_chunks) > MAX_CHUNKS:
            return recursive_splitter.split_text(normalized)[:MAX_CHUNKS]

        return [chunk.page_content for chunk in markdown_chunks[:MAX_CHUNKS]]

    paragraphs = [paragraph.strip() for paragraph in normalized.split("\n\n") if paragraph.strip()]
    chunks: list[str] = []
    current = ""

    def append_chunk(chunk_text: str) -> None:
        if chunk_text:
            chunks.append(chunk_text.strip())

    for paragraph in paragraphs:
        if len(paragraph) <= CHUNK_SIZE:
            if current and len(current) + len(paragraph) + 2 > CHUNK_SIZE:
                append_chunk(current)
                current = paragraph
            else:
                current = f"{current}\n\n{paragraph}" if current else paragraph
        else:
            if current:
                append_chunk(current)
                current = ""
            for start in range(0, len(paragraph), CHUNK_SIZE - CHUNK_OVERLAP):
                slice_text = paragraph[start : start + CHUNK_SIZE].strip()
                if slice_text:
                    append_chunk(slice_text)

    if current:
        append_chunk(current)

    return chunks[:MAX_CHUNKS]


async def _create_embeddings(texts: list[str]) -> list[list[float]]:
    if settings.openai_api_key.strip() == "":
        return []

    # Sanitize inputs: ensure list of non-empty strings
    if not isinstance(texts, list):
        raise TypeError("_create_embeddings expects a list of strings")

    sanitized: list[str] = []
    for t in texts:
        if t is None:
            continue
        if not isinstance(t, str):
            try:
                t = str(t)
            except Exception:
                continue
        if not t.strip():
            continue
        sanitized.append(t)

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
        # Provide extra context for debugging (preserve original exception)
        raise RuntimeError(
            f"Embeddings API call failed (model={OPENAI_EMBEDDING_MODEL}, inputs={len(sanitized)}): {exc}"
        ) from exc

    embeddings: list[list[float]] = []
    for item in getattr(response, "data", []) or []:
        embedding = getattr(item, "embedding", None)
        if isinstance(embedding, list):
            embeddings.append(embedding)
    return embeddings


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


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

    await client.create_payload_index(
        collection_name=QDRANT_COLLECTION,
        field_name="document_id",
        field_schema=qmodels.PayloadSchemaType.KEYWORD,
        wait=True,
    )


async def _delete_qdrant_document(document_id: str) -> None:
    client = await _get_qdrant_client()
    if client is None or qmodels is None:
        return

    await client.delete(
        collection_name=QDRANT_COLLECTION,
        points_selector=qmodels.FilterSelector(
            filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="document_id",
                        match=qmodels.MatchValue(value=document_id),
                    )
                ]
            )
        ),
        wait=True,
    )


def _delete_local_vector_store_document(document_id: str) -> bool:
    items = _load_vector_store()
    if not items:
        return False

    next_items = [item for item in items if str(item.get("document_id")) != str(document_id)]
    if len(next_items) == len(items):
        return False

    _save_vector_store(next_items)
    return True


async def delete_document_vectors(document_id: str) -> None:
    """Delete all stored vectors for a document from Qdrant and local fallback storage."""
    qdrant_error: Exception | None = None
    try:
        await _delete_qdrant_document(document_id)
    except Exception as exc:
        qdrant_error = exc

    local_deleted = _delete_local_vector_store_document(document_id)

    if qdrant_error is not None and not local_deleted:
        raise RuntimeError(f"Failed to delete vectors for document {document_id}: {qdrant_error}") from qdrant_error


async def _store_to_qdrant(items: list[dict[str, Any]]) -> bool:
    client = await _get_qdrant_client()
    if client is None or qmodels is None:
        return False

    await _ensure_qdrant_collection(client)
    qdrant_points = []
    for item in items:
        qdrant_points.append(
            qmodels.PointStruct(
                id=item["id"],
                vector={QDRANT_VECTOR_NAME: item["embedding"]},
                payload={
                    "document_id": item["document_id"],
                    "original_name": item["original_name"],
                    "bucket_path": item["bucket_path"],
                    "chunk": item["chunk"],
                    "created_at": item["created_at"],
                },
            )
        )

    await client.upsert(collection_name=QDRANT_COLLECTION, points=qdrant_points)
    return True


async def store_document_vectors(
    document_id: str,
    original_name: str,
    bucket_path: str,
    text: str,
    created_at: str,
) -> None:
    chunks = _split_text_into_chunks(text)
    if not chunks:
        return

    embeddings = await _create_embeddings(chunks)
    if len(embeddings) != len(chunks):
        return

    items = [
        {
            "id": str(uuid.uuid4()),
            "document_id": document_id,
            "original_name": original_name,
            "bucket_path": bucket_path,
            "chunk": chunk_text,
            "embedding": embedding,
            "created_at": created_at,
        }
        for chunk_text, embedding in zip(chunks, embeddings)
    ]

    qdrant_success = await _store_to_qdrant(items)
    if not qdrant_success:
        raise RuntimeError("Unable to store vectors in Qdrant. Check QDRANT_URL/QDRANT_API_KEY and collection availability.")


async def search_vectors(query: str, top_k: int = 4) -> list[dict[str, Any]]:
    embeddings = await _create_embeddings([query])
    if not embeddings:
        return []

    query_embedding = embeddings[0]
    client = await _get_qdrant_client()
    if client is not None and qmodels is not None:
        await _ensure_qdrant_collection(client)
        try:
            # Try FastEmbed API first (query_text parameter)
            try:
                search_results = await client.query(
                    collection_name=QDRANT_COLLECTION,
                    query_text=query,
                    limit=top_k,
                    with_payload=True,
                )
            except TypeError:
                # Fallback to standard AsyncQdrantClient API (query parameter with embedding)
                search_results = await client.query(
                    collection_name=QDRANT_COLLECTION,
                    query=query_embedding,
                    vector_name=QDRANT_VECTOR_NAME,
                    limit=top_k,
                    with_payload=True,
                )
            return [
                {
                    **(result.payload or {}),
                    "score": getattr(result, "score", 0.0),
                }
                for result in search_results
                if result.payload
            ]
        except Exception as e:
            # Fallback to local search if Qdrant fails
            import logging
            logging.getLogger("whatsapp").exception(f"Qdrant search failed: {e}")
            pass

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
