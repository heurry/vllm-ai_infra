"""Build deterministic evidence bundles for flow steps."""

from __future__ import annotations

import re

from diagnostic_platform.schemas import (
    BuildStepEvidenceRequest,
    BuildStepEvidenceResponse,
    EvidenceMatch,
    EvidenceUnit,
    FlowNode,
    NodeEvidenceBundle,
    StepEvidenceBundle,
)


def build_step_evidence_bundles(request: BuildStepEvidenceRequest) -> BuildStepEvidenceResponse:
    """Link each flow node to the most relevant evidence units."""

    bundles: list[StepEvidenceBundle] = []
    for step in request.flow_plan.steps:
        node_bundles: list[NodeEvidenceBundle] = []
        for node in step.parallel_nodes:
            matches = _rank_evidence(node, request.evidence_units, request.top_k_per_node)
            notes = [] if matches else ["no_evidence_match"]
            node_bundles.append(
                NodeEvidenceBundle(
                    step_key=step.step_key,
                    node_name=node.name,
                    template_name=node.template_name,
                    matches=matches,
                    graph_paths=[],
                    notes=notes,
                )
            )

        bundles.append(
            StepEvidenceBundle(
                step_key=step.step_key,
                display_name=step.display_name,
                order=step.order,
                column=step.column,
                node_bundles=node_bundles,
            )
        )
    return BuildStepEvidenceResponse(source_flow_path=request.flow_plan.source_path, bundles=bundles)


def _rank_evidence(node: FlowNode, evidence_units: list[EvidenceUnit], top_k: int) -> list[EvidenceMatch]:
    query_terms = _node_terms(node)
    scored: list[EvidenceMatch] = []
    for evidence in evidence_units:
        text = _normalized_evidence_text(evidence)
        matched_terms = sorted(term for term in query_terms if term in text)
        score = _score_evidence(node, evidence, text, matched_terms)
        if score <= 0:
            continue
        scored.append(EvidenceMatch(evidence=evidence, score=score, matched_terms=matched_terms))

    scored.sort(key=lambda item: (-item.score, item.evidence.doc_id, item.evidence.page_idx, item.evidence.evidence_id))
    return scored[:top_k]


def _score_evidence(
    node: FlowNode,
    evidence: EvidenceUnit,
    evidence_text: str,
    matched_terms: list[str],
) -> float:
    score = float(len(matched_terms))
    node_phrase = _normalize(node.name.replace("_", " "))
    template_phrase = _normalize(node.template_name.replace("_", " "))
    action_phrase = _normalize(_action_text(node.name))

    if node_phrase and node_phrase in evidence_text:
        score += 10.0
    if action_phrase and action_phrase in evidence_text:
        score += 6.0
    if template_phrase and template_phrase in evidence_text:
        score += 5.0
    if evidence.evidence_type in {"table", "flowchart"}:
        score += 0.5
    if _primary_ecu_key(node) and _normalize(_primary_ecu_key(node)) in evidence_text:
        score += 4.0
    if "refer to" in evidence_text or "eol process" in evidence_text:
        score += 1.0

    return score


def _node_terms(node: FlowNode) -> set[str]:
    terms: set[str] = set()
    for value in [node.name, node.template_name, _action_text(node.name), _primary_ecu_key(node)]:
        normalized = _normalize(value)
        if normalized:
            terms.add(normalized)
        for part in re.split(r"[^0-9a-zA-Z]+", value):
            part = _normalize(part)
            if len(part) >= 3:
                terms.add(part)
    return terms


def _normalized_evidence_text(evidence: EvidenceUnit) -> str:
    metadata_values = []
    for value in evidence.metadata.values():
        if isinstance(value, list):
            metadata_values.extend(str(item) for item in value)
        else:
            metadata_values.append(str(value))
    return _normalize(" ".join([evidence.content, *metadata_values]))


def _action_text(node_name: str) -> str:
    value = re.sub(r"_\d+$", "", node_name)
    parts = value.split("_")
    if len(parts) > 1 and _looks_like_ecu(parts[0]):
        value = "_".join(parts[1:])
    return value.replace("_", " ")


def _primary_ecu_key(node: FlowNode) -> str:
    first = node.name.split("_", 1)[0]
    if _looks_like_ecu(first):
        return first
    template_first = node.template_name.split("_", 1)[0]
    return template_first if _looks_like_ecu(template_first) else ""


def _looks_like_ecu(value: str) -> bool:
    return bool(re.search(r"\d", value)) or value.upper() in {
        "ADCU",
        "ASDM",
        "BCM",
        "CMD",
        "ECM",
        "FAH",
        "HCML",
        "HCMR",
        "TCAM",
        "WPC",
        "ZCUD",
    }


def _normalize(value: str) -> str:
    value = value.replace("_", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip().lower()
