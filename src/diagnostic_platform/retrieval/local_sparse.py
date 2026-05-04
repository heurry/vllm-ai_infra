"""Dependency-free sparse retrieval over local knowledge JSONL."""

from __future__ import annotations

from collections import Counter
import json
import math
from pathlib import Path
import re

from diagnostic_platform.indexing.local_builder import KNOWLEDGE_UNITS_FILE
from diagnostic_platform.retrieval.dense import query_dense_index
from diagnostic_platform.retrieval.embedding import EmbeddingClient
from diagnostic_platform.retrieval.vector_store import VectorStore
from diagnostic_platform.schemas import (
    KnowledgeUnit,
    RetrievedChunk,
    RetrievalFilters,
    RetrievalQueryRequest,
    RetrievalResponse,
)

TOKEN_RE = re.compile(r"0x[0-9a-fA-F]+|[A-Za-z]+[A-Za-z0-9]*|[0-9a-fA-F]{2,8}")


def query_local_index(
    request: RetrievalQueryRequest,
    *,
    embedding_client: EmbeddingClient | None = None,
    vector_store: VectorStore | None = None,
) -> RetrievalResponse:
    """Query local sparse and optional dense indexes and pack the top chunks as context."""

    sparse_chunks: list[RetrievedChunk] = []
    trace: dict = {
        "query": request.query,
        "sparse": {"enabled": request.enable_sparse, "top_k": request.top_k, "results": []},
        "dense": {"enabled": request.enable_dense, "top_k": request.dense_top_k, "results": []},
        "hybrid": {
            "enabled": request.enable_sparse and request.enable_dense,
            "top_k": request.hybrid_top_k,
            "dense_weight": request.dense_weight,
            "sparse_weight": request.sparse_weight,
            "results": [],
        },
    }

    if request.enable_sparse:
        units = _load_units(Path(request.index_dir) / KNOWLEDGE_UNITS_FILE)
        candidates = [unit for unit in units if _matches_filters(unit, request.filters)]
        scored = _score_units(request.query, candidates)
        sparse_chunks = [
            _to_retrieved_chunk(unit=unit, score=score)
            for unit, score in scored[: request.top_k]
            if score > 0
        ]
        trace["sparse"]["results"] = [_trace_chunk(chunk) for chunk in sparse_chunks]

    dense_chunks: list[RetrievedChunk] = []
    dense_trace: dict = {}
    if request.enable_dense:
        dense_response = query_dense_index(
            request,
            embedding_client=embedding_client,
            vector_store=vector_store,
        )
        dense_chunks = dense_response.chunks
        dense_trace = dense_response.trace.get("dense") or {}
        trace["dense"] = dense_trace

    chunks = _merge_hybrid_chunks(
        sparse_chunks=sparse_chunks,
        dense_chunks=dense_chunks,
        sparse_weight=request.sparse_weight,
        dense_weight=request.dense_weight,
        top_k=request.hybrid_top_k if request.enable_dense and request.enable_sparse else request.top_k,
    )
    if request.enable_dense and request.enable_sparse:
        trace["hybrid"]["results"] = [_trace_chunk(chunk) for chunk in chunks]
    return RetrievalResponse(
        query=request.query,
        rewritten_query=request.query,
        chunks=chunks,
        context=_pack_context(chunks),
        trace=trace,
    )


def _load_units(path: Path) -> list[KnowledgeUnit]:
    if not path.exists():
        raise FileNotFoundError(f"Knowledge unit index does not exist: {path}")

    units: list[KnowledgeUnit] = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            if line.strip():
                units.append(KnowledgeUnit.model_validate(json.loads(line)))
    return units


def _matches_filters(unit: KnowledgeUnit, filters: RetrievalFilters) -> bool:
    if filters.doc_ids and not any(unit.chunk_id.startswith(f"{doc_id}_") for doc_id in filters.doc_ids):
        return False
    if filters.unit_types and unit.unit_type not in filters.unit_types:
        return False
    if filters.protocol and unit.protocol.lower() != filters.protocol.lower():
        return False
    if filters.module and (unit.module or "").lower() != filters.module.lower():
        return False
    if filters.ecu and (unit.ecu or "").lower() != filters.ecu.lower():
        return False
    if filters.service_ids and not _overlaps(unit.service_ids, filters.service_ids):
        return False
    if filters.dids and not _overlaps(unit.dids, filters.dids):
        return False
    if filters.sessions and not _overlaps(unit.sessions, filters.sessions):
        return False
    if filters.security_levels and not _overlaps(unit.security_levels, filters.security_levels):
        return False
    return True


def _score_units(query: str, units: list[KnowledgeUnit]) -> list[tuple[KnowledgeUnit, float]]:
    query_terms = _tokens(query)
    if not query_terms or not units:
        return []

    unit_tokens = [_tokens(_searchable_text(unit)) for unit in units]
    document_frequencies: Counter[str] = Counter()
    for tokens in unit_tokens:
        document_frequencies.update(set(tokens))

    avg_len = sum(len(tokens) for tokens in unit_tokens) / len(unit_tokens)
    scored: list[tuple[KnowledgeUnit, float]] = []
    for unit, tokens in zip(units, unit_tokens):
        score = _bm25_score(query_terms, tokens, document_frequencies, len(units), avg_len)
        score += _exact_match_boost(query, unit)
        if score > 0:
            scored.append((unit, round(score, 6)))

    scored.sort(key=lambda item: (-item[1], item[0].chunk_id))
    return scored


def _bm25_score(
    query_terms: list[str],
    tokens: list[str],
    document_frequencies: Counter[str],
    document_count: int,
    avg_len: float,
) -> float:
    token_counts = Counter(tokens)
    score = 0.0
    k1 = 1.5
    b = 0.75
    doc_len = max(len(tokens), 1)
    for term in query_terms:
        term_frequency = token_counts[term]
        if term_frequency == 0:
            continue
        doc_frequency = document_frequencies[term]
        idf = math.log(1 + (document_count - doc_frequency + 0.5) / (doc_frequency + 0.5))
        denominator = term_frequency + k1 * (1 - b + b * doc_len / max(avg_len, 1.0))
        score += idf * term_frequency * (k1 + 1) / denominator
    return score


def _exact_match_boost(query: str, unit: KnowledgeUnit) -> float:
    normalized_query = _normalize_text(query)
    normalized_content = _normalize_text(unit.content)
    score = 0.0
    if normalized_query and normalized_query in normalized_content:
        score += 5.0
    query_values = {token.upper() for token in _tokens(query)}
    score += 2.0 * len(query_values.intersection({value.upper() for value in unit.dids}))
    score += 1.5 * len(query_values.intersection({value.upper() for value in unit.service_ids}))
    score += 1.0 * len(query_values.intersection({value.upper() for value in unit.sessions}))
    score += 1.0 * len(query_values.intersection({value.upper() for value in unit.security_levels}))
    return score


def _to_retrieved_chunk(unit: KnowledgeUnit, score: float) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=unit.chunk_id,
        score=score,
        source="sparse",
        content=unit.content,
        metadata={
            "unit_type": unit.unit_type,
            "protocol": unit.protocol,
            "module": unit.module or "",
            "ecu": unit.ecu or "",
            "service_ids": unit.service_ids,
            "dids": unit.dids,
            "sessions": unit.sessions,
            "security_levels": unit.security_levels,
            "page_idx": unit.page_idx,
            "bbox": unit.bbox,
            "source_path": unit.source_path or "",
            "source_evidence_id": _source_evidence_id_from_chunk_id(unit.chunk_id),
        },
    )


def _pack_context(chunks: list[RetrievedChunk]) -> str:
    parts: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        metadata = chunk.metadata
        source_path = metadata.get("source_path") or ""
        page_idx = metadata.get("page_idx")
        parts.append(
            "\n".join(
                [
                    f"[{index}] chunk_id={chunk.chunk_id} source={chunk.source} score={chunk.score}",
                    f"source={source_path} page_idx={page_idx}",
                    chunk.content,
                ]
            )
        )
    return "\n\n".join(parts)


def _source_evidence_id_from_chunk_id(chunk_id: str) -> str:
    if chunk_id.startswith("md_"):
        return chunk_id.removeprefix("md_")
    return ""


def _searchable_text(unit: KnowledgeUnit) -> str:
    metadata_text = " ".join(
        [
            unit.protocol,
            unit.module or "",
            unit.ecu or "",
            " ".join(unit.service_ids),
            " ".join(unit.dids),
            " ".join(unit.sessions),
            " ".join(unit.security_levels),
        ]
    )
    return f"{unit.content} {metadata_text}"


def _tokens(value: str) -> list[str]:
    return [_normalize_token(match.group(0)) for match in TOKEN_RE.finditer(value) if _normalize_token(match.group(0))]


def _normalize_token(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("0x"):
        value = value[2:]
    return value


def _normalize_text(value: str) -> str:
    return " ".join(_tokens(value))


def _overlaps(left: list[str], right: list[str]) -> bool:
    left_values = {value.upper().removeprefix("0X") for value in left}
    right_values = {value.upper().removeprefix("0X") for value in right}
    return bool(left_values.intersection(right_values))


def _merge_hybrid_chunks(
    *,
    sparse_chunks: list[RetrievedChunk],
    dense_chunks: list[RetrievedChunk],
    sparse_weight: float,
    dense_weight: float,
    top_k: int,
) -> list[RetrievedChunk]:
    if not sparse_chunks:
        return dense_chunks[:top_k]
    if not dense_chunks:
        return sparse_chunks[:top_k]

    sparse_scores = _normalized_scores(sparse_chunks)
    dense_scores = _normalized_scores(dense_chunks)
    by_id: dict[str, RetrievedChunk] = {}
    weighted_scores: dict[str, float] = {}
    sources: dict[str, list[str]] = {}

    for chunk in sparse_chunks:
        by_id.setdefault(chunk.chunk_id, chunk)
        sources.setdefault(chunk.chunk_id, []).append("sparse")
        weighted_scores[chunk.chunk_id] = weighted_scores.get(chunk.chunk_id, 0.0) + sparse_weight * sparse_scores[chunk.chunk_id]
    for chunk in dense_chunks:
        by_id.setdefault(chunk.chunk_id, chunk)
        sources.setdefault(chunk.chunk_id, []).append("dense")
        weighted_scores[chunk.chunk_id] = weighted_scores.get(chunk.chunk_id, 0.0) + dense_weight * dense_scores[chunk.chunk_id]

    merged: list[RetrievedChunk] = []
    for chunk_id, score in sorted(weighted_scores.items(), key=lambda item: (-item[1], item[0])):
        base = by_id[chunk_id]
        source_parts = sorted(set(sources.get(chunk_id) or [base.source]))
        metadata = {
            **base.metadata,
            "hybrid_sources": source_parts,
            "hybrid_score": round(score, 6),
        }
        merged.append(
            base.model_copy(
                update={
                    "score": round(score, 6),
                    "source": "hybrid" if len(source_parts) > 1 else source_parts[0],
                    "metadata": metadata,
                }
            )
        )
    return merged[:top_k]


def _normalized_scores(chunks: list[RetrievedChunk]) -> dict[str, float]:
    if not chunks:
        return {}
    values = [chunk.score for chunk in chunks]
    min_score = min(values)
    max_score = max(values)
    if max_score == min_score:
        return {chunk.chunk_id: 1.0 for chunk in chunks}
    return {chunk.chunk_id: (chunk.score - min_score) / (max_score - min_score) for chunk in chunks}


def _trace_chunk(chunk: RetrievedChunk) -> dict:
    return {
        "chunk_id": chunk.chunk_id,
        "source": chunk.source,
        "score": chunk.score,
        "metadata": chunk.metadata,
    }
