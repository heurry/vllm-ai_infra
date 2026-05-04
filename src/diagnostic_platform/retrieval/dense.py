"""Dense retrieval over Milvus vector indexes."""

from __future__ import annotations

from pathlib import Path
import time

from diagnostic_platform.indexing.vector_index import (
    default_vector_manifest_path,
    load_vector_manifest,
    validate_vector_index_manifest,
)
from diagnostic_platform.retrieval.embedding import EmbeddingClient, build_embedding_client
from diagnostic_platform.retrieval.vector_store import MilvusVectorStore, VectorSearchResult, VectorStore
from diagnostic_platform.schemas import RetrievedChunk, RetrievalQueryRequest, RetrievalResponse, VectorIndexManifest


def query_dense_index(
    request: RetrievalQueryRequest,
    *,
    embedding_client: EmbeddingClient | None = None,
    vector_store: VectorStore | None = None,
) -> RetrievalResponse:
    """Query a Milvus vector index and return traceable chunks."""

    started = time.monotonic()
    manifest = _resolve_manifest(request)
    collection_name = request.vector_collection or (manifest.collection_name if manifest else "")
    milvus_uri = request.milvus_uri or (manifest.milvus_uri if manifest else "http://127.0.0.1:19530")
    embedding_model = request.embedding_model or (manifest.embedding_model if manifest else "BAAI/bge-m3")
    if not collection_name:
        collection_name = f"diagnostic_knowledge_{Path(request.index_dir).name}"

    embedder = embedding_client or build_embedding_client(embedding_model)
    query_vector = embedder.embed_texts([request.query])[0]
    store = vector_store or MilvusVectorStore(uri=milvus_uri, collection_name=collection_name)
    if manifest is not None:
        validation = validate_vector_index_manifest(manifest, vector_store=store)
        if not validation["valid"]:
            raise RuntimeError(f"Vector index manifest validation failed: {validation['errors']}")
    results = store.search(query_vector, top_k=request.dense_top_k, filters=request.filters)
    chunks = [_chunk_from_result(result) for result in results]
    trace = {
        "dense": {
            "enabled": True,
            "collection_name": collection_name,
            "milvus_uri": milvus_uri,
            "embedding_model": embedder.model_name,
            "embedding_dimension": len(query_vector),
            "top_k": request.dense_top_k,
            "latency_seconds": round(time.monotonic() - started, 6),
            "results": [_trace_result(result) for result in results],
        }
    }
    return RetrievalResponse(
        query=request.query,
        rewritten_query=request.query,
        chunks=chunks,
        context=_pack_context(chunks),
        trace=trace,
    )


def _resolve_manifest(request: RetrievalQueryRequest) -> VectorIndexManifest | None:
    manifest_path = Path(request.vector_manifest_path) if request.vector_manifest_path else default_vector_manifest_path(request.index_dir)
    if manifest_path.exists():
        return load_vector_manifest(manifest_path)
    return None


def _chunk_from_result(result: VectorSearchResult) -> RetrievedChunk:
    metadata = {
        **result.record.metadata,
        "record_type": result.record.record_type,
        "source_id": result.record.source_id,
        "primary_id": result.record.primary_id,
    }
    return RetrievedChunk(
        chunk_id=result.record.source_id,
        score=result.score,
        source="dense",
        content=result.record.content,
        metadata=metadata,
    )


def _trace_result(result: VectorSearchResult) -> dict:
    return {
        "chunk_id": result.record.source_id,
        "primary_id": result.record.primary_id,
        "record_type": result.record.record_type,
        "score": result.score,
        "metadata": result.record.metadata,
    }


def _pack_context(chunks: list[RetrievedChunk]) -> str:
    parts: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        metadata = chunk.metadata
        parts.append(
            "\n".join(
                [
                    f"[{index}] chunk_id={chunk.chunk_id} source=dense score={chunk.score}",
                    f"source={metadata.get('source_path') or ''} page_idx={metadata.get('page_idx')}",
                    chunk.content,
                ]
            )
        )
    return "\n\n".join(parts)
