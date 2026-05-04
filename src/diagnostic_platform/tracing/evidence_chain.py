"""Reference-aware evidence chain planning, filtering, and reporting."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from diagnostic_platform.graph.full_kg import GraphRepository, LocalGraphRepository
from diagnostic_platform.retrieval.graph_rag import extract_parameter_fields
from diagnostic_platform.schemas import (
    BuildStepEvidenceResponse,
    EvidenceChainNodeTrace,
    EvidenceChainReport,
    EvidenceFilterDecision,
    EvidenceUnit,
    FlowNode,
    FlowStepPlan,
    NodeEvidenceBundle,
    NodeIntent,
    QueryPlan,
    QuerySpec,
    ReferenceHop,
    TaskEvidenceCandidate,
)


ECU_PREFIXES = ("ADCU", "ASDM", "BCM", "CMD", "CMP", "ECM", "FAH", "HCML", "HCMR", "TCAM", "WPC", "ZCUD")
REFERENCE_PATTERNS = [
    re.compile(r"\b(?:refer|refers|reference)\s+to\s+(?:the\s+)?(.{3,180}?)(?:[.;]|\n|$)", re.IGNORECASE),
    re.compile(r"\b(?:detail|details)\s+(?:can\s+)?refer\s+to\s+(?:the\s+)?(.{3,180}?)(?:[.;]|\n|$)", re.IGNORECASE),
    re.compile(r"\bcan\s+refer\s+to\s+(?:the\s+)?(.{3,180}?)(?:[.;]|\n|$)", re.IGNORECASE),
]


def build_query_plan(node: FlowNode) -> QueryPlan:
    """Build generic typed retrieval queries for one flow node."""

    intent = parse_node_intent(node)
    queries: list[QuerySpec] = []

    def add(query_type: str, parts: Iterable[str]) -> None:
        query = _clean_query(" ".join(part for part in parts if part))
        if query and query not in {item.query for item in queries}:
            queries.append(QuerySpec(query_type=query_type, query=query))

    add("anchor", [node.name, node.template_name, node.raw, node.name.replace("_", " "), node.template_name.replace("_", " ")])
    add("ecu_doc", [intent.target_ecu, "TREG Parameter", intent.operation_phrase, intent.type_hint])
    add("ecu_doc", [intent.target_ecu, intent.operation_family, intent.type_hint])
    add("public_process", ["EOL process", intent.operation_phrase, intent.type_hint, *intent.qualifiers])
    add("public_process", [intent.operation_phrase, intent.type_hint, "flow"])
    add("template", [node.template_name, "XML ScriptNode Args"])

    if not queries:
        add("fallback", [node.name, node.template_name])
    return QueryPlan(node_intent=intent, queries=queries)


def parse_node_intent(node: FlowNode) -> NodeIntent:
    """Parse ECU/action/type hints without binding to specific node families."""

    combined = " ".join([node.name, node.template_name, node.raw]).strip()
    tokens = _tokens(combined)
    target_ecu = _target_ecu(tokens)
    type_hint = _type_hint(combined)
    operation_tokens = _operation_tokens(node.name, node.template_name, target_ecu, type_hint)
    operation_phrase = " ".join(operation_tokens)
    if not operation_phrase:
        operation_phrase = node.name.replace("_", " ")
    qualifiers = _parenthetical_values(combined)
    return NodeIntent(
        node_name=node.name,
        template_name=node.template_name,
        target_ecu=target_ecu,
        operation_phrase=operation_phrase,
        operation_family=operation_phrase,
        type_hint=type_hint,
        qualifiers=qualifiers,
        raw_tokens=tokens,
    )


def extract_reference_texts(evidence: EvidenceUnit) -> list[str]:
    """Extract refer-to text from metadata and full content."""

    values: list[str] = []
    for key in ("reference_text", "REFERENCE_TEXT", "flowchart_title", "FLOWCHART_TITLE"):
        value = evidence.metadata.get(key)
        if isinstance(value, str) and value.strip():
            values.append(_clean_reference(value))
    fields = evidence.metadata.get("table_fields")
    if isinstance(fields, dict):
        for key in ("REFERENCE_TEXT", "FLOWCHART_TITLE"):
            value = fields.get(key)
            if isinstance(value, str) and value.strip():
                values.append(_clean_reference(value))
    text = _strip_html(evidence.content)
    for pattern in REFERENCE_PATTERNS:
        for match in pattern.finditer(text):
            value = _clean_reference(match.group(1))
            if _looks_like_reference(value):
                values.append(value)
    return _dedupe(value for value in values if value)


def expand_references(
    *,
    node: FlowNode,
    candidates: list[TaskEvidenceCandidate],
    evidence_by_id: dict[str, EvidenceUnit],
    graph_repository: GraphRepository | None,
    max_depth: int = 3,
    per_hop_limit: int = 5,
) -> tuple[list[TaskEvidenceCandidate], list[ReferenceHop]]:
    """Resolve references from candidate evidence and add resolved evidence candidates."""

    if max_depth <= 0:
        return candidates, []

    expanded = list(candidates)
    seen_evidence = {candidate.evidence.evidence_id for candidate in expanded}
    frontier = list(candidates)
    hops: list[ReferenceHop] = []
    node_intent = parse_node_intent(node)

    for depth in range(1, max_depth + 1):
        next_frontier: list[TaskEvidenceCandidate] = []
        for candidate in frontier:
            for reference_text in extract_reference_texts(candidate.evidence):
                query = _reference_query(reference_text, node_intent)
                resolved = _resolve_reference_evidence(
                    reference_text=reference_text,
                    evidence_by_id=evidence_by_id,
                    graph_repository=graph_repository,
                    limit=per_hop_limit,
                )
                status = "found" if resolved else "not_found"
                if len(resolved) > 1:
                    status = "ambiguous"
                hop = ReferenceHop(
                    hop=depth,
                    from_evidence_id=candidate.evidence.evidence_id,
                    reference_text=reference_text,
                    query=query,
                    resolved_title=_best_title(resolved) if resolved else "not_found",
                    resolved_evidence_ids=[item.evidence_id for item in resolved],
                    status=status,
                    score=1.0 if resolved else 0.0,
                    needs_review=["reference_not_resolved"] if not resolved else (["ambiguous_reference"] if len(resolved) > 1 else []),
                )
                hops.append(hop)
                for evidence in resolved:
                    if evidence.evidence_id in seen_evidence:
                        continue
                    seen_evidence.add(evidence.evidence_id)
                    reference_candidate = _candidate_from_reference(
                        node=node,
                        evidence=evidence,
                        source_candidate=candidate,
                        depth=depth,
                        reference_text=reference_text,
                    )
                    expanded.append(reference_candidate)
                    next_frontier.append(reference_candidate)
        if not next_frontier:
            break
        frontier = next_frontier
    return expanded, hops


def filter_evidence_context(
    *,
    node: FlowNode,
    candidates: list[TaskEvidenceCandidate],
) -> tuple[list[TaskEvidenceCandidate], list[EvidenceFilterDecision]]:
    """Apply generic keep/demote/drop filtering to candidate evidence."""

    target_ecu = parse_node_intent(node).target_ecu
    filtered: list[TaskEvidenceCandidate] = []
    decisions: list[EvidenceFilterDecision] = []
    for candidate in candidates:
        decision = _filter_decision(target_ecu, candidate.evidence)
        decisions.append(decision)
        if decision.decision == "drop":
            continue
        score = max(candidate.score + decision.score_delta, 0.0)
        notes = _dedupe([*candidate.notes, f"filter:{decision.decision}:{decision.reason}"])
        filtered.append(candidate.model_copy(update={"score": score, "notes": notes}))
    filtered.sort(key=lambda item: (-item.score, item.evidence.evidence_id))
    return filtered, decisions


def include_parent_context(
    candidates: list[TaskEvidenceCandidate],
    evidence_by_id: dict[str, EvidenceUnit],
) -> list[TaskEvidenceCandidate]:
    """Include parent section/table and sibling rows when a table row is selected."""

    by_id = {candidate.evidence.evidence_id: candidate for candidate in candidates}
    output = list(candidates)
    for candidate in list(candidates):
        parent_id = str(candidate.evidence.metadata.get("parent_evidence_id") or "")
        parent = _find_parent_evidence(parent_id, evidence_by_id)
        if parent is not None and parent.evidence_id not in by_id:
            parent_candidate = _context_candidate_from_evidence(
                source_candidate=candidate,
                evidence=parent,
                score=max(candidate.score + 0.05, 0.0),
                retrieval_source="parent_context",
                note="included_parent_context",
            )
            by_id[parent.evidence_id] = parent_candidate
            output.append(parent_candidate)
        for sibling in _sibling_table_rows(candidate.evidence, evidence_by_id):
            if sibling.evidence_id in by_id:
                continue
            sibling_candidate = _context_candidate_from_evidence(
                source_candidate=candidate,
                evidence=sibling,
                score=max(candidate.score - 0.02, 0.0),
                retrieval_source="sibling_table_context",
                note="included_sibling_table_row",
            )
            by_id[sibling.evidence_id] = sibling_candidate
            output.append(sibling_candidate)
    output.sort(key=lambda item: (-item.score, item.evidence.evidence_id))
    return output


def build_reference_aware_evidence_bundle(
    *,
    node_bundle: NodeEvidenceBundle,
    flow_node: FlowNode,
    evidence_by_id: dict[str, EvidenceUnit],
    graph_repository: GraphRepository | None = None,
    max_reference_depth: int = 3,
    reference_limit: int = 5,
) -> NodeEvidenceBundle:
    """Attach query plan, reference expansion, filtering, and parent context to a bundle."""

    query_plan = build_query_plan(flow_node)
    expanded, reference_hops = expand_references(
        node=flow_node,
        candidates=node_bundle.candidates,
        evidence_by_id=evidence_by_id,
        graph_repository=graph_repository,
        max_depth=max_reference_depth,
        per_hop_limit=reference_limit,
    )
    with_parent = include_parent_context(expanded, evidence_by_id)
    filtered, filter_decisions = filter_evidence_context(node=flow_node, candidates=with_parent)
    trace = {
        **node_bundle.retrieval_trace,
        "node_intent": query_plan.node_intent.model_dump(mode="json"),
        "query_plan": query_plan.model_dump(mode="json"),
        "reference_hops": [hop.model_dump(mode="json") for hop in reference_hops],
        "filter_decisions": [decision.model_dump(mode="json") for decision in filter_decisions],
        "kept_context": [candidate.evidence.evidence_id for candidate in filtered],
        "demoted_context": [decision.evidence_id for decision in filter_decisions if decision.decision == "demote"],
        "dropped_context": [decision.evidence_id for decision in filter_decisions if decision.decision == "drop"],
    }
    graph_paths = _dedupe([*node_bundle.graph_paths, *(path for candidate in filtered for path in candidate.graph_paths)])
    return node_bundle.model_copy(update={"candidates": filtered, "retrieval_trace": trace, "graph_paths": graph_paths})


def build_evidence_chain_report(
    *,
    flow_plan: FlowStepPlan,
    evidence_response: BuildStepEvidenceResponse,
    content_chars: int = 6000,
) -> EvidenceChainReport:
    """Build a human-readable evidence chain report model."""

    bundle_by_key = {
        (step.step_key, node_bundle.node_name): (step, node_bundle)
        for step in evidence_response.bundles
        for node_bundle in step.node_bundles
    }
    nodes: list[EvidenceChainNodeTrace] = []
    for step in flow_plan.steps:
        for node in sorted(step.parallel_nodes, key=lambda item: item.column):
            _step_bundle, node_bundle = bundle_by_key.get((step.step_key, node.name), (None, None))
            if node_bundle is None:
                nodes.append(_missing_node_trace(step, node))
                continue
            nodes.append(_node_trace(step, node, node_bundle, content_chars))
    resolved = sum(1 for node in nodes if node.flowchart_status == "found")
    with_refer = sum(1 for node in nodes if node.refer_texts)
    kept = sum(1 for node in nodes if node.kept_evidence_ids)
    return EvidenceChainReport(
        source_flow_path=flow_plan.source_path,
        nodes=nodes,
        summary={
            "nodes": len(nodes),
            "nodes_with_refer": with_refer,
            "nodes_with_flowchart": resolved,
            "nodes_with_kept_context": kept,
            "reference_resolution_rate": round(resolved / with_refer, 6) if with_refer else 0.0,
        },
    )


def render_evidence_chain_markdown(report: EvidenceChainReport) -> str:
    """Render evidence chain report in the legacy human-readable format."""

    lines = [
        "# Flow Steps 参数与流程图报告",
        "",
        f"- Source Flow: `{report.source_flow_path}`",
        f"- Nodes: `{report.summary.get('nodes', 0)}`",
        f"- Reference Resolution Rate: `{report.summary.get('reference_resolution_rate', 0.0)}`",
        "",
    ]
    for node in report.nodes:
        lines.extend(
            [
                f"## 第{_chinese_step(node.step_order)}步",
                "",
                f"### {node.node_name} ({node.template_name or 'not_available'})",
                "",
                f"- 动作: `{node.action or 'not_available'}`",
                f"- 匹配类/章节: `{node.matched_section or 'not_available'}`",
                f"- 参数来源 Markdown: `{node.parameter_source_markdown}`",
                f"- 参数来源行: `{node.parameter_source_lines}`",
                f"- 参数来源 PDF: `{node.parameter_source_pdf}`",
                f"- 参数来源页码: `{node.parameter_source_page}`",
                f"- 参数页图片: `{node.parameter_page_image}`",
                "- Refer 信息:",
            ]
        )
        if node.refer_texts:
            for reference_text in node.refer_texts:
                lines.append(f"  - `{reference_text}`")
        else:
            lines.append("  - `not_found`")
        lines.extend(
            [
                f"- 最终流程标题: `{node.final_flow_title}`",
                f"- 流程图状态: `{node.flowchart_status}`",
                f"- 流程图图片: `{node.flowchart_image}`",
                "",
                "#### 参数文字",
                "",
                "```text",
                node.parameter_text or "not_available",
                "```",
                "",
                "#### 参数页截图",
                "",
                _image_markdown(f"{node.node_name} 参数页", node.parameter_page_image),
                "",
                "#### 最终流程图",
                "",
                _image_markdown(f"{node.node_name} 流程图", node.flowchart_image),
                "",
            ]
        )
    return "\n".join(lines)


def _missing_node_trace(step, node: FlowNode) -> EvidenceChainNodeTrace:
    return EvidenceChainNodeTrace(
        step_key=step.step_key,
        step_display_name=step.display_name,
        step_order=step.order,
        node_name=node.name,
        template_name=node.template_name,
        action=parse_node_intent(node).operation_phrase,
    )


def _node_trace(step, node: FlowNode, node_bundle: NodeEvidenceBundle, content_chars: int) -> EvidenceChainNodeTrace:
    trace = node_bundle.retrieval_trace or {}
    decisions = [EvidenceFilterDecision.model_validate(item) for item in trace.get("filter_decisions") or []]
    hops = [ReferenceHop.model_validate(item) for item in trace.get("reference_hops") or []]
    primary = _primary_parameter_candidate(node_bundle)
    flowchart = _primary_flowchart_candidate(node_bundle, hops)
    refs_raw = [hop.reference_text for hop in hops if hop.reference_text]
    for candidate in node_bundle.candidates:
        refs_raw.extend(extract_reference_texts(candidate.evidence))
    refs = _dedupe(refs_raw)
    return EvidenceChainNodeTrace(
        step_key=step.step_key,
        step_display_name=step.display_name,
        step_order=step.order,
        node_name=node.name,
        template_name=node.template_name,
        action=parse_node_intent(node).operation_phrase,
        matched_section=_section_title(primary) if primary else "not_available",
        parameter_source_markdown=_source_path(primary) if primary else "not_available",
        parameter_source_lines=_source_lines(primary) if primary else "not_available",
        parameter_source_pdf=_pdf_path(primary) if primary else "not_available",
        parameter_source_page=_page(primary) if primary else "not_available",
        parameter_page_image=_image_path(primary) if primary else "not_available",
        refer_texts=refs,
        final_flow_title=_flow_title(flowchart, hops),
        flowchart_status="found" if flowchart or any(hop.status in {"found", "ambiguous"} for hop in hops) else "not_found",
        flowchart_image=_image_path(flowchart) if flowchart else "not_available",
        parameter_text=_clip(primary.evidence.content, content_chars) if primary else "not_available",
        reference_hops=hops,
        filter_decisions=decisions,
        kept_evidence_ids=list(trace.get("kept_context") or []),
        demoted_evidence_ids=list(trace.get("demoted_context") or []),
        dropped_evidence_ids=list(trace.get("dropped_context") or []),
    )


def _primary_parameter_candidate(node_bundle: NodeEvidenceBundle) -> TaskEvidenceCandidate | None:
    for candidate in node_bundle.candidates:
        module = str(candidate.evidence.metadata.get("module") or "")
        if module == "ecu_parameter" and candidate.retrieval_source == "parent_context" and candidate.block_role in {"section_text", "table"}:
            return candidate
    for role in ["section_text", "table"]:
        for candidate in node_bundle.candidates:
            module = str(candidate.evidence.metadata.get("module") or "")
            if module == "ecu_parameter" and candidate.block_role == role:
                return candidate
    for candidate in node_bundle.candidates:
        module = str(candidate.evidence.metadata.get("module") or "")
        if module == "ecu_parameter" and candidate.block_role == "table_row":
            return candidate
    for candidate in node_bundle.candidates:
        if candidate.retrieval_source == "parent_context" and candidate.block_role in {"section_text", "table"}:
            return candidate
    for role in ["section_text", "table", "table_row"]:
        for candidate in node_bundle.candidates:
            if candidate.block_role == role:
                return candidate
    return node_bundle.candidates[0] if node_bundle.candidates else None


def _primary_flowchart_candidate(node_bundle: NodeEvidenceBundle, hops: list[ReferenceHop]) -> TaskEvidenceCandidate | None:
    resolved = {evidence_id for hop in hops for evidence_id in hop.resolved_evidence_ids}
    for candidate in node_bundle.candidates:
        if candidate.evidence.evidence_id in resolved and candidate.block_role in {"flowchart_title", "flowchart_image"}:
            return candidate
    for candidate in node_bundle.candidates:
        if candidate.block_role in {"flowchart_title", "flowchart_image"} or str(candidate.evidence.metadata.get("module") or "") == "eol_process":
            return candidate
    return None


def _filter_decision(target_ecu: str, evidence: EvidenceUnit) -> EvidenceFilterDecision:
    module = str(evidence.metadata.get("module") or "")
    evidence_ecu = str(evidence.metadata.get("ecu") or "")
    title = str(evidence.metadata.get("section_title") or "")
    text = " ".join([title, _strip_html(evidence.content[:500])])
    local_ecus = _local_ecu_mentions(text)
    trace = {"target_ecu": target_ecu, "evidence_ecu": evidence_ecu, "module": module}
    if _is_noise(evidence):
        return EvidenceFilterDecision(evidence_id=evidence.evidence_id, decision="drop", reason="noise", score_delta=-100.0, trace=trace)
    if module == "eol_process":
        return EvidenceFilterDecision(evidence_id=evidence.evidence_id, decision="keep", reason="public_process_keep", trace=trace)
    if target_ecu and evidence_ecu and not _ecu_compatible(evidence_ecu, target_ecu) and module == "ecu_parameter":
        return EvidenceFilterDecision(evidence_id=evidence.evidence_id, decision="drop", reason="explicit_ecu_conflict", score_delta=-100.0, trace=trace)
    conflicting = [value for value in local_ecus if target_ecu and not _ecu_compatible(value, target_ecu)]
    if conflicting and _ecu_compatible(evidence_ecu, target_ecu):
        trace["conflicting_local_ecus"] = conflicting
        return EvidenceFilterDecision(evidence_id=evidence.evidence_id, decision="demote", reason="local_ecu_context_conflict", score_delta=-20.0, trace=trace)
    return EvidenceFilterDecision(evidence_id=evidence.evidence_id, decision="keep", reason="generic_keep", trace=trace)


def _resolve_reference_evidence(
    *,
    reference_text: str,
    evidence_by_id: dict[str, EvidenceUnit],
    graph_repository: GraphRepository | None,
    limit: int,
) -> list[EvidenceUnit]:
    resolved: list[EvidenceUnit] = []
    if graph_repository is not None:
        for resolution in graph_repository.resolve_references(reference_text, limit=limit):
            if isinstance(graph_repository, LocalGraphRepository):
                for node_id in resolution.resolved_node_ids:
                    node = graph_repository.nodes_by_id.get(node_id)
                    if node is None:
                        continue
                    for evidence_id in node.evidence_ids:
                        evidence = evidence_by_id.get(evidence_id)
                        if evidence and evidence.evidence_id not in {item.evidence_id for item in resolved}:
                            resolved.append(evidence)
                            if len(resolved) >= limit:
                                return resolved
    if resolved:
        return resolved[:limit]

    reference_tokens = set(_tokens(reference_text))
    scored: list[tuple[EvidenceUnit, float]] = []
    for evidence in evidence_by_id.values():
        title = str(evidence.metadata.get("section_title") or evidence.metadata.get("flowchart_title") or "")
        haystack = " ".join([title, evidence.content[:1000]])
        tokens = set(_tokens(haystack))
        if not reference_tokens or not tokens:
            continue
        overlap = len(reference_tokens.intersection(tokens)) / max(len(reference_tokens), 1)
        if overlap >= 0.45:
            role = str(evidence.metadata.get("block_role") or evidence.evidence_type)
            module = str(evidence.metadata.get("module") or "")
            if role in {"flowchart_title", "flowchart_image"} or module == "eol_process":
                overlap += 0.2
            scored.append((evidence, overlap))
    scored.sort(key=lambda item: (-item[1], item[0].evidence_id))
    return [item[0] for item in scored[:limit]]


def _candidate_from_reference(
    *,
    node: FlowNode,
    evidence: EvidenceUnit,
    source_candidate: TaskEvidenceCandidate,
    depth: int,
    reference_text: str,
) -> TaskEvidenceCandidate:
    fields = extract_parameter_fields(evidence, [reference_text])
    role = str(evidence.metadata.get("block_role") or evidence.evidence_type)
    return TaskEvidenceCandidate(
        evidence=evidence,
        score=max(source_candidate.score - depth, 0.1),
        matched_terms=[reference_text],
        graph_paths=[
            f"FlowTask:{node.name} --reference_expanded--> Evidence:{evidence.evidence_id}",
            f"Evidence:{source_candidate.evidence.evidence_id} --has_reference--> ReferenceText:{reference_text}",
        ],
        score_breakdown={"reference_expansion": 1.0, "source_score": source_candidate.score},
        provenance_refs=list(source_candidate.provenance_refs),
        raw_source_path=evidence.source_path or "",
        block_role=role,
        parameter_fields=fields,
        flowchart_title=str(fields.get("FLOWCHART_TITLE") or evidence.metadata.get("flowchart_title") or ""),
        template_match=node.template_name,
        retrieval_source="reference_expansion",
        retrieval_rank=source_candidate.retrieval_rank,
        retrieval_score=source_candidate.retrieval_score,
        retrieval_metadata={"reference_text": reference_text, "source_evidence_id": source_candidate.evidence.evidence_id},
        notes=["reference_expansion"],
    )


def _context_candidate_from_evidence(
    *,
    source_candidate: TaskEvidenceCandidate,
    evidence: EvidenceUnit,
    score: float,
    retrieval_source: str,
    note: str,
) -> TaskEvidenceCandidate:
    return source_candidate.model_copy(
        update={
            "evidence": evidence,
            "score": score,
            "block_role": str(evidence.metadata.get("block_role") or evidence.evidence_type),
            "parameter_fields": extract_parameter_fields(evidence, source_candidate.matched_terms),
            "retrieval_source": retrieval_source,
            "retrieval_rank": source_candidate.retrieval_rank,
            "notes": _dedupe([*source_candidate.notes, note]),
        }
    )


def _find_parent_evidence(parent_id: str, evidence_by_id: dict[str, EvidenceUnit]) -> EvidenceUnit | None:
    if not parent_id:
        return None
    for candidate_id in _parent_id_variants(parent_id):
        evidence = evidence_by_id.get(candidate_id)
        if evidence is not None:
            return evidence
    return None


def _sibling_table_rows(evidence: EvidenceUnit, evidence_by_id: dict[str, EvidenceUnit]) -> list[EvidenceUnit]:
    parent_id = str(evidence.metadata.get("parent_evidence_id") or "")
    if not parent_id:
        return []
    parent_key = _normalize_evidence_id(parent_id)
    siblings = [
        item
        for item in evidence_by_id.values()
        if item.evidence_id != evidence.evidence_id
        and str(item.metadata.get("block_role") or item.evidence_type) == "table_row"
        and _normalize_evidence_id(str(item.metadata.get("parent_evidence_id") or "")) == parent_key
    ]
    siblings.sort(key=lambda item: (int(item.metadata.get("row_index") or 0), item.evidence_id))
    return siblings[:80]


def _parent_id_variants(parent_id: str) -> list[str]:
    value = parent_id.strip()
    if not value:
        return []
    variants = [value]
    stripped = value.removeprefix("md_")
    variants.append(stripped)
    variants.append(f"md_{stripped}")
    return _dedupe(variants)


def _normalize_evidence_id(value: str) -> str:
    return value.strip().removeprefix("md_")


def _target_ecu(tokens: list[str]) -> str:
    for token in tokens:
        upper = token.upper()
        if upper.startswith("GEEA"):
            continue
        if upper in ECU_PREFIXES or any(upper.startswith(prefix) for prefix in ECU_PREFIXES):
            return upper
    return ""


def _type_hint(value: str) -> str:
    match = re.search(r"\bType\s*_?\s*(\d+)\b", value, flags=re.IGNORECASE)
    return f"Type{match.group(1)}" if match else ""


def _operation_tokens(node_name: str, template_name: str, target_ecu: str, type_hint: str) -> list[str]:
    value = " ".join([node_name, template_name]).strip()
    value = re.sub(r"\([^)]*\)", " ", value)
    parts = [part for part in re.split(r"[_\s-]+", value) if part]
    ignored = {target_ecu.upper(), type_hint.upper(), "GEEA30", "GEEA", "30"}
    result: list[str] = []
    for part in parts:
        upper = part.upper()
        if upper.isdigit():
            continue
        if upper in ignored or upper.startswith("GEEA"):
            continue
        if upper in ECU_PREFIXES or any(upper.startswith(prefix) for prefix in ECU_PREFIXES):
            continue
        if re.fullmatch(r"Type\d+", part, flags=re.IGNORECASE):
            continue
        if upper in {item.upper() for item in result}:
            continue
        result.append(part)
    return result[:6]


def _parenthetical_values(value: str) -> list[str]:
    return _dedupe(match.strip() for match in re.findall(r"\(([^)]{3,120})\)", value) if match.strip())


def _reference_query(reference_text: str, intent: NodeIntent) -> str:
    return _clean_query(" ".join(["EOL process", reference_text, intent.target_ecu]))


def _best_title(evidence_items: list[EvidenceUnit]) -> str:
    for evidence in evidence_items:
        title = str(evidence.metadata.get("section_title") or evidence.metadata.get("flowchart_title") or "")
        if title:
            return title
    return evidence_items[0].evidence_id if evidence_items else "not_found"


def _section_title(candidate: TaskEvidenceCandidate | None) -> str:
    if candidate is None:
        return "not_available"
    return str(candidate.evidence.metadata.get("section_title") or candidate.flowchart_title or candidate.evidence.evidence_id)


def _source_path(candidate: TaskEvidenceCandidate | None) -> str:
    return str(candidate.evidence.source_path or "not_available") if candidate else "not_available"


def _source_lines(candidate: TaskEvidenceCandidate | None) -> str:
    if candidate is None:
        return "not_available"
    start = candidate.evidence.metadata.get("line_start")
    end = candidate.evidence.metadata.get("line_end")
    if start and end:
        return f"{start}-{end}"
    return "not_available"


def _pdf_path(candidate: TaskEvidenceCandidate | None) -> str:
    if candidate is None or not candidate.evidence.source_path:
        return "not_available"
    path = Path(candidate.evidence.source_path)
    return str(path.with_suffix(".pdf")) if path.suffix.lower() == ".md" else "not_available"


def _page(candidate: TaskEvidenceCandidate | None) -> str:
    if candidate is None:
        return "not_available"
    page = candidate.evidence.page_idx
    return str(page + 1 if page >= 0 else "not_available")


def _image_path(candidate: TaskEvidenceCandidate | None) -> str:
    if candidate is None:
        return "not_available"
    for key in ("img_path", "image_path", "page_image"):
        value = candidate.evidence.metadata.get(key)
        if isinstance(value, str) and value:
            return value
    source_blocks = candidate.evidence.metadata.get("source_blocks_json")
    if isinstance(source_blocks, str) and "img_path" in source_blocks:
        match = re.search(r'"img_path"\s*:\s*"([^"]+)"', source_blocks)
        if match and match.group(1):
            return match.group(1)
    return "not_available"


def _flow_title(candidate: TaskEvidenceCandidate | None, hops: list[ReferenceHop]) -> str:
    if candidate and _section_title(candidate) != "not_available":
        return _section_title(candidate)
    for hop in hops:
        if hop.resolved_title and hop.resolved_title != "not_found":
            return hop.resolved_title
    return "not_found"


def _image_markdown(label: str, path: str) -> str:
    if not path or path in {"not_available", "not_found", "not_applicable"}:
        return "`not_available`"
    return f"![{label}]({path})"


def _chinese_step(value: int) -> str:
    digits = "零一二三四五六七八九"
    if 0 <= value < 10:
        return digits[value]
    if value == 10:
        return "十"
    if 10 < value < 20:
        return "十" + digits[value % 10]
    return str(value)


def _is_noise(evidence: EvidenceUnit) -> bool:
    text = _strip_html(evidence.content)
    normalized = " ".join(_tokens(text[:1000]))
    title = str(evidence.metadata.get("section_title") or "").lower()
    if not text.strip():
        return True
    if "contents" in normalized and len(text) > 3000:
        return True
    if "revision history" in title or "requirement log" in title:
        return True
    return False


def _local_ecu_mentions(text: str) -> list[str]:
    values: list[str] = []
    for match in re.finditer(r"\b(?:ADCU|ASDM|BCM|CMD|CMP|ECM|FAH|HCML|HCMR|TCAM|WPC|ZCUD)[A-Z0-9-]*\b", text, flags=re.IGNORECASE):
        value = match.group(0).upper()
        if value not in values:
            values.append(value)
    return values


def _ecu_compatible(evidence_ecu: str, target_ecu: str) -> bool:
    evidence = evidence_ecu.upper()
    target = target_ecu.upper()
    if not evidence or not target:
        return False
    return evidence == target or evidence.startswith(target) or target in set(_tokens(evidence))


def _tokens(value: str) -> list[str]:
    return [token.lower().removeprefix("0x") for token in re.findall(r"0x[0-9a-fA-F]+|[A-Za-z]+[A-Za-z0-9-]*|[0-9a-fA-F]{2,8}", value) if token]


def _clean_query(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("_", " ")).strip()


def _clean_reference(value: str) -> str:
    value = _strip_html(value)
    value = re.sub(r"\s+", " ", value)
    value = value.strip(" :：,，-")
    return value[:160].strip()


def _looks_like_reference(value: str) -> bool:
    tokens = set(_tokens(value))
    return bool(tokens.intersection({"eol", "process", "flow", "control", "routine", "dtc", "did", "write", "read", "security", "type", "ecu", "treg", "parameter", "list"}))


def _strip_html(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value or "")


def _dedupe(values: Iterable) -> list:
    result: list = []
    for value in values:
        if not value:
            continue
        if value not in result:
            result.append(value)
    return result


def _clip(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: max(limit - 3, 0)].rstrip() + "..."
