"""Attach Graph RAG path evidence to flow evidence bundles."""

from __future__ import annotations

import re

from diagnostic_platform.graph.diagnostic_graph import build_diagnostic_graph, search_graph_paths
from diagnostic_platform.retrieval.graph_rag import extract_parameter_fields
from diagnostic_platform.schemas import (
    BuildDiagnosticGraphRequest,
    BuildStepEvidenceResponse,
    DiagnosticGraph,
    EvidenceMatch,
    EvidenceUnit,
    FlowStepPlan,
    GraphPath,
    GraphPathSearchRequest,
    NodeEvidenceBundle,
    TaskEvidenceCandidate,
)


def attach_graph_paths_to_evidence_response(
    flow_plan: FlowStepPlan,
    evidence_response: BuildStepEvidenceResponse,
    evidence_units: list[EvidenceUnit],
    max_paths_per_node: int = 12,
    max_depth: int = 2,
) -> tuple[BuildStepEvidenceResponse, DiagnosticGraph]:
    """Build a flow-aware Graph RAG graph and add readable paths to each node bundle."""

    graph = build_diagnostic_graph(
        BuildDiagnosticGraphRequest(
            flow_plan=flow_plan,
            evidence_response=evidence_response,
            evidence_units=evidence_units,
        )
    )
    enriched_steps = []
    for step in evidence_response.bundles:
        node_bundles = []
        for node_bundle in step.node_bundles:
            graph_paths = _graph_paths_for_node(
                graph=graph,
                node_bundle=node_bundle,
                max_paths_per_node=max_paths_per_node,
                max_depth=max_depth,
            )
            candidates = _update_candidate_paths(node_bundle.candidates, graph_paths)
            node_bundles.append(
                node_bundle.model_copy(update={"graph_paths": graph_paths, "candidates": candidates})
            )
        enriched_steps.append(step.model_copy(update={"node_bundles": node_bundles}))

    return evidence_response.model_copy(update={"bundles": enriched_steps}), graph


def _graph_paths_for_node(
    graph: DiagnosticGraph,
    node_bundle: NodeEvidenceBundle,
    max_paths_per_node: int,
    max_depth: int,
) -> list[str]:
    source_id = _entity_id("FlowTask", node_bundle.node_name)
    paths: list[str] = []

    for candidate in node_bundle.candidates:
        for path in candidate.graph_paths:
            _append_path(paths, path, max_paths_per_node)

    for path in _search_paths(
        graph=graph,
        source_id=source_id,
        target_id=None,
        relation_types=["next_step"],
        max_depth=1,
        max_paths=2,
    ):
        _append_path(paths, path, max_paths_per_node)

    for target in _path_targets(node_bundle):
        if len(paths) >= max_paths_per_node:
            break
        for path in _search_paths(
            graph=graph,
            source_id=source_id,
            target_id=target["target_id"],
            relation_types=target["relation_types"],
            max_depth=min(max_depth, int(target["max_depth"])),
            max_paths=max_paths_per_node - len(paths),
        ):
            _append_path(paths, path, max_paths_per_node)
    return paths


def _search_paths(
    graph: DiagnosticGraph,
    source_id: str,
    target_id: str | None,
    relation_types: list[str],
    max_depth: int,
    max_paths: int,
) -> list[str]:
    result = search_graph_paths(
        GraphPathSearchRequest(
            graph=graph,
            source_id=source_id,
            target_id=target_id,
            relation_types=relation_types,
            max_depth=max_depth,
            max_paths=max_paths,
        )
    )
    return [_serialize_path(path) for path in result.paths]


def _path_targets(node_bundle: NodeEvidenceBundle) -> list[dict[str, str | int | list[str]]]:
    targets: list[dict[str, str | int | list[str]]] = []
    if node_bundle.template_name:
        targets.append(
            {
                "target_id": _entity_id("TemplateClass", node_bundle.template_name),
                "relation_types": ["uses_template"],
                "max_depth": 1,
            }
        )
        targets.append(
            {
                "target_id": _entity_id("XmlTemplate", node_bundle.template_name),
                "relation_types": ["uses_template", "backed_by_xml_template"],
                "max_depth": 2,
            }
        )

    for item in _candidate_items(node_bundle):
        targets.append(
            {
                "target_id": _role_entity_id(item["evidence"]),
                "relation_types": ["supported_by"],
                "max_depth": 1,
            }
        )
        for key, value in item["fields"].items():
            if _normalize_key(key) == "FLOWCHART_TITLE":
                targets.append(
                    {
                        "target_id": _flowchart_title_id(str(value)),
                        "relation_types": ["supported_by", "section_refers_flowchart"],
                        "max_depth": 2,
                    }
                )
                continue
            targets.append(
                {
                    "target_id": _parameter_field_id(str(key), str(value)),
                    "relation_types": ["supported_by", "row_has_field", "section_has_field"],
                    "max_depth": 2,
                }
            )

    deduped: list[dict[str, str | int | list[str]]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for target in targets:
        relation_types = tuple(str(item) for item in target["relation_types"])
        key = (str(target["target_id"]), relation_types)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(target)
    return deduped


def _candidate_items(node_bundle: NodeEvidenceBundle) -> list[dict]:
    if node_bundle.candidates:
        return [_candidate_item(candidate) for candidate in node_bundle.candidates]
    return [_match_item(match) for match in node_bundle.matches]


def _candidate_item(candidate: TaskEvidenceCandidate) -> dict:
    return {
        "evidence": candidate.evidence,
        "fields": candidate.parameter_fields or extract_parameter_fields(candidate.evidence, candidate.matched_terms),
    }


def _match_item(match: EvidenceMatch) -> dict:
    return {"evidence": match.evidence, "fields": extract_parameter_fields(match.evidence, match.matched_terms)}


def _update_candidate_paths(
    candidates: list[TaskEvidenceCandidate],
    graph_paths: list[str],
) -> list[TaskEvidenceCandidate]:
    updated = []
    for candidate in candidates:
        paths = [*candidate.graph_paths]
        role_id = _role_entity_id(candidate.evidence)
        for path in graph_paths:
            if role_id in path or any(_parameter_field_id(str(k), str(v)) in path for k, v in candidate.parameter_fields.items()):
                _append_path(paths, path, len(paths) + 1)
        updated.append(candidate.model_copy(update={"graph_paths": _dedupe(paths)}))
    return updated


def _role_entity_id(evidence: EvidenceUnit) -> str:
    role = str(evidence.metadata.get("block_role") or "")
    if role == "table_row":
        return _entity_id("TableRow", evidence.evidence_id)
    if role == "flowchart_title":
        return _flowchart_title_id(str(evidence.metadata.get("section_title") or evidence.evidence_id))
    if role == "flowchart_image":
        return _entity_id("FlowchartImage", evidence.evidence_id)
    return _entity_id("Section", evidence.evidence_id)


def _append_path(paths: list[str], path: str, max_paths: int) -> None:
    if path and path not in paths and len(paths) < max_paths:
        paths.append(path)


def _serialize_path(path: GraphPath) -> str:
    if not path.entity_ids:
        return ""
    parts = [path.entity_ids[0]]
    for relation_type, entity_id in zip(path.relation_types, path.entity_ids[1:]):
        parts.append(f"--{relation_type}-->")
        parts.append(entity_id)
    return " ".join(parts)


def _entity_id(entity_type: str, name: str) -> str:
    return f"{entity_type}:{name}"


def _flowchart_title_id(name: str) -> str:
    return _entity_id("FlowchartTitle", _safe_fragment(name))


def _parameter_field_id(key: str, value: str) -> str:
    return _entity_id("ParameterField", f"{_safe_fragment(key)}={_safe_fragment(value, limit=80)}")


def _normalize_key(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", value).strip("_").upper()


def _safe_fragment(value: str, limit: int = 120) -> str:
    value = re.sub(r"[^0-9A-Za-z_.=-]+", "_", value.strip())
    value = re.sub(r"_+", "_", value).strip("_")
    return (value[:limit].strip("_") or "value")


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result
