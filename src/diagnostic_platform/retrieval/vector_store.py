"""Milvus vector-store adapter and in-memory test implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Protocol

from diagnostic_platform.schemas import RetrievalFilters


VECTOR_FIELD = "embedding"
VECTOR_OUTPUT_FIELDS = [
    "source_id",
    "record_type",
    "content",
    "unit_type",
    "evidence_type",
    "doc_id",
    "protocol",
    "module",
    "ecu",
    "service_ids",
    "dids",
    "sessions",
    "security_levels",
    "source_path",
    "page_idx",
    "bbox",
    "source_evidence_id",
]


@dataclass
class VectorRecord:
    """One traceable vector document."""

    primary_id: str
    source_id: str
    record_type: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] = field(default_factory=list)


@dataclass
class VectorSearchResult:
    """One vector search result."""

    record: VectorRecord
    score: float


class VectorStore(Protocol):
    """Minimal vector store interface used by indexing and retrieval."""

    def ensure_collection(self, *, dimension: int, metric_type: str, drop_existing: bool = False) -> None: ...

    def upsert(self, records: list[VectorRecord]) -> None: ...

    def flush(self) -> None: ...

    def describe_collection(self) -> dict[str, Any]: ...

    def search(
        self,
        query_vector: list[float],
        *,
        top_k: int,
        filters: RetrievalFilters | None = None,
        overfetch: int = 5,
    ) -> list[VectorSearchResult]: ...


class MilvusVectorStore:
    """Milvus-backed vector store using pymilvus MilvusClient."""

    def __init__(self, *, uri: str, collection_name: str, timeout: float | None = None) -> None:
        try:
            from pymilvus import DataType, MilvusClient
        except ImportError as exc:
            raise RuntimeError(
                "pymilvus is required for Milvus vector indexing/retrieval. "
                "Install the vector extras or use InMemoryVectorStore in tests."
            ) from exc

        self.uri = uri
        self.collection_name = collection_name
        self._DataType = DataType
        self.client = MilvusClient(uri=uri, timeout=timeout)
        self.metric_type = "COSINE"

    def ensure_collection(self, *, dimension: int, metric_type: str, drop_existing: bool = False) -> None:
        self.metric_type = metric_type.upper()
        if self.client.has_collection(self.collection_name):
            if drop_existing:
                self.client.drop_collection(self.collection_name)
            else:
                return

        schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", self._DataType.VARCHAR, is_primary=True, max_length=512)
        schema.add_field(VECTOR_FIELD, self._DataType.FLOAT_VECTOR, dim=dimension)
        for field_name in VECTOR_OUTPUT_FIELDS:
            if field_name == "page_idx":
                schema.add_field(field_name, self._DataType.INT64)
            else:
                schema.add_field(field_name, self._DataType.VARCHAR, max_length=65535 if field_name == "content" else 2048)

        index_params = self.client.prepare_index_params()
        index_params.add_index(
            field_name=VECTOR_FIELD,
            index_type="AUTOINDEX",
            metric_type=self.metric_type,
        )
        self.client.create_collection(
            collection_name=self.collection_name,
            schema=schema,
            index_params=index_params,
        )

    def upsert(self, records: list[VectorRecord]) -> None:
        if not records:
            return
        rows = [_record_to_milvus_row(record) for record in records]
        if hasattr(self.client, "upsert"):
            self.client.upsert(collection_name=self.collection_name, data=rows)
        else:
            self.client.insert(collection_name=self.collection_name, data=rows)

    def flush(self) -> None:
        if hasattr(self.client, "flush"):
            self.client.flush(collection_name=self.collection_name)

    def describe_collection(self) -> dict[str, Any]:
        description: dict[str, Any] = {
            "collection_name": self.collection_name,
            "exists": bool(self.client.has_collection(self.collection_name)),
            "metric_type": self.metric_type,
        }
        if not description["exists"]:
            return description
        try:
            payload = self.client.describe_collection(collection_name=self.collection_name)
            description["raw_description"] = payload
            description["dimension"] = _dimension_from_description(payload)
            description["metric_type"] = _metric_from_description(payload) or self.metric_type
        except Exception as exc:  # noqa: BLE001 - validation should report rather than mask client differences.
            description["describe_error"] = str(exc)
        try:
            stats = self.client.get_collection_stats(collection_name=self.collection_name)
            description["row_count"] = int(stats.get("row_count") or stats.get("num_entities") or 0)
        except Exception as exc:  # noqa: BLE001
            description["stats_error"] = str(exc)
        return description

    def search(
        self,
        query_vector: list[float],
        *,
        top_k: int,
        filters: RetrievalFilters | None = None,
        overfetch: int = 5,
    ) -> list[VectorSearchResult]:
        limit = max(top_k, 1) * max(overfetch, 1)
        response = self.client.search(
            collection_name=self.collection_name,
            data=[query_vector],
            anns_field=VECTOR_FIELD,
            limit=limit,
            filter=_milvus_scalar_filter(filters),
            output_fields=VECTOR_OUTPUT_FIELDS,
            search_params={"metric_type": self.metric_type, "params": {}},
        )
        hits = response[0] if response else []
        results: list[VectorSearchResult] = []
        seen_source_ids: set[str] = set()
        for hit in hits:
            entity = _hit_entity(hit)
            record = _record_from_entity(entity)
            if not _record_matches_filters(record, filters):
                continue
            if record.source_id in seen_source_ids:
                continue
            seen_source_ids.add(record.source_id)
            results.append(VectorSearchResult(record=record, score=_hit_score(hit)))
            if len(results) >= top_k:
                break
        return results


class InMemoryVectorStore:
    """Deterministic in-memory vector store used by tests."""

    def __init__(self, *, collection_name: str = "memory") -> None:
        self.collection_name = collection_name
        self.records: dict[str, VectorRecord] = {}
        self.dimension = 0
        self.metric_type = "COSINE"

    def ensure_collection(self, *, dimension: int, metric_type: str, drop_existing: bool = False) -> None:
        self.dimension = dimension
        self.metric_type = metric_type.upper()
        if drop_existing:
            self.records = {}

    def upsert(self, records: list[VectorRecord]) -> None:
        for record in records:
            self.records[record.primary_id] = record

    def flush(self) -> None:
        return None

    def describe_collection(self) -> dict[str, Any]:
        return {
            "collection_name": self.collection_name,
            "exists": True,
            "dimension": self.dimension,
            "metric_type": self.metric_type,
            "row_count": len(self.records),
        }

    def search(
        self,
        query_vector: list[float],
        *,
        top_k: int,
        filters: RetrievalFilters | None = None,
        overfetch: int = 5,
    ) -> list[VectorSearchResult]:
        scored: list[VectorSearchResult] = []
        for record in self.records.values():
            if not _record_matches_filters(record, filters):
                continue
            scored.append(VectorSearchResult(record=record, score=_similarity(query_vector, record.embedding, self.metric_type)))
        scored.sort(key=lambda item: (-item.score, item.record.primary_id))
        results: list[VectorSearchResult] = []
        seen_source_ids: set[str] = set()
        for item in scored:
            if item.record.source_id in seen_source_ids:
                continue
            seen_source_ids.add(item.record.source_id)
            results.append(item)
            if len(results) >= top_k:
                break
        return results


def _record_to_milvus_row(record: VectorRecord) -> dict[str, Any]:
    metadata = record.metadata
    return {
        "id": record.primary_id,
        VECTOR_FIELD: record.embedding,
        "source_id": record.source_id,
        "record_type": record.record_type,
        "content": record.content,
        "unit_type": str(metadata.get("unit_type") or ""),
        "evidence_type": str(metadata.get("evidence_type") or ""),
        "doc_id": str(metadata.get("doc_id") or ""),
        "protocol": str(metadata.get("protocol") or ""),
        "module": str(metadata.get("module") or ""),
        "ecu": str(metadata.get("ecu") or ""),
        "service_ids": _join_values(metadata.get("service_ids")),
        "dids": _join_values(metadata.get("dids")),
        "sessions": _join_values(metadata.get("sessions")),
        "security_levels": _join_values(metadata.get("security_levels")),
        "source_path": str(metadata.get("source_path") or ""),
        "page_idx": int(metadata.get("page_idx") or 0),
        "bbox": _join_values(metadata.get("bbox")),
        "source_evidence_id": str(metadata.get("source_evidence_id") or ""),
    }


def _dimension_from_description(payload: Any) -> int:
    fields = []
    if isinstance(payload, dict):
        fields = list(payload.get("fields") or payload.get("schema", {}).get("fields") or [])
    for field in fields:
        if isinstance(field, dict) and field.get("name") == VECTOR_FIELD:
            params = field.get("params") or field.get("type_params") or {}
            try:
                return int(params.get("dim") or field.get("dim") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _metric_from_description(payload: Any) -> str:
    indexes = []
    if isinstance(payload, dict):
        indexes = list(payload.get("indexes") or payload.get("index_descriptions") or [])
    for index in indexes:
        if not isinstance(index, dict):
            continue
        params = index.get("params") or index.get("index_param") or {}
        metric = params.get("metric_type") or index.get("metric_type")
        if metric:
            return str(metric).upper()
    return ""


def _record_from_entity(entity: dict[str, Any]) -> VectorRecord:
    metadata = {
        "unit_type": entity.get("unit_type") or "",
        "evidence_type": entity.get("evidence_type") or "",
        "doc_id": entity.get("doc_id") or "",
        "protocol": entity.get("protocol") or "",
        "module": entity.get("module") or "",
        "ecu": entity.get("ecu") or "",
        "service_ids": _split_values(entity.get("service_ids")),
        "dids": _split_values(entity.get("dids")),
        "sessions": _split_values(entity.get("sessions")),
        "security_levels": _split_values(entity.get("security_levels")),
        "source_path": entity.get("source_path") or "",
        "page_idx": int(entity.get("page_idx") or 0),
        "bbox": _split_values(entity.get("bbox")),
        "source_evidence_id": entity.get("source_evidence_id") or "",
    }
    source_id = str(entity.get("source_id") or entity.get("id") or "")
    record_type = str(entity.get("record_type") or "vector")
    return VectorRecord(
        primary_id=str(entity.get("id") or source_id),
        source_id=source_id,
        record_type=record_type,
        content=str(entity.get("content") or ""),
        metadata=metadata,
    )


def _record_matches_filters(record: VectorRecord, filters: RetrievalFilters | None) -> bool:
    if filters is None:
        return True
    metadata = record.metadata
    if filters.doc_ids and str(metadata.get("doc_id") or "") not in set(filters.doc_ids):
        return False
    if filters.unit_types and str(metadata.get("unit_type") or "") not in set(filters.unit_types):
        return False
    if filters.protocol and str(metadata.get("protocol") or "").lower() != filters.protocol.lower():
        return False
    if filters.module and str(metadata.get("module") or "").lower() != filters.module.lower():
        return False
    if filters.ecu and str(metadata.get("ecu") or "").lower() != filters.ecu.lower():
        return False
    if filters.service_ids and not _overlaps(_split_values(metadata.get("service_ids")), filters.service_ids):
        return False
    if filters.dids and not _overlaps(_split_values(metadata.get("dids")), filters.dids):
        return False
    if filters.sessions and not _overlaps(_split_values(metadata.get("sessions")), filters.sessions):
        return False
    if filters.security_levels and not _overlaps(_split_values(metadata.get("security_levels")), filters.security_levels):
        return False
    return True


def _milvus_scalar_filter(filters: RetrievalFilters | None) -> str:
    if filters is None:
        return ""
    clauses: list[str] = []
    if filters.doc_ids:
        clauses.append(_in_clause("doc_id", filters.doc_ids))
    if filters.unit_types:
        clauses.append(_in_clause("unit_type", [str(value) for value in filters.unit_types]))
    if filters.protocol:
        clauses.append(f'protocol == "{_escape(filters.protocol)}"')
    if filters.module:
        clauses.append(f'module == "{_escape(filters.module)}"')
    if filters.ecu:
        clauses.append(f'ecu == "{_escape(filters.ecu)}"')
    return " and ".join(clause for clause in clauses if clause)


def _in_clause(field_name: str, values: list[str]) -> str:
    encoded = ", ".join(f'"{_escape(value)}"' for value in values)
    return f"{field_name} in [{encoded}]"


def _hit_entity(hit: Any) -> dict[str, Any]:
    if isinstance(hit, dict):
        entity = hit.get("entity") or hit
        if "id" not in entity and "id" in hit:
            entity = {**entity, "id": hit["id"]}
        return dict(entity)
    entity = getattr(hit, "entity", None) or {}
    if isinstance(entity, dict):
        payload = dict(entity)
    else:
        payload = {field: getattr(entity, field, "") for field in VECTOR_OUTPUT_FIELDS}
    hit_id = getattr(hit, "id", None)
    if hit_id is not None and "id" not in payload:
        payload["id"] = hit_id
    return payload


def _hit_score(hit: Any) -> float:
    if isinstance(hit, dict):
        value = hit.get("score", hit.get("distance", 0.0))
    else:
        value = getattr(hit, "score", getattr(hit, "distance", 0.0))
    return round(float(value or 0.0), 6)


def _similarity(left: list[float], right: list[float], metric_type: str) -> float:
    if metric_type.upper() in {"L2", "EUCLIDEAN"}:
        distance = math.sqrt(sum((a - b) * (a - b) for a, b in zip(left, right)))
        return round(1.0 / (1.0 + distance), 6)
    if metric_type.upper() in {"IP", "INNER_PRODUCT"}:
        return round(sum(a * b for a, b in zip(left, right)), 6)
    return round(_cosine(left, right), 6)


def _cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return numerator / (left_norm * right_norm)


def _join_values(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return " ".join(str(item) for item in value if str(item))
    return str(value)


def _split_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return [item for item in str(value).split() if item]


def _overlaps(left: list[str], right: list[str]) -> bool:
    left_values = {_normalize_filter_value(value) for value in left}
    right_values = {_normalize_filter_value(value) for value in right}
    return bool(left_values.intersection(right_values))


def _normalize_filter_value(value: str) -> str:
    return str(value).upper().removeprefix("0X")


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')
