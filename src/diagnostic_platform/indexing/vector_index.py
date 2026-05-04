"""Build Milvus vector indexes from local JSONL corpus files."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Callable

from diagnostic_platform.indexing.local_builder import EVIDENCE_UNITS_FILE, KNOWLEDGE_UNITS_FILE, SUMMARY_FILE
from diagnostic_platform.retrieval.embedding import EmbeddingClient, build_embedding_client
from diagnostic_platform.retrieval.vector_store import MilvusVectorStore, VectorRecord, VectorStore
from diagnostic_platform.schemas import (
    EvidenceUnit,
    KnowledgeUnit,
    VectorIndexBuildRequest,
    VectorIndexBuildResult,
    VectorIndexManifest,
)


VECTOR_MANIFEST_FILE = "vector_manifest.json"
UPSERT_BATCH_SIZE = 128
VectorIndexProgressCallback = Callable[[dict[str, Any]], None]


def build_vector_index(
    request: VectorIndexBuildRequest,
    *,
    embedding_client: EmbeddingClient | None = None,
    vector_store: VectorStore | None = None,
    progress_callback: VectorIndexProgressCallback | None = None,
    milvus_timeout_seconds: float | None = None,
) -> VectorIndexBuildResult:
    """Embed local JSONL corpus records and write them into a Milvus collection."""

    started = time.monotonic()
    index_dir = Path(request.index_dir)
    records = _load_vector_records(index_dir, request)
    if not records:
        raise ValueError("No records selected for vector indexing.")
    _emit_progress(
        progress_callback,
        "vector_index_records_loaded",
        elapsed_seconds=time.monotonic() - started,
        index_dir=str(index_dir),
        record_count=len(records),
    )

    embedder = embedding_client or build_embedding_client(request.embedding_model)
    collection_name = request.collection_name or _default_collection_name(index_dir)
    manifest_path = Path(request.manifest_path) if request.manifest_path else index_dir / VECTOR_MANIFEST_FILE

    embedded_records: list[VectorRecord] = []
    total_embed_batches = _batch_count(len(records), request.batch_size)
    for batch_index, batch in enumerate(_batched(records, request.batch_size), start=1):
        batch_started = time.monotonic()
        _emit_progress(
            progress_callback,
            "vector_index_embed_batch_start",
            elapsed_seconds=batch_started - started,
            batch_index=batch_index,
            total_batches=total_embed_batches,
            batch_size=len(batch),
            embedded_count=len(embedded_records),
            total_records=len(records),
        )
        vectors = embedder.embed_texts([record.content for record in batch])
        if len(vectors) != len(batch):
            raise ValueError("Embedding client returned a different number of vectors than input texts.")
        for record, vector in zip(batch, vectors):
            embedded_records.append(record.__class__(**{**record.__dict__, "embedding": vector}))
        _emit_progress(
            progress_callback,
            "vector_index_embed_batch_done",
            elapsed_seconds=time.monotonic() - started,
            batch_index=batch_index,
            total_batches=total_embed_batches,
            batch_size=len(batch),
            embedded_count=len(embedded_records),
            total_records=len(records),
            batch_duration_seconds=time.monotonic() - batch_started,
        )

    dimension = len(embedded_records[0].embedding)
    if dimension <= 0:
        raise ValueError("Embedding dimension must be greater than zero.")
    _emit_progress(
        progress_callback,
        "vector_index_milvus_connect_start",
        elapsed_seconds=time.monotonic() - started,
        milvus_uri=request.milvus_uri,
        collection_name=collection_name,
        timeout_seconds=milvus_timeout_seconds,
    )
    store = vector_store or MilvusVectorStore(
        uri=request.milvus_uri,
        collection_name=collection_name,
        timeout=milvus_timeout_seconds,
    )
    _emit_progress(
        progress_callback,
        "vector_index_collection_start",
        elapsed_seconds=time.monotonic() - started,
        collection_name=collection_name,
        dimension=dimension,
        metric_type=request.metric_type.upper(),
        drop_existing=request.drop_existing,
    )
    store.ensure_collection(dimension=dimension, metric_type=request.metric_type, drop_existing=request.drop_existing)
    upsert_batch_size = max(request.batch_size, UPSERT_BATCH_SIZE)
    total_upsert_batches = _batch_count(len(embedded_records), upsert_batch_size)
    upserted_count = 0
    for batch_index, batch in enumerate(_batched(embedded_records, upsert_batch_size), start=1):
        store.upsert(batch)
        upserted_count += len(batch)
        _emit_progress(
            progress_callback,
            "vector_index_upsert_batch_done",
            elapsed_seconds=time.monotonic() - started,
            batch_index=batch_index,
            total_batches=total_upsert_batches,
            batch_size=len(batch),
            upserted_count=upserted_count,
            total_records=len(embedded_records),
        )
    _emit_progress(
        progress_callback,
        "vector_index_flush_start",
        elapsed_seconds=time.monotonic() - started,
        upserted_count=upserted_count,
    )
    store.flush()

    source_files = _source_files(index_dir, request)
    source_hashes = {name: _sha256(Path(path)) for name, path in source_files.items() if Path(path).exists()}
    knowledge_count = sum(1 for record in records if record.record_type == "knowledge_unit")
    evidence_count = sum(1 for record in records if record.record_type == "evidence_unit")
    manifest = VectorIndexManifest(
        index_dir=str(index_dir),
        collection_name=collection_name,
        milvus_uri=request.milvus_uri,
        embedding_model=embedder.model_name,
        embedding_dimension=dimension,
        metric_type=request.metric_type.upper(),
        vector_count=len(embedded_records),
        knowledge_unit_count=knowledge_count,
        evidence_unit_count=evidence_count,
        source_files=source_files,
        source_hashes=source_hashes,
        built_at=datetime.now(timezone.utc).isoformat(),
    )
    validation = validate_vector_index_manifest(manifest, vector_store=store)
    if not validation["valid"]:
        raise RuntimeError(f"Vector index validation failed: {validation['errors']}")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = VectorIndexBuildResult(
        collection_name=collection_name,
        milvus_uri=request.milvus_uri,
        embedding_model=embedder.model_name,
        embedding_dimension=dimension,
        metric_type=request.metric_type.upper(),
        vector_count=len(embedded_records),
        knowledge_unit_count=knowledge_count,
        evidence_unit_count=evidence_count,
        manifest_path=str(manifest_path),
        source_hashes=source_hashes,
        validation=validation,
    )
    _emit_progress(
        progress_callback,
        "vector_index_done",
        elapsed_seconds=time.monotonic() - started,
        collection_name=collection_name,
        vector_count=result.vector_count,
        manifest_path=str(manifest_path),
    )
    return result


def load_vector_manifest(path: str | Path) -> VectorIndexManifest:
    """Load a vector index manifest."""

    return VectorIndexManifest.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def validate_vector_index_manifest(
    manifest: VectorIndexManifest,
    *,
    vector_store: VectorStore | None = None,
) -> dict[str, Any]:
    """Validate source hashes/counts and optional vector collection metadata."""

    checks: dict[str, Any] = {
        "source_hashes": {},
        "source_counts": {},
        "collection": {},
    }
    errors: list[str] = []

    source_files = manifest.source_files or {}
    for name, path_value in source_files.items():
        path = Path(path_value)
        expected_hash = manifest.source_hashes.get(name)
        if not path.exists():
            errors.append(f"source_missing:{name}:{path}")
            checks["source_hashes"][name] = {"exists": False, "expected": expected_hash, "actual": ""}
            continue
        actual_hash = _sha256(path)
        checks["source_hashes"][name] = {"exists": True, "expected": expected_hash, "actual": actual_hash}
        if expected_hash and expected_hash != actual_hash:
            errors.append(f"source_hash_mismatch:{name}")
        if path.suffix == ".jsonl":
            checks["source_counts"][name] = _jsonl_count(path)

    expected_knowledge = checks["source_counts"].get("knowledge_units")
    expected_evidence = checks["source_counts"].get("evidence_units")
    if expected_knowledge is not None and int(expected_knowledge) != manifest.knowledge_unit_count:
        errors.append("knowledge_unit_count_mismatch")
    if expected_evidence is not None and int(expected_evidence) != manifest.evidence_unit_count:
        errors.append("evidence_unit_count_mismatch")
    if int(manifest.knowledge_unit_count + manifest.evidence_unit_count) != int(manifest.vector_count):
        errors.append("manifest_vector_count_mismatch")

    if vector_store is not None and hasattr(vector_store, "describe_collection"):
        collection = vector_store.describe_collection()
        checks["collection"] = collection
        if not collection.get("exists", False):
            errors.append("collection_missing")
        dimension = int(collection.get("dimension") or 0)
        if dimension and dimension != manifest.embedding_dimension:
            errors.append("collection_dimension_mismatch")
        metric = str(collection.get("metric_type") or "").upper()
        if metric and metric != manifest.metric_type.upper():
            errors.append("collection_metric_mismatch")
        row_count = collection.get("row_count")
        if row_count is not None and int(row_count) < int(manifest.vector_count):
            errors.append("collection_row_count_below_manifest")

    return {
        "valid": not errors,
        "errors": errors,
        "checks": checks,
    }


def default_vector_manifest_path(index_dir: str | Path) -> Path:
    """Return the default vector manifest path for an index directory."""

    return Path(index_dir) / VECTOR_MANIFEST_FILE


def _load_vector_records(index_dir: Path, request: VectorIndexBuildRequest) -> list[VectorRecord]:
    records: list[VectorRecord] = []
    if request.include_knowledge_units:
        records.extend(_knowledge_records(index_dir / KNOWLEDGE_UNITS_FILE))
    if request.include_evidence_units:
        records.extend(_evidence_records(index_dir / EVIDENCE_UNITS_FILE))
    return records


def _knowledge_records(path: Path) -> list[VectorRecord]:
    if not path.exists():
        return []
    records: list[VectorRecord] = []
    for unit in _read_jsonl_models(path, KnowledgeUnit):
        records.append(
            VectorRecord(
                primary_id=f"ku:{unit.chunk_id}",
                source_id=unit.chunk_id,
                record_type="knowledge_unit",
                content=_record_content(unit.content, _knowledge_metadata(unit)),
                metadata=_knowledge_metadata(unit),
            )
        )
    return records


def _evidence_records(path: Path) -> list[VectorRecord]:
    if not path.exists():
        return []
    records: list[VectorRecord] = []
    for evidence in _read_jsonl_models(path, EvidenceUnit):
        records.append(
            VectorRecord(
                primary_id=f"ev:{evidence.evidence_id}",
                source_id=evidence.evidence_id,
                record_type="evidence_unit",
                content=_record_content(evidence.content, _evidence_metadata(evidence)),
                metadata=_evidence_metadata(evidence),
            )
        )
    return records


def _knowledge_metadata(unit: KnowledgeUnit) -> dict[str, Any]:
    source_evidence_id = unit.chunk_id.removeprefix("md_") if unit.chunk_id.startswith("md_") else ""
    return {
        "unit_type": unit.unit_type,
        "evidence_type": "",
        "doc_id": _doc_id_from_knowledge_unit(unit),
        "protocol": unit.protocol,
        "module": unit.module or "",
        "ecu": unit.ecu or "",
        "service_ids": unit.service_ids,
        "dids": unit.dids,
        "sessions": unit.sessions,
        "security_levels": unit.security_levels,
        "source_path": unit.source_path or "",
        "page_idx": unit.page_idx,
        "bbox": unit.bbox,
        "source_evidence_id": source_evidence_id,
    }


def _evidence_metadata(evidence: EvidenceUnit) -> dict[str, Any]:
    metadata = evidence.metadata
    return {
        "unit_type": "",
        "evidence_type": evidence.evidence_type,
        "doc_id": evidence.doc_id,
        "protocol": str(metadata.get("protocol") or ""),
        "module": str(metadata.get("module") or ""),
        "ecu": str(metadata.get("ecu") or ""),
        "service_ids": _list_metadata(metadata.get("service_ids")),
        "dids": _list_metadata(metadata.get("dids")),
        "sessions": _list_metadata(metadata.get("sessions")),
        "security_levels": _list_metadata(metadata.get("security_levels")),
        "source_path": evidence.source_path or "",
        "page_idx": evidence.page_idx,
        "bbox": evidence.bbox,
        "source_evidence_id": str(metadata.get("source_evidence_id") or ""),
    }


def _record_content(content: str, metadata: dict[str, Any]) -> str:
    metadata_text = " ".join(
        [
            str(metadata.get("protocol") or ""),
            str(metadata.get("module") or ""),
            str(metadata.get("ecu") or ""),
            " ".join(_list_metadata(metadata.get("service_ids"))),
            " ".join(_list_metadata(metadata.get("dids"))),
            " ".join(_list_metadata(metadata.get("sessions"))),
            " ".join(_list_metadata(metadata.get("security_levels"))),
        ]
    ).strip()
    return f"{content}\n{metadata_text}".strip()


def _source_files(index_dir: Path, request: VectorIndexBuildRequest) -> dict[str, str]:
    files: dict[str, str] = {}
    if request.include_knowledge_units:
        files["knowledge_units"] = str(index_dir / KNOWLEDGE_UNITS_FILE)
    if request.include_evidence_units:
        files["evidence_units"] = str(index_dir / EVIDENCE_UNITS_FILE)
    summary_path = index_dir / SUMMARY_FILE
    if summary_path.exists():
        files["summary"] = str(summary_path)
    return files


def _default_collection_name(index_dir: Path) -> str:
    collection_name = "default"
    summary_path = index_dir / SUMMARY_FILE
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        collection_name = str(summary.get("collection_name") or collection_name)
    return f"diagnostic_knowledge_{_safe_name(collection_name)}"


def _safe_name(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z_]+", "_", value.strip())
    value = value.strip("_")
    return value or "default"


def _doc_id_from_knowledge_unit(unit: KnowledgeUnit) -> str:
    source_path = Path(unit.source_path or "")
    if source_path.name:
        stem = source_path.stem
        if stem.endswith("_content_list"):
            return stem.removesuffix("_content_list")
        if stem.endswith("_middle"):
            return stem.removesuffix("_middle")
        return stem
    markers = ["_sem_", "_text_", "_table_", "_image_", "_equation_"]
    for marker in markers:
        if marker in unit.chunk_id:
            return unit.chunk_id.split(marker, 1)[0]
    return unit.chunk_id.split("_", 1)[0]


def _read_jsonl_models(path: Path, model_class) -> list[Any]:
    models = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            if line.strip():
                models.append(model_class.model_validate(json.loads(line)))
    return models


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jsonl_count(path: Path) -> int:
    with path.open("r", encoding="utf-8") as file:
        return sum(1 for line in file if line.strip())


def _batched(items: list[Any], batch_size: int):
    size = max(batch_size, 1)
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _batch_count(item_count: int, batch_size: int) -> int:
    size = max(batch_size, 1)
    return (max(item_count, 0) + size - 1) // size


def _emit_progress(callback: VectorIndexProgressCallback | None, event: str, **payload: Any) -> None:
    if not callback:
        return
    payload["event"] = event
    if "elapsed_seconds" in payload:
        payload["elapsed_seconds"] = round(float(payload["elapsed_seconds"]), 3)
    if "batch_duration_seconds" in payload:
        payload["batch_duration_seconds"] = round(float(payload["batch_duration_seconds"]), 3)
    callback(payload)


def _list_metadata(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    text = str(value)
    return [text] if text else []
