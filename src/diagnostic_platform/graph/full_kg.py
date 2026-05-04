"""Versioned local full knowledge graph snapshot and repository APIs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict, deque
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable

from diagnostic_platform.retrieval.graph_rag import (
    extract_parameter_fields,
    retrieve_task_evidence_candidates,
)
from diagnostic_platform.schemas import (
    EvidenceUnit,
    FlowNode,
    KnowledgeGraphEdge,
    KnowledgeGraphManifest,
    KnowledgeGraphNode,
    KnowledgeGraphProvenance,
    KnowledgeGraphSnapshot,
    ReferenceResolution,
    TaskEvidenceCandidate,
)


KG_MANIFEST_FILE = "kg_manifest.json"
KG_NODES_FILE = "kg_nodes.jsonl"
KG_EDGES_FILE = "kg_edges.jsonl"
KG_PROVENANCE_FILE = "kg_provenance.jsonl"
EVIDENCE_UNITS_FILE = "evidence_units.jsonl"
SCHEMA_VERSION = "kg.v1"

TOKEN_RE = re.compile(r"0x[0-9a-fA-F]+|[A-Za-z]+[A-Za-z0-9]*|[0-9a-fA-F]{2,8}")


class GraphRepository(ABC):
    """Storage-independent graph query interface used by retrieval/generation."""

    @abstractmethod
    def query_task_candidates(self, node: FlowNode, top_k: int) -> list[TaskEvidenceCandidate]:
        """Return KG-backed evidence candidates for one flow node."""

    @abstractmethod
    def resolve_references(self, reference_text: str, limit: int = 5) -> list[ReferenceResolution]:
        """Resolve a textual reference to flowchart/title/image KG nodes."""

    @abstractmethod
    def explain_path(self, source_id: str, target_id: str | None = None, max_depth: int = 2, max_paths: int = 8) -> list[str]:
        """Return readable KG paths."""


class LocalGraphRepository(GraphRepository):
    """Local JSONL-backed full-KG repository."""

    def __init__(self, snapshot: KnowledgeGraphSnapshot, evidence_units: list[EvidenceUnit]) -> None:
        self.snapshot = snapshot
        self.evidence_units = evidence_units
        self.nodes_by_id = {node.node_id: node for node in snapshot.nodes}
        self.edges_by_source: dict[str, list[KnowledgeGraphEdge]] = defaultdict(list)
        for edge in snapshot.edges:
            self.edges_by_source[edge.source_id].append(edge)
        self.node_ids_by_evidence: dict[str, list[str]] = defaultdict(list)
        for node in snapshot.nodes:
            for evidence_id in node.evidence_ids:
                self.node_ids_by_evidence[evidence_id].append(node.node_id)
            evidence_id = str(node.metadata.get("evidence_id") or "")
            if evidence_id:
                self.node_ids_by_evidence[evidence_id].append(node.node_id)
        self.provenance_by_evidence: dict[str, list[KnowledgeGraphProvenance]] = defaultdict(list)
        for provenance in snapshot.provenance:
            self.provenance_by_evidence[provenance.evidence_id].append(provenance)

    @classmethod
    def from_index_dir(cls, index_dir: str | Path) -> "LocalGraphRepository":
        root = Path(index_dir)
        snapshot = load_knowledge_graph_snapshot(root)
        evidence_units = _read_jsonl_models(root / EVIDENCE_UNITS_FILE, EvidenceUnit)
        return cls(snapshot=snapshot, evidence_units=evidence_units)

    def query_task_candidates(self, node: FlowNode, top_k: int) -> list[TaskEvidenceCandidate]:
        candidates = retrieve_task_evidence_candidates(
            node=node,
            evidence_units=self.evidence_units,
            top_k=max(top_k * 4, top_k),
        )
        table_review = _table_review_notes(candidates)
        enriched: list[TaskEvidenceCandidate] = []
        for candidate in candidates:
            kg_ids = self._candidate_node_ids(candidate)
            kg_paths = self._candidate_paths(candidate, kg_ids)
            provenance_refs = self._candidate_provenance_refs(candidate)
            score_breakdown = _candidate_score_breakdown(candidate)
            needs_review = [*candidate.needs_review, *table_review.get(candidate.evidence.evidence_id, [])]
            notes = [*candidate.notes]
            if needs_review:
                notes.extend(needs_review)
            enriched.append(
                candidate.model_copy(
                    update={
                        "kg_candidate_ids": kg_ids,
                        "kg_paths": kg_paths,
                        "score_breakdown": score_breakdown,
                        "provenance_refs": provenance_refs,
                        "raw_source_path": candidate.evidence.source_path or "",
                        "needs_review": _dedupe(needs_review),
                        "notes": _dedupe(notes),
                    }
                )
            )
        enriched.sort(
            key=lambda item: (
                -item.score,
                item.evidence.doc_id,
                item.evidence.page_idx,
                item.evidence.evidence_id,
            )
        )
        return enriched[:top_k]

    def resolve_references(self, reference_text: str, limit: int = 5) -> list[ReferenceResolution]:
        reference = _canonical_name(reference_text)
        if not reference:
            return [
                ReferenceResolution(
                    reference_text=reference_text,
                    needs_review=["empty_reference_text"],
                )
            ]

        scored: list[tuple[KnowledgeGraphNode, float]] = []
        for node in self.snapshot.nodes:
            if node.node_type != "FlowchartTitle":
                continue
            score = _reference_score(reference, node)
            if score > 0:
                scored.append((node, score))
        scored.sort(key=lambda item: (-item[1], item[0].node_id))
        selected = scored[:limit]
        if not selected:
            return [
                ReferenceResolution(
                    reference_text=reference_text,
                    needs_review=["reference_not_resolved"],
                )
            ]

        ambiguous = len(selected) > 1 and abs(selected[0][1] - selected[1][1]) < 0.001
        resolutions: list[ReferenceResolution] = []
        for node, score in selected:
            paths = self.explain_path(node.node_id, max_depth=2, max_paths=6)
            provenance_refs = _dedupe(
                provenance.provenance_id
                for evidence_id in node.evidence_ids
                for provenance in self.provenance_by_evidence.get(evidence_id, [])
            )
            review = ["ambiguous_reference"] if ambiguous else []
            resolutions.append(
                ReferenceResolution(
                    reference_text=reference_text,
                    resolved_node_ids=[node.node_id],
                    kg_paths=paths,
                    provenance_refs=provenance_refs,
                    needs_review=review,
                    score=round(score, 6),
                )
            )
        return resolutions

    def explain_path(
        self,
        source_id: str,
        target_id: str | None = None,
        max_depth: int = 2,
        max_paths: int = 8,
    ) -> list[str]:
        if source_id not in self.nodes_by_id:
            return []

        paths: list[tuple[list[str], list[str]]] = []
        queue = deque([(source_id, [source_id], [])])
        while queue and len(paths) < max_paths:
            current_id, node_ids, relations = queue.popleft()
            if len(relations) >= max_depth:
                continue
            for edge in self.edges_by_source.get(current_id, []):
                if edge.target_id in node_ids:
                    continue
                next_nodes = [*node_ids, edge.target_id]
                next_relations = [*relations, edge.relation_type]
                if target_id is None or edge.target_id == target_id:
                    paths.append((next_nodes, next_relations))
                    if len(paths) >= max_paths:
                        break
                queue.append((edge.target_id, next_nodes, next_relations))
        return [_render_path(node_ids, relations) for node_ids, relations in paths]

    def _candidate_node_ids(self, candidate: TaskEvidenceCandidate) -> list[str]:
        ids = list(self.node_ids_by_evidence.get(candidate.evidence.evidence_id, []))
        evidence_node_id = _node_id("Evidence", candidate.evidence.evidence_id)
        if evidence_node_id in self.nodes_by_id:
            ids.append(evidence_node_id)
        return _dedupe(ids)

    def _candidate_paths(self, candidate: TaskEvidenceCandidate, kg_ids: list[str]) -> list[str]:
        paths: list[str] = []
        for node_id in kg_ids:
            node = self.nodes_by_id.get(node_id)
            if node is None or node.node_type == "Evidence":
                continue
            paths.extend(self.explain_path(node_id, max_depth=2, max_paths=5))
        return _dedupe([*candidate.graph_paths, *paths])

    def _candidate_provenance_refs(self, candidate: TaskEvidenceCandidate) -> list[str]:
        return _dedupe(
            provenance.provenance_id
            for provenance in self.provenance_by_evidence.get(candidate.evidence.evidence_id, [])
        )


def build_full_knowledge_graph(
    evidence_units: list[EvidenceUnit],
    collection_name: str = "default",
    source: str = "",
) -> KnowledgeGraphSnapshot:
    """Build a deterministic full-KG snapshot from all evidence units."""

    builder = _KnowledgeGraphBuilder()
    for evidence in _dedupe_evidence_units(evidence_units):
        builder.add_evidence(evidence)
    builder.add_same_as_edges()
    builder.link_flowchart_images()
    snapshot = builder.to_snapshot(collection_name=collection_name, source=source, evidence_unit_count=len(evidence_units))
    return snapshot


def write_knowledge_graph_snapshot(snapshot: KnowledgeGraphSnapshot, output_dir: str | Path) -> dict[str, str]:
    """Write a full-KG snapshot to the local JSON/JSONL layout."""

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    files = {
        "kg_manifest": str(root / KG_MANIFEST_FILE),
        "kg_nodes": str(root / KG_NODES_FILE),
        "kg_edges": str(root / KG_EDGES_FILE),
        "kg_provenance": str(root / KG_PROVENANCE_FILE),
    }
    snapshot.manifest.files = files
    snapshot.manifest.node_count = len(snapshot.nodes)
    snapshot.manifest.edge_count = len(snapshot.edges)
    snapshot.manifest.provenance_count = len(snapshot.provenance)
    _write_json(root / KG_MANIFEST_FILE, snapshot.manifest.model_dump(mode="json"))
    _write_jsonl(root / KG_NODES_FILE, snapshot.nodes)
    _write_jsonl(root / KG_EDGES_FILE, snapshot.edges)
    _write_jsonl(root / KG_PROVENANCE_FILE, snapshot.provenance)
    return files


def load_knowledge_graph_snapshot(index_dir: str | Path) -> KnowledgeGraphSnapshot:
    """Load a local full-KG snapshot."""

    root = Path(index_dir)
    manifest_path = root / KG_MANIFEST_FILE
    if not manifest_path.exists():
        raise FileNotFoundError(f"KG manifest does not exist: {manifest_path}")
    manifest = KnowledgeGraphManifest.model_validate(json.loads(manifest_path.read_text(encoding="utf-8")))
    nodes = _read_jsonl_models(root / KG_NODES_FILE, KnowledgeGraphNode)
    edges = _read_jsonl_models(root / KG_EDGES_FILE, KnowledgeGraphEdge)
    provenance = _read_jsonl_models(root / KG_PROVENANCE_FILE, KnowledgeGraphProvenance)
    return KnowledgeGraphSnapshot(manifest=manifest, nodes=nodes, edges=edges, provenance=provenance)


class _KnowledgeGraphBuilder:
    def __init__(self) -> None:
        self.nodes: dict[str, KnowledgeGraphNode] = {}
        self.edges: dict[str, KnowledgeGraphEdge] = {}
        self.provenance: dict[str, KnowledgeGraphProvenance] = {}

    def add_evidence(self, evidence: EvidenceUnit) -> None:
        provenance = _provenance_for_evidence(evidence)
        self.provenance[provenance.provenance_id] = provenance

        document_id = _node_id("Document", evidence.doc_id)
        evidence_id = _node_id("Evidence", evidence.evidence_id)
        role_type, role_id, role_name = _role_node(evidence)

        self.add_node(document_id, "Document", evidence.doc_id, evidence)
        self.add_node(evidence_id, "Evidence", evidence.evidence_id, evidence)
        self.add_node(role_id, role_type, role_name, evidence)

        self.add_edge(document_id, _document_relation(role_type), role_id, evidence, [provenance.provenance_id])
        self.add_edge(role_id, "backed_by_evidence", evidence_id, evidence, [provenance.provenance_id])

        parent_evidence_id = str(evidence.metadata.get("parent_evidence_id") or "")
        if parent_evidence_id and role_type == "TableRow":
            section_id = _node_id("Section", parent_evidence_id)
            table_id = _node_id("Table", f"{evidence.doc_id}:{parent_evidence_id}")
            self.add_node(section_id, "Section", parent_evidence_id, evidence)
            self.add_node(table_id, "Table", f"{parent_evidence_id}:table", evidence)
            self.add_edge(section_id, "contains_table", table_id, evidence, [provenance.provenance_id])
            self.add_edge(table_id, "contains_row", role_id, evidence, [provenance.provenance_id])

        ecu = str(evidence.metadata.get("ecu") or "").strip()
        if ecu:
            ecu_id = _node_id("ECU", ecu)
            self.add_node(ecu_id, "ECU", ecu, evidence)
            self.add_edge(document_id, "document_for_ecu", ecu_id, evidence, [provenance.provenance_id])
            self.add_edge(role_id, "mentions_ecu", ecu_id, evidence, [provenance.provenance_id])

        if not _is_flowchart_body_evidence(evidence):
            for service_id in _list_metadata(evidence.metadata.get("service_ids")):
                service_node_id = _node_id("Service", service_id)
                self.add_node(service_node_id, "Service", service_id, evidence)
                self.add_edge(role_id, "mentions_service", service_node_id, evidence, [provenance.provenance_id])

            fields = extract_parameter_fields(evidence)
            for field_name, value in fields.items():
                self.add_parameter_field(role_id, field_name, value, evidence, provenance.provenance_id)

    def add_parameter_field(self, source_id: str, field_name: str, value: str, evidence: EvidenceUnit, provenance_id: str) -> None:
        if not str(value).strip():
            return
        normalized_key = _normalize_key(field_name)
        if normalized_key == "FLOWCHART_TITLE":
            title_id = _node_id("FlowchartTitle", _safe_fragment(value))
            self.add_node(title_id, "FlowchartTitle", value, evidence)
            self.add_edge(source_id, "refers_to", title_id, evidence, [provenance_id])
            return

        parameter_id = _node_id("ParameterField", f"{_safe_fragment(field_name)}={_safe_fragment(value, 100)}")
        self.add_node(
            parameter_id,
            "ParameterField",
            f"{field_name}={value}",
            evidence,
            metadata={"field_name": field_name, "field_value": value},
        )
        self.add_edge(source_id, "has_field", parameter_id, evidence, [provenance_id])

        if normalized_key == "DID":
            did_id = _node_id("DID", _identifier(value))
            self.add_node(did_id, "DID", _identifier(value), evidence)
            self.add_edge(source_id, "mentions_did", did_id, evidence, [provenance_id])
            self.add_edge(parameter_id, "normalizes_to", did_id, evidence, [provenance_id])
        elif normalized_key == "RID":
            rid_id = _node_id("RID", _identifier(value))
            self.add_node(rid_id, "RID", _identifier(value), evidence)
            self.add_edge(source_id, "mentions_rid", rid_id, evidence, [provenance_id])
            self.add_edge(parameter_id, "normalizes_to", rid_id, evidence, [provenance_id])

    def add_same_as_edges(self) -> None:
        by_tokens: dict[tuple[str, ...], list[KnowledgeGraphNode]] = defaultdict(list)
        for node in self.nodes.values():
            if node.node_type not in {"FlowchartTitle", "AliasTerm", "TemplateClass"}:
                continue
            token_key = tuple(sorted(node.normalized_tokens))
            if token_key:
                by_tokens[token_key].append(node)
        for nodes in by_tokens.values():
            if len(nodes) < 2:
                continue
            canonical = sorted(nodes, key=lambda item: item.node_id)[0]
            for node in nodes:
                if node.node_id != canonical.node_id:
                    self.add_edge(node.node_id, "same_as", canonical.node_id)

    def link_flowchart_images(self) -> None:
        titles = [node for node in self.nodes.values() if node.node_type == "FlowchartTitle"]
        images = [node for node in self.nodes.values() if node.node_type == "FlowchartImage"]
        for title in titles:
            for image in images:
                if _token_overlap(title.name, image.name) >= 0.5:
                    self.add_edge(title.node_id, "backed_by_image", image.node_id)

    def add_node(
        self,
        node_id: str,
        node_type: str,
        name: str,
        evidence: EvidenceUnit | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        existing = self.nodes.get(node_id)
        evidence_ids = [evidence.evidence_id] if evidence is not None else []
        node_metadata = dict(metadata or {})
        if evidence is not None:
            node_metadata.update(
                {
                    "doc_id": evidence.doc_id,
                    "source_path": evidence.source_path or "",
                    "page_idx": evidence.page_idx,
                    "evidence_id": evidence.evidence_id,
                    "block_role": str(evidence.metadata.get("block_role") or ""),
                    "module": str(evidence.metadata.get("module") or ""),
                    "ecu": str(evidence.metadata.get("ecu") or ""),
                }
            )
        if existing is None:
            self.nodes[node_id] = KnowledgeGraphNode(
                node_id=node_id,
                node_type=node_type,
                name=name,
                canonical_name=_canonical_name(name),
                normalized_tokens=sorted(set(_tokens(name))),
                evidence_ids=evidence_ids,
                metadata=node_metadata,
            )
            return
        for evidence_id in evidence_ids:
            if evidence_id not in existing.evidence_ids:
                existing.evidence_ids.append(evidence_id)
        existing.metadata.update({key: value for key, value in node_metadata.items() if value != ""})

    def add_edge(
        self,
        source_id: str,
        relation_type: str,
        target_id: str,
        evidence: EvidenceUnit | None = None,
        provenance_refs: list[str] | None = None,
    ) -> None:
        edge_id = _edge_id(source_id, relation_type, target_id)
        evidence_ids = [evidence.evidence_id] if evidence is not None else []
        if edge_id not in self.edges:
            self.edges[edge_id] = KnowledgeGraphEdge(
                edge_id=edge_id,
                source_id=source_id,
                relation_type=relation_type,
                target_id=target_id,
                evidence_ids=evidence_ids,
                provenance_refs=list(provenance_refs or []),
            )
            return
        edge = self.edges[edge_id]
        for evidence_id in evidence_ids:
            if evidence_id not in edge.evidence_ids:
                edge.evidence_ids.append(evidence_id)
        for provenance_ref in provenance_refs or []:
            if provenance_ref not in edge.provenance_refs:
                edge.provenance_refs.append(provenance_ref)

    def to_snapshot(self, collection_name: str, source: str, evidence_unit_count: int) -> KnowledgeGraphSnapshot:
        nodes = sorted(self.nodes.values(), key=lambda item: item.node_id)
        edges = sorted(self.edges.values(), key=lambda item: item.edge_id)
        provenance = sorted(self.provenance.values(), key=lambda item: item.provenance_id)
        manifest = KnowledgeGraphManifest(
            schema_version=SCHEMA_VERSION,
            collection_name=collection_name,
            source=source,
            node_count=len(nodes),
            edge_count=len(edges),
            provenance_count=len(provenance),
            evidence_unit_count=evidence_unit_count,
        )
        return KnowledgeGraphSnapshot(manifest=manifest, nodes=nodes, edges=edges, provenance=provenance)


def _candidate_score_breakdown(candidate: TaskEvidenceCandidate) -> dict[str, float]:
    breakdown = {"graph_rag_score": round(candidate.score, 6)}
    module = str(candidate.evidence.metadata.get("module") or "")
    if module == "ecu_parameter":
        breakdown["ecu_parameter_doc"] = 2.0
    elif module:
        breakdown["generic_doc_source"] = 1.0
    if candidate.block_role == "table_row":
        breakdown["table_row"] = 2.0
    if candidate.parameter_fields:
        breakdown["parameter_fields"] = 2.0
    if candidate.flowchart_title or candidate.parameter_fields.get("FLOWCHART_TITLE"):
        breakdown["flowchart_reference"] = 1.0
    return breakdown


def _table_review_notes(candidates: list[TaskEvidenceCandidate]) -> dict[str, list[str]]:
    by_parent: dict[str, list[TaskEvidenceCandidate]] = defaultdict(list)
    for candidate in candidates:
        if candidate.block_role != "table_row":
            continue
        parent = str(candidate.evidence.metadata.get("parent_evidence_id") or "")
        if parent:
            by_parent[parent].append(candidate)

    notes: dict[str, list[str]] = defaultdict(list)
    for rows in by_parent.values():
        if len(rows) < 2:
            continue
        top_score = max(row.score for row in rows)
        close_rows = [row for row in rows if top_score - row.score <= 3.0]
        if len(close_rows) < 2:
            continue
        for row in close_rows:
            notes[row.evidence.evidence_id].append("ambiguous_table_rows_not_expanded")
    return notes


def _role_node(evidence: EvidenceUnit) -> tuple[str, str, str]:
    role = str(evidence.metadata.get("block_role") or "")
    if role == "flowchart_body":
        name = str(evidence.metadata.get("img_path") or evidence.content or evidence.evidence_id)
        return "FlowchartImage", _node_id("FlowchartImage", evidence.evidence_id), name
    if role == "table_row":
        return "TableRow", _node_id("TableRow", evidence.evidence_id), evidence.evidence_id
    if role == "flowchart_image":
        name = str(evidence.metadata.get("img_path") or evidence.content or evidence.evidence_id)
        return "FlowchartImage", _node_id("FlowchartImage", evidence.evidence_id), name
    if role == "flowchart_title" or evidence.evidence_type == "flowchart":
        title = str(evidence.metadata.get("section_title") or evidence.content or evidence.evidence_id)
        return "FlowchartTitle", _node_id("FlowchartTitle", _safe_fragment(title)), title
    if evidence.evidence_type == "table":
        return "Table", _node_id("Table", evidence.evidence_id), evidence.evidence_id
    title = str(evidence.metadata.get("section_title") or evidence.evidence_id)
    return "Section", _node_id("Section", evidence.evidence_id), title


def _is_flowchart_body_evidence(evidence: EvidenceUnit) -> bool:
    return str(evidence.metadata.get("block_role") or "") == "flowchart_body"


def _document_relation(role_type: str) -> str:
    return {
        "Section": "contains_section",
        "Table": "contains_table",
        "TableRow": "contains_row",
        "FlowchartTitle": "contains_flowchart_title",
        "FlowchartImage": "contains_flowchart_image",
    }.get(role_type, "contains_evidence")


def _provenance_for_evidence(evidence: EvidenceUnit) -> KnowledgeGraphProvenance:
    content_hash = hashlib.sha256(evidence.content.encode("utf-8")).hexdigest()
    return KnowledgeGraphProvenance(
        provenance_id=f"prov:{evidence.evidence_id}:{content_hash[:12]}",
        evidence_id=evidence.evidence_id,
        source_path=evidence.source_path or "",
        doc_id=evidence.doc_id,
        page_idx=evidence.page_idx,
        bbox=evidence.bbox,
        content_hash=content_hash,
        content_excerpt=_clip(evidence.content, 500),
        metadata={
            "block_role": str(evidence.metadata.get("block_role") or ""),
            "section_title": str(evidence.metadata.get("section_title") or ""),
            "parent_evidence_id": str(evidence.metadata.get("parent_evidence_id") or ""),
            "module": str(evidence.metadata.get("module") or ""),
            "ecu": str(evidence.metadata.get("ecu") or ""),
        },
    )


def _render_path(node_ids: list[str], relations: list[str]) -> str:
    parts: list[str] = [node_ids[0]]
    for relation, node_id in zip(relations, node_ids[1:]):
        parts.append(f"--{relation}-->")
        parts.append(node_id)
    return " ".join(parts)


def _reference_score(reference: str, node: KnowledgeGraphNode) -> float:
    if reference == node.canonical_name:
        return 10.0
    if reference and reference in node.canonical_name:
        return 8.0
    overlap = _token_overlap(reference, node.name)
    if overlap >= 0.58:
        return 4.0 + overlap
    return 0.0


def _node_id(node_type: str, value: str) -> str:
    return f"{node_type}:{_safe_fragment(value)}"


def _edge_id(source_id: str, relation_type: str, target_id: str) -> str:
    digest = hashlib.sha1(f"{source_id}\0{relation_type}\0{target_id}".encode("utf-8")).hexdigest()
    return f"Edge:{digest[:20]}"


def _identifier(value: str) -> str:
    match = re.search(r"\b(?:0x)?[0-9A-Fa-f]{4,8}\b", value)
    if not match:
        return value
    raw = match.group(0).upper()
    return f"0x{raw[2:]}" if raw.startswith("0X") else f"0x{raw}"


def _normalize_key(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", value).strip("_").upper()


def _canonical_name(value: str) -> str:
    return " ".join(_tokens(value))


def _tokens(value: str) -> list[str]:
    tokens: list[str] = []
    for match in TOKEN_RE.finditer(value):
        token = _normalize_token(match.group(0))
        if not token:
            continue
        tokens.append(token)
        compact = re.fullmatch(r"([a-z]+)([0-9]+)", token)
        if compact:
            tokens.extend([compact.group(1), compact.group(2)])
    return tokens


def _normalize_token(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("0x"):
        value = value[2:]
    if value == "immo":
        return "immobilizer"
    return value


def _token_overlap(left: str, right: str) -> float:
    left_tokens = set(_tokens(left))
    if not left_tokens:
        return 0.0
    return len(left_tokens.intersection(_tokens(right))) / len(left_tokens)


def _safe_fragment(value: str, limit: int = 140) -> str:
    value = re.sub(r"[^0-9A-Za-z_.=-]+", "_", str(value).strip())
    value = re.sub(r"_+", "_", value).strip("_")
    return (value[:limit].strip("_") or "value")


def _list_metadata(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _clip(value: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "..."


def _dedupe(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _dedupe_evidence_units(evidence_units: list[EvidenceUnit]) -> list[EvidenceUnit]:
    output: dict[str, EvidenceUnit] = {}
    for evidence in evidence_units:
        output.setdefault(evidence.evidence_id, evidence)
    return list(output.values())


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, models: list) -> None:
    with path.open("w", encoding="utf-8") as file:
        for model in models:
            file.write(json.dumps(model.model_dump(mode="json"), ensure_ascii=False))
            file.write("\n")


def _read_jsonl_models(path: Path, model_cls):
    if not path.exists():
        raise FileNotFoundError(f"KG index file does not exist: {path}")
    output = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            if line.strip():
                output.append(model_cls.model_validate(json.loads(line)))
    return output
