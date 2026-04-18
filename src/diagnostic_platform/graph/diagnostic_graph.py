"""Build and search a lightweight diagnostic graph."""

from __future__ import annotations

from collections import deque

from diagnostic_platform.schemas import (
    BuildDiagnosticGraphRequest,
    DiagnosticGraph,
    EvidenceUnit,
    GraphEntity,
    GraphPath,
    GraphPathSearchRequest,
    GraphPathSearchResponse,
    GraphRelation,
)


def build_diagnostic_graph(request: BuildDiagnosticGraphRequest) -> DiagnosticGraph:
    """Build a deterministic graph from flow steps and evidence bundles."""

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
    previous_nodes: list[str] = []
    for step in request.flow_plan.steps:
        current_nodes: list[str] = []
        for node in step.parallel_nodes:
            flow_id = _flow_node_id(node.name)
            current_nodes.append(flow_id)
            builder.add_entity(
                GraphEntity(
                    entity_id=flow_id,
                    entity_type="FlowNode",
                    name=node.name,
                    aliases=[node.raw],
                    metadata={
                        "step_key": step.step_key,
                        "display_name": step.display_name,
                        "order": step.order,
                        "column": node.column,
                        "template_name": node.template_name,
                    },
                )
            )

            if node.template_name:
                template_id = _template_id(node.template_name)
                builder.add_entity(
                    GraphEntity(
                        entity_id=template_id,
                        entity_type="XmlTemplate",
                        name=node.template_name,
                    )
                )
                builder.add_relation(
                    GraphRelation(
                        source_id=flow_id,
                        relation_type="uses_template",
                        target_id=template_id,
                    )
                )

            ecu = _primary_ecu_key(node.name, node.template_name)
            if ecu:
                ecu_id = _entity_id("ECU", ecu)
                builder.add_entity(GraphEntity(entity_id=ecu_id, entity_type="ECU", name=ecu))
                builder.add_relation(
                    GraphRelation(source_id=flow_id, relation_type="targets_ecu", target_id=ecu_id)
                )

        for previous in previous_nodes:
            for current in current_nodes:
                builder.add_relation(
                    GraphRelation(source_id=previous, relation_type="next_step", target_id=current)
                )
        previous_nodes = current_nodes


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
            for match in node_bundle.matches:
                if match.evidence.evidence_id in seen:
                    continue
                seen.add(match.evidence.evidence_id)
                _add_single_evidence(builder, match.evidence)


def _add_single_evidence(builder: "_GraphBuilder", evidence: EvidenceUnit) -> None:
    evidence_id = _evidence_id(evidence.evidence_id)
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

    for key, relation_type, entity_type in [
        ("dids", "mentions_did", "DID"),
        ("service_ids", "mentions_service", "Service"),
        ("sessions", "mentions_session", "Session"),
        ("security_levels", "mentions_security", "SecurityLevel"),
    ]:
        for value in _metadata_list(evidence, key):
            target_id = _entity_id(entity_type, value)
            builder.add_entity(GraphEntity(entity_id=target_id, entity_type=entity_type, name=value))
            builder.add_relation(
                GraphRelation(
                    source_id=evidence_id,
                    relation_type=relation_type,
                    target_id=target_id,
                    evidence_ids=[evidence.evidence_id],
                )
            )

    ecu = str(evidence.metadata.get("ecu") or "").strip()
    if ecu:
        ecu_id = _entity_id("ECU", ecu)
        builder.add_entity(GraphEntity(entity_id=ecu_id, entity_type="ECU", name=ecu))
        builder.add_relation(
            GraphRelation(
                source_id=evidence_id,
                relation_type="mentions_ecu",
                target_id=ecu_id,
                evidence_ids=[evidence.evidence_id],
            )
        )


def _add_bundle_relations(builder: "_GraphBuilder", request: BuildDiagnosticGraphRequest) -> None:
    if request.evidence_response is None:
        return
    for step in request.evidence_response.bundles:
        for node_bundle in step.node_bundles:
            flow_id = _flow_node_id(node_bundle.node_name)
            for match in node_bundle.matches:
                evidence_node_id = _evidence_id(match.evidence.evidence_id)
                builder.add_relation(
                    GraphRelation(
                        source_id=flow_id,
                        relation_type="supported_by",
                        target_id=evidence_node_id,
                        evidence_ids=[match.evidence.evidence_id],
                        metadata={"score": match.score, "matched_terms": match.matched_terms},
                    )
                )

                for did in _metadata_list(match.evidence, "dids"):
                    builder.add_relation(
                        GraphRelation(
                            source_id=flow_id,
                            relation_type="uses_did",
                            target_id=_entity_id("DID", did),
                            evidence_ids=[match.evidence.evidence_id],
                        )
                    )
                for service in _metadata_list(match.evidence, "service_ids"):
                    builder.add_relation(
                        GraphRelation(
                            source_id=flow_id,
                            relation_type="uses_service",
                            target_id=_entity_id("Service", service),
                            evidence_ids=[match.evidence.evidence_id],
                        )
                    )
                for session in _metadata_list(match.evidence, "sessions"):
                    builder.add_relation(
                        GraphRelation(
                            source_id=flow_id,
                            relation_type="requires_session",
                            target_id=_entity_id("Session", session),
                            evidence_ids=[match.evidence.evidence_id],
                        )
                    )
                for security in _metadata_list(match.evidence, "security_levels"):
                    builder.add_relation(
                        GraphRelation(
                            source_id=flow_id,
                            relation_type="requires_security",
                            target_id=_entity_id("SecurityLevel", security),
                            evidence_ids=[match.evidence.evidence_id],
                        )
                    )


def _metadata_list(evidence: EvidenceUnit, key: str) -> list[str]:
    value = evidence.metadata.get(key)
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if value:
        return [str(value)]
    return []


def _primary_ecu_key(node_name: str, template_name: str) -> str:
    for value in [node_name.split("_", 1)[0], template_name.split("_", 1)[0]]:
        if any(char.isdigit() for char in value) or value.upper() in {"ADCU", "ASDM", "BCM", "CMD", "ECM", "FAH", "HCML", "HCMR", "TCAM", "WPC", "ZCUD"}:
            return value
    return ""


def _flow_node_id(name: str) -> str:
    return _entity_id("FlowNode", name)


def _template_id(name: str) -> str:
    return _entity_id("XmlTemplate", name)


def _evidence_id(name: str) -> str:
    return _entity_id("Evidence", name)


def _entity_id(entity_type: str, name: str) -> str:
    return f"{entity_type}:{name}"


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
