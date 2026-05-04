"""Build and search a lightweight diagnostic Graph RAG graph."""

from __future__ import annotations

from collections import deque
import re
from typing import Iterable

from diagnostic_platform.retrieval.graph_rag import extract_parameter_fields, task_aliases
from diagnostic_platform.schemas import (
    BuildDiagnosticGraphRequest,
    DiagnosticGraph,
    EvidenceMatch,
    EvidenceUnit,
    FlowNode,
    GraphEntity,
    GraphPath,
    GraphPathSearchRequest,
    GraphPathSearchResponse,
    GraphRelation,
    NodeEvidenceBundle,
    TaskEvidenceCandidate,
)


ECU_KEYS = {"ADCU", "ASDM", "BCM", "CMD", "CMP", "ECM", "FAH", "HCML", "HCMR", "TCAM", "WPC", "ZCUD"}


def build_diagnostic_graph(request: BuildDiagnosticGraphRequest) -> DiagnosticGraph:
    """Build a deterministic graph from flow tasks and Graph RAG evidence."""

    builder = _GraphBuilder()
    _add_flow_entities_and_sequence(builder, request)
    _add_evidence_entities(builder, request)
    _add_bundle_relations(builder, request)
    return builder.to_graph()


def search_graph_paths(request: GraphPathSearchRequest) -> GraphPathSearchResponse:
    """Search graph paths using breadth-first traversal."""

    allowed_relations = set(request.relation_types)
    adjacency: dict[str, list[GraphRelation]] = {}
    for relation in request.graph.relations:
        if allowed_relations and relation.relation_type not in allowed_relations:
            continue
        adjacency.setdefault(relation.source_id, []).append(relation)

    paths: list[GraphPath] = []
    queue = deque([(request.source_id, [request.source_id], [])])
    while queue and len(paths) < request.max_paths:
        current_id, entity_ids, relation_types = queue.popleft()
        if len(relation_types) >= request.max_depth:
            continue

        for relation in adjacency.get(current_id, []):
            if relation.target_id in entity_ids:
                continue
            next_entities = [*entity_ids, relation.target_id]
            next_relations = [*relation_types, relation.relation_type]
            if request.target_id is None or relation.target_id == request.target_id:
                paths.append(GraphPath(entity_ids=next_entities, relation_types=next_relations))
                if len(paths) >= request.max_paths:
                    break
            queue.append((relation.target_id, next_entities, next_relations))

    return GraphPathSearchResponse(paths=paths)


def _add_flow_entities_and_sequence(builder: "_GraphBuilder", request: BuildDiagnosticGraphRequest) -> None:
    previous_tasks: list[str] = []
    for step in request.flow_plan.steps:
        current_tasks: list[str] = []
        for node in step.parallel_nodes:
            task_id = _flow_task_id(node.name)
            current_tasks.append(task_id)
            aliases = task_aliases(node)
            builder.add_entity(
                GraphEntity(
                    entity_id=task_id,
                    entity_type="FlowTask",
                    name=node.name,
                    aliases=aliases,
                    metadata={
                        "raw": node.raw,
                        "step_key": step.step_key,
                        "display_name": step.display_name,
                        "order": step.order,
                        "row": node.row,
                        "column": node.column,
                        "template_name": node.template_name,
                    },
                )
            )

            _add_compat_flow_node(builder, node, task_id)
            _add_template_entities(builder, task_id, node.template_name)
            _add_alias_entities(builder, task_id, aliases)
            _add_target_ecu(builder, task_id, node.name, node.template_name)

        for previous in previous_tasks:
            for current in current_tasks:
                builder.add_relation(GraphRelation(source_id=previous, relation_type="next_step", target_id=current))
        previous_tasks = current_tasks


def _add_compat_flow_node(builder: "_GraphBuilder", node: FlowNode, task_id: str) -> None:
    flow_node_id = _entity_id("FlowNode", node.name)
    builder.add_entity(
        GraphEntity(
            entity_id=flow_node_id,
            entity_type="FlowNode",
            name=node.name,
            aliases=[node.raw],
            metadata={"template_name": node.template_name, "compatibility": True},
        )
    )
    builder.add_relation(GraphRelation(source_id=flow_node_id, relation_type="alias_of", target_id=task_id))


def _add_template_entities(builder: "_GraphBuilder", task_id: str, template_name: str) -> None:
    if not template_name:
        return
    template_class_id = _template_class_id(template_name)
    xml_template_id = _xml_template_id(template_name)
    builder.add_entity(GraphEntity(entity_id=template_class_id, entity_type="TemplateClass", name=template_name))
    builder.add_entity(GraphEntity(entity_id=xml_template_id, entity_type="XmlTemplate", name=template_name))
    builder.add_relation(GraphRelation(source_id=task_id, relation_type="uses_template", target_id=template_class_id))
    builder.add_relation(
        GraphRelation(source_id=template_class_id, relation_type="backed_by_xml_template", target_id=xml_template_id)
    )


def _add_alias_entities(builder: "_GraphBuilder", task_id: str, aliases: Iterable[str]) -> None:
    for alias in aliases:
        alias = " ".join(alias.split())
        if not alias:
            continue
        alias_id = _alias_id(alias)
        builder.add_entity(GraphEntity(entity_id=alias_id, entity_type="AliasTerm", name=alias))
        builder.add_relation(GraphRelation(source_id=task_id, relation_type="matches_alias", target_id=alias_id))


def _add_target_ecu(builder: "_GraphBuilder", task_id: str, node_name: str, template_name: str) -> None:
    ecu = _primary_ecu_key(node_name, template_name)
    if not ecu:
        return
    ecu_id = _entity_id("ECU", ecu)
    builder.add_entity(GraphEntity(entity_id=ecu_id, entity_type="ECU", name=ecu))
    builder.add_relation(GraphRelation(source_id=task_id, relation_type="targets_ecu", target_id=ecu_id))


def _add_evidence_entities(builder: "_GraphBuilder", request: BuildDiagnosticGraphRequest) -> None:
    seen: set[str] = set()
    for evidence in request.evidence_units:
        if evidence.evidence_id in seen:
            continue
        seen.add(evidence.evidence_id)
        _add_single_evidence(builder, evidence)

    if request.evidence_response is None:
        return
    for step in request.evidence_response.bundles:
        for node_bundle in step.node_bundles:
            for evidence in _bundle_evidence_units(node_bundle):
                if evidence.evidence_id in seen:
                    continue
                seen.add(evidence.evidence_id)
                _add_single_evidence(builder, evidence)


def _add_single_evidence(builder: "_GraphBuilder", evidence: EvidenceUnit) -> None:
    document_id = _document_id(evidence.doc_id)
    role_entity = _evidence_role_entity(evidence)
    evidence_id = _evidence_id(evidence.evidence_id)
    builder.add_entity(GraphEntity(entity_id=document_id, entity_type="Document", name=evidence.doc_id))
    builder.add_entity(
        GraphEntity(
            entity_id=evidence_id,
            entity_type="Evidence",
            name=evidence.evidence_id,
            metadata={
                "evidence_type": evidence.evidence_type,
                "doc_id": evidence.doc_id,
                "page_idx": evidence.page_idx,
                "source_path": evidence.source_path or "",
            },
        )
    )
    builder.add_entity(
        GraphEntity(
            entity_id=role_entity["entity_id"],
            entity_type=role_entity["entity_type"],
            name=str(evidence.metadata.get("section_title") or evidence.evidence_id),
            metadata={
                "evidence_id": evidence.evidence_id,
                "block_role": role_entity["block_role"],
                "doc_id": evidence.doc_id,
                "page_idx": evidence.page_idx,
                "source_path": evidence.source_path or "",
            },
        )
    )
    builder.add_relation(
        GraphRelation(
            source_id=document_id,
            relation_type=role_entity["document_relation"],
            target_id=role_entity["entity_id"],
            evidence_ids=[evidence.evidence_id],
        )
    )
    builder.add_relation(
        GraphRelation(
            source_id=role_entity["entity_id"],
            relation_type="backed_by_evidence",
            target_id=evidence_id,
            evidence_ids=[evidence.evidence_id],
        )
    )

    parent_evidence_id = str(evidence.metadata.get("parent_evidence_id") or "")
    if parent_evidence_id and role_entity["entity_type"] == "TableRow":
        parent_id = _section_id(parent_evidence_id)
        builder.add_entity(GraphEntity(entity_id=parent_id, entity_type="Section", name=parent_evidence_id))
        builder.add_relation(
            GraphRelation(
                source_id=parent_id,
                relation_type="contains_row",
                target_id=role_entity["entity_id"],
                evidence_ids=[evidence.evidence_id],
            )
        )

    ecu = str(evidence.metadata.get("ecu") or "").strip()
    if ecu:
        ecu_id = _entity_id("ECU", ecu)
        builder.add_entity(GraphEntity(entity_id=ecu_id, entity_type="ECU", name=ecu))
        builder.add_relation(
            GraphRelation(source_id=document_id, relation_type="document_for_ecu", target_id=ecu_id)
        )

    _add_parameter_field_entities(builder, role_entity["entity_id"], role_entity["entity_type"], evidence)


def _add_parameter_field_entities(
    builder: "_GraphBuilder",
    source_id: str,
    source_type: str,
    evidence: EvidenceUnit,
    fields: dict[str, str] | None = None,
) -> None:
    fields = fields if fields is not None else extract_parameter_fields(evidence)
    for key, value in fields.items():
        if not str(value).strip():
            continue
        if _normalize_key(key) == "FLOWCHART_TITLE":
            title_id = _flowchart_title_id(str(value))
            builder.add_entity(GraphEntity(entity_id=title_id, entity_type="FlowchartTitle", name=str(value)))
            builder.add_relation(
                GraphRelation(
                    source_id=source_id,
                    relation_type="section_refers_flowchart",
                    target_id=title_id,
                    evidence_ids=[evidence.evidence_id],
                )
            )
            continue

        field_id = _parameter_field_id(str(key), str(value))
        builder.add_entity(
            GraphEntity(
                entity_id=field_id,
                entity_type="ParameterField",
                name=f"{key}={value}",
                metadata={"field_name": str(key), "field_value": str(value)},
            )
        )
        relation = "row_has_field" if source_type == "TableRow" else "section_has_field"
        builder.add_relation(
            GraphRelation(
                source_id=source_id,
                relation_type=relation,
                target_id=field_id,
                evidence_ids=[evidence.evidence_id],
            )
        )


def _add_bundle_relations(builder: "_GraphBuilder", request: BuildDiagnosticGraphRequest) -> None:
    if request.evidence_response is None:
        return
    for step in request.evidence_response.bundles:
        for node_bundle in step.node_bundles:
            task_id = _flow_task_id(node_bundle.node_name)
            for item in _bundle_candidate_items(node_bundle):
                evidence = item["evidence"]
                role_entity = _evidence_role_entity(evidence)
                builder.add_relation(
                    GraphRelation(
                        source_id=task_id,
                        relation_type="supported_by",
                        target_id=role_entity["entity_id"],
                        evidence_ids=[evidence.evidence_id],
                        metadata={
                            "score": float(item["score"]),
                            "matched_terms": list(item["matched_terms"]),
                            "block_role": role_entity["block_role"],
                        },
                    )
                )
                for alias in item["matched_terms"]:
                    alias_id = _alias_id(str(alias))
                    builder.add_entity(GraphEntity(entity_id=alias_id, entity_type="AliasTerm", name=str(alias)))
                    builder.add_relation(
                        GraphRelation(source_id=task_id, relation_type="matches_alias", target_id=alias_id)
                    )
                _add_parameter_field_entities(
                    builder=builder,
                    source_id=role_entity["entity_id"],
                    source_type=role_entity["entity_type"],
                    evidence=evidence,
                    fields=item["fields"],
                )


def _bundle_evidence_units(node_bundle: NodeEvidenceBundle) -> list[EvidenceUnit]:
    units: list[EvidenceUnit] = []
    for candidate in node_bundle.candidates:
        units.append(candidate.evidence)
    for match in node_bundle.matches:
        units.append(match.evidence)
    return _dedupe_evidence_units(units)


def _bundle_candidate_items(node_bundle: NodeEvidenceBundle) -> list[dict]:
    items: list[dict] = []
    if node_bundle.candidates:
        for candidate in node_bundle.candidates:
            items.append(_candidate_item(candidate))
        return items
    for match in node_bundle.matches:
        items.append(_match_item(match))
    return items


def _candidate_item(candidate: TaskEvidenceCandidate) -> dict:
    fields = candidate.parameter_fields or extract_parameter_fields(candidate.evidence, candidate.matched_terms)
    return {
        "evidence": candidate.evidence,
        "score": candidate.score,
        "matched_terms": candidate.matched_terms,
        "fields": fields,
    }


def _match_item(match: EvidenceMatch) -> dict:
    return {
        "evidence": match.evidence,
        "score": match.score,
        "matched_terms": match.matched_terms,
        "fields": extract_parameter_fields(match.evidence, match.matched_terms),
    }


def _evidence_role_entity(evidence: EvidenceUnit) -> dict[str, str]:
    role = _block_role(evidence)
    if role == "table_row":
        return {
            "entity_id": _table_row_id(evidence.evidence_id),
            "entity_type": "TableRow",
            "block_role": role,
            "document_relation": "contains_row",
        }
    if role == "flowchart_title":
        title = str(evidence.metadata.get("section_title") or evidence.evidence_id)
        return {
            "entity_id": _flowchart_title_id(title),
            "entity_type": "FlowchartTitle",
            "block_role": role,
            "document_relation": "contains_flowchart_title",
        }
    if role == "flowchart_image":
        return {
            "entity_id": _entity_id("FlowchartImage", evidence.evidence_id),
            "entity_type": "FlowchartImage",
            "block_role": role,
            "document_relation": "contains_flowchart_image",
        }
    return {
        "entity_id": _section_id(evidence.evidence_id),
        "entity_type": "Section",
        "block_role": role,
        "document_relation": "contains_section",
    }


def _block_role(evidence: EvidenceUnit) -> str:
    role = str(evidence.metadata.get("block_role") or "")
    if role:
        return role
    if evidence.evidence_type == "table":
        return "table_row" if evidence.metadata.get("table_fields") else "table"
    if evidence.evidence_type == "flowchart":
        return "flowchart_title"
    return "section_text"


def _primary_ecu_key(node_name: str, template_name: str) -> str:
    for value in [node_name.split("_", 1)[0], template_name.split("_", 1)[0]]:
        normalized = value.upper()
        if normalized.startswith("GEEA"):
            continue
        if normalized in ECU_KEYS or any(normalized.startswith(prefix) for prefix in ECU_KEYS):
            return normalized
    return ""


def _flow_task_id(name: str) -> str:
    return _entity_id("FlowTask", name)


def _template_class_id(name: str) -> str:
    return _entity_id("TemplateClass", name)


def _xml_template_id(name: str) -> str:
    return _entity_id("XmlTemplate", name)


def _document_id(name: str) -> str:
    return _entity_id("Document", name)


def _section_id(name: str) -> str:
    return _entity_id("Section", name)


def _table_row_id(name: str) -> str:
    return _entity_id("TableRow", name)


def _evidence_id(name: str) -> str:
    return _entity_id("Evidence", name)


def _alias_id(name: str) -> str:
    return _entity_id("AliasTerm", _safe_fragment(name))


def _flowchart_title_id(name: str) -> str:
    return _entity_id("FlowchartTitle", _safe_fragment(name))


def _parameter_field_id(key: str, value: str) -> str:
    return _entity_id("ParameterField", f"{_safe_fragment(key)}={_safe_fragment(value, limit=80)}")


def _entity_id(entity_type: str, name: str) -> str:
    return f"{entity_type}:{name}"


def _normalize_key(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", value).strip("_").upper()


def _safe_fragment(value: str, limit: int = 120) -> str:
    value = re.sub(r"[^0-9A-Za-z_.=-]+", "_", value.strip())
    value = re.sub(r"_+", "_", value).strip("_")
    return (value[:limit].strip("_") or "value")


def _dedupe_evidence_units(evidence_units: list[EvidenceUnit]) -> list[EvidenceUnit]:
    output: list[EvidenceUnit] = []
    seen: set[str] = set()
    for unit in evidence_units:
        if unit.evidence_id in seen:
            continue
        seen.add(unit.evidence_id)
        output.append(unit)
    return output


class _GraphBuilder:
    def __init__(self) -> None:
        self.entities: dict[str, GraphEntity] = {}
        self.relations: dict[tuple[str, str, str], GraphRelation] = {}

    def add_entity(self, entity: GraphEntity) -> None:
        if entity.entity_id not in self.entities:
            self.entities[entity.entity_id] = entity

    def add_relation(self, relation: GraphRelation) -> None:
        key = (relation.source_id, relation.relation_type, relation.target_id)
        if key not in self.relations:
            self.relations[key] = relation
            return
        existing = self.relations[key]
        evidence_ids = [*existing.evidence_ids]
        for evidence_id in relation.evidence_ids:
            if evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
        existing.evidence_ids = evidence_ids

    def to_graph(self) -> DiagnosticGraph:
        return DiagnosticGraph(
            entities=sorted(self.entities.values(), key=lambda item: item.entity_id),
            relations=sorted(
                self.relations.values(),
                key=lambda item: (item.source_id, item.relation_type, item.target_id),
            ),
        )
