"""Build local JSONL indexes from MinerU outputs."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
from typing import Any

from diagnostic_platform.graph.diagnostic_graph import build_diagnostic_graph
from diagnostic_platform.graph.full_kg import (
    KG_EDGES_FILE,
    KG_MANIFEST_FILE,
    KG_NODES_FILE,
    KG_PROVENANCE_FILE,
    build_full_knowledge_graph,
    write_knowledge_graph_snapshot,
)
from diagnostic_platform.ingestion.mineru_client import load_mineru_output
from diagnostic_platform.normalizer.evidence import evidence_from_mineru
from diagnostic_platform.normalizer.mineru import normalize_request
from diagnostic_platform.normalizer.semantic_chunk import (
    evidence_units_from_semantic_chunks,
    knowledge_units_from_semantic_chunks,
    semantic_chunks_from_evidence_units,
    semantic_chunks_from_mineru,
)
from diagnostic_platform.retrieval.graph_rag import load_markdown_graph_evidence_units
from diagnostic_platform.schemas import (
    BuildDiagnosticGraphRequest,
    DiagnosticGraph,
    DocumentMetadata,
    EvidenceUnit,
    FlowStepPlan,
    KnowledgeUnit,
    KnowledgeGraphSnapshot,
    LoadMinerUOutputRequest,
    LocalIndexBuildRequest,
    LocalIndexBuildResult,
)


DOCUMENTS_FILE = "documents.jsonl"
KNOWLEDGE_UNITS_FILE = "knowledge_units.jsonl"
EVIDENCE_UNITS_FILE = "evidence_units.jsonl"
GRAPH_FILE = "diagnostic_graph.json"
SUMMARY_FILE = "summary.json"


def build_local_index(request: LocalIndexBuildRequest) -> LocalIndexBuildResult:
    """Build a deterministic local corpus from existing MinerU output directories."""

    mineru_output_dir = Path(request.mineru_output_dir)
    if not mineru_output_dir.exists():
        raise FileNotFoundError(f"MinerU output directory does not exist: {mineru_output_dir}")

    content_list_paths = sorted(mineru_output_dir.rglob("*_content_list.json"))
    if not content_list_paths:
        raise FileNotFoundError(f"No *_content_list.json files found under: {mineru_output_dir}")

    index_dir = Path(request.index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)

    documents: list[DocumentMetadata] = []
    knowledge_units: list[KnowledgeUnit] = []
    evidence_units: list[EvidenceUnit] = []
    semantic_chunk_count = 0
    block_count = 0

    for content_list_path in content_list_paths:
        metadata = _metadata_from_content_list(content_list_path, request)
        normalized = load_mineru_output(
            LoadMinerUOutputRequest(
                output_dir=str(content_list_path.parent),
                metadata=metadata,
                content_list_paths=[str(content_list_path)],
                include_markdown_fallback=False,
            )
        )
        documents.append(metadata)
        block_count += len(normalized.blocks)
        if request.chunking_mode == "semantic":
            semantic_chunks = semantic_chunks_from_mineru(normalized)
            semantic_chunk_count += len(semantic_chunks)
            knowledge_units.extend(knowledge_units_from_semantic_chunks(semantic_chunks))
            evidence_units.extend(evidence_units_from_semantic_chunks(semantic_chunks))
        else:
            knowledge_units.extend(normalize_request(normalized))
            evidence_units.extend(evidence_from_mineru(normalized))

    markdown_evidence_units = load_markdown_graph_evidence_units(mineru_output_dir)
    if request.chunking_mode == "semantic":
        markdown_chunks = semantic_chunks_from_evidence_units(markdown_evidence_units)
        semantic_chunk_count += len(markdown_chunks)
        knowledge_units.extend(knowledge_units_from_semantic_chunks(markdown_chunks))
        evidence_units.extend(evidence_units_from_semantic_chunks(markdown_chunks))
    else:
        evidence_units.extend(markdown_evidence_units)
    evidence_units = _dedupe_evidence_units(evidence_units)
    graph = _build_graph(mineru_output_dir, evidence_units) if request.include_graph else DiagnosticGraph()
    kg_snapshot = (
        build_full_knowledge_graph(
            evidence_units=evidence_units,
            collection_name=request.collection_name,
            source=str(mineru_output_dir),
        )
        if request.include_graph
        else KnowledgeGraphSnapshot(manifest={"collection_name": request.collection_name})
    )

    files = {
        "documents": str(index_dir / DOCUMENTS_FILE),
        "knowledge_units": str(index_dir / KNOWLEDGE_UNITS_FILE),
        "evidence_units": str(index_dir / EVIDENCE_UNITS_FILE),
        "summary": str(index_dir / SUMMARY_FILE),
    }
    if request.include_graph:
        files["graph"] = str(index_dir / GRAPH_FILE)
        files["kg_manifest"] = str(index_dir / KG_MANIFEST_FILE)
        files["kg_nodes"] = str(index_dir / KG_NODES_FILE)
        files["kg_edges"] = str(index_dir / KG_EDGES_FILE)
        files["kg_provenance"] = str(index_dir / KG_PROVENANCE_FILE)

    summary = _build_summary(
        request=request,
        documents=documents,
        knowledge_units=knowledge_units,
        evidence_units=evidence_units,
        graph=graph,
        kg_snapshot=kg_snapshot,
        block_count=block_count,
        semantic_chunk_count=semantic_chunk_count,
    )

    _write_jsonl(index_dir / DOCUMENTS_FILE, documents)
    _write_jsonl(index_dir / KNOWLEDGE_UNITS_FILE, knowledge_units)
    _write_jsonl(index_dir / EVIDENCE_UNITS_FILE, evidence_units)
    if request.include_graph:
        _write_json(index_dir / GRAPH_FILE, graph.model_dump(mode="json"))
        files.update(write_knowledge_graph_snapshot(kg_snapshot, index_dir))
    _write_json(index_dir / SUMMARY_FILE, summary)

    return LocalIndexBuildResult(
        collection_name=request.collection_name,
        index_dir=str(index_dir),
        document_count=len(documents),
        knowledge_unit_count=len(knowledge_units),
        evidence_unit_count=len(evidence_units),
        graph_entity_count=len(graph.entities),
        graph_relation_count=len(graph.relations),
        kg_node_count=len(kg_snapshot.nodes),
        kg_edge_count=len(kg_snapshot.edges),
        kg_provenance_count=len(kg_snapshot.provenance),
        files=files,
        summary=summary,
    )


def _metadata_from_content_list(
    content_list_path: Path,
    request: LocalIndexBuildRequest,
) -> DocumentMetadata:
    doc_name = content_list_path.parent.name
    return DocumentMetadata(
        doc_id=_safe_doc_id(doc_name),
        doc_type=request.doc_type,
        protocol=request.protocol,
        module=_infer_module(doc_name),
        ecu=_infer_ecu(doc_name),
        source=request.source,
        source_path=str(content_list_path),
    )


def _build_graph(mineru_output_dir: Path, evidence_units: list[EvidenceUnit]) -> DiagnosticGraph:
    return build_diagnostic_graph(
        BuildDiagnosticGraphRequest(
            flow_plan=FlowStepPlan(source_path=str(mineru_output_dir), steps=[]),
            evidence_units=evidence_units,
        )
    )


def _build_summary(
    request: LocalIndexBuildRequest,
    documents: list[DocumentMetadata],
    knowledge_units: list[KnowledgeUnit],
    evidence_units: list[EvidenceUnit],
    graph: DiagnosticGraph,
    kg_snapshot: KnowledgeGraphSnapshot,
    block_count: int,
    semantic_chunk_count: int,
) -> dict[str, int | str | list[str] | dict[str, int]]:
    unit_types = Counter(unit.unit_type for unit in knowledge_units)
    evidence_types = Counter(unit.evidence_type for unit in evidence_units)
    ecus = Counter(unit.ecu or "" for unit in knowledge_units if unit.ecu)
    modules = Counter(unit.module or "" for unit in knowledge_units if unit.module)
    service_ids = _counter_from_lists(unit.service_ids for unit in knowledge_units)
    dids = _counter_from_lists(unit.dids for unit in knowledge_units)
    sessions = _counter_from_lists(unit.sessions for unit in knowledge_units)
    security_levels = _counter_from_lists(unit.security_levels for unit in knowledge_units)

    return {
        "collection_name": request.collection_name,
        "mineru_output_dir": request.mineru_output_dir,
        "chunking_mode": request.chunking_mode,
        "document_count": len(documents),
        "block_count": block_count,
        "semantic_chunk_count": semantic_chunk_count,
        "knowledge_unit_count": len(knowledge_units),
        "evidence_unit_count": len(evidence_units),
        "graph_entity_count": len(graph.entities),
        "graph_relation_count": len(graph.relations),
        "kg_node_count": len(kg_snapshot.nodes),
        "kg_edge_count": len(kg_snapshot.edges),
        "kg_provenance_count": len(kg_snapshot.provenance),
        "unit_types": dict(sorted(unit_types.items())),
        "evidence_types": dict(sorted(evidence_types.items())),
        "ecus": dict(sorted(ecus.items())),
        "modules": dict(sorted(modules.items())),
        "top_service_ids": _top_values(service_ids),
        "top_dids": _top_values(dids),
        "sessions": dict(sorted(sessions.items())),
        "security_levels": dict(sorted(security_levels.items())),
    }


def _counter_from_lists(values: Any) -> Counter[str]:
    counter: Counter[str] = Counter()
    for items in values:
        for item in items:
            counter[str(item)] += 1
    return counter


def _top_values(counter: Counter[str], limit: int = 20) -> list[str]:
    return [key for key, _count in counter.most_common(limit)]


def _dedupe_evidence_units(evidence_units: list[EvidenceUnit]) -> list[EvidenceUnit]:
    deduped: dict[str, EvidenceUnit] = {}
    for unit in evidence_units:
        deduped.setdefault(unit.evidence_id, unit)
    return list(deduped.values())


def _safe_doc_id(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z_.-]+", "_", value.strip())
    value = re.sub(r"_+", "_", value)
    return value.strip("_") or "document"


def _infer_module(doc_name: str) -> str:
    normalized = doc_name.lower().replace("_", " ")
    if "process-test sequence" in normalized or "process test sequence" in normalized:
        return "eol_process"
    if "general eol test" in normalized:
        return "general_eol_test"
    if "parameter" in normalized:
        return "ecu_parameter"
    return "treg"


def _infer_ecu(doc_name: str) -> str | None:
    normalized = doc_name.replace(" ", "_")
    match = re.search(r"TREG[-_](.+?)[-_]Parameter", normalized, re.IGNORECASE)
    if not match:
        return None
    ecu = match.group(1).strip("_- ")
    return ecu or None


def _write_jsonl(path: Path, models: list[Any]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for model in models:
            file.write(json.dumps(model.model_dump(mode="json"), ensure_ascii=False))
            file.write("\n")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
