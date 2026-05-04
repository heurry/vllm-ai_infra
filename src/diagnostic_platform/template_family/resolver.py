"""Resolve flow nodes and evidence packs to TemplateFamily entries."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Iterable

from diagnostic_platform.schemas import (
    FlowNode,
    NodeEvidenceBundle,
    TemplateFamilyEntry,
    TemplateFamilyRegistry,
    TemplateFamilyResolution,
)
from diagnostic_platform.template_family.xml_signature import family_id_from_class_name, normalize_alias


def load_template_family_registry(value: TemplateFamilyRegistry | str | Path) -> TemplateFamilyRegistry:
    """Load a TemplateFamilyRegistry from an object or JSON path."""

    if isinstance(value, TemplateFamilyRegistry):
        return value
    payload = json.loads(Path(value).read_text(encoding="utf-8"))
    return TemplateFamilyRegistry.model_validate(payload)


def resolve_template_family(
    *,
    node: FlowNode | None = None,
    node_bundle: NodeEvidenceBundle | None = None,
    registry: TemplateFamilyRegistry | str | Path,
    evidence_texts: Iterable[str] = (),
) -> TemplateFamilyResolution:
    """Resolve one flow node to the best matching template family."""

    loaded = load_template_family_registry(registry)
    node_name = node.name if node is not None else (node_bundle.node_name if node_bundle is not None else "")
    template_name = node.template_name if node is not None else (node_bundle.template_name if node_bundle is not None else "")
    texts = list(evidence_texts)
    if node_bundle is not None:
        texts.extend(candidate.evidence.content for candidate in node_bundle.candidates[:8])
        texts.extend(match.evidence.content for match in node_bundle.matches[:8])
    scored: list[tuple[float, TemplateFamilyEntry, list[str]]] = []
    for family in loaded.families:
        score, reasons = _score_family(
            family=family,
            node_name=node_name,
            template_name=template_name,
            evidence_texts=texts,
        )
        if score > 0:
            scored.append((score, family, reasons))
    if not scored:
        return TemplateFamilyResolution(node_name=node_name, template_name=template_name, status="not_found")
    score, family, reasons = sorted(scored, key=lambda item: (-item[0], item[1].family_id))[0]
    status = "found" if score >= 50 else "ambiguous"
    return TemplateFamilyResolution(
        node_name=node_name,
        template_name=template_name,
        family_id=family.family_id,
        score=round(score, 6),
        status=status,
        match_reasons=reasons,
        family=family,
    )


def _score_family(
    *,
    family: TemplateFamilyEntry,
    node_name: str,
    template_name: str,
    evidence_texts: list[str],
) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    if template_name in family.template_class_names:
        score += 120.0
        reasons.append("exact_template_class")
    generalized = family_id_from_class_name(template_name)
    if generalized == family.family_id:
        score += 90.0
        reasons.append("generalized_template_family")
    node_generalized = family_id_from_class_name(node_name)
    if node_generalized == family.family_id:
        score += 45.0
        reasons.append("generalized_node_family")

    query_aliases = [node_name, template_name, node_name.replace("_", " "), template_name.replace("_", " ")]
    for alias in family.aliases:
        alias_norm = normalize_alias(alias)
        if not alias_norm:
            continue
        for query in query_aliases:
            query_norm = normalize_alias(query)
            if query_norm and alias_norm == query_norm:
                score += 30.0
                reasons.append(f"alias_exact:{alias}")
                break
            overlap = _token_overlap(alias_norm, query_norm)
            if overlap >= 0.65:
                score += overlap * 15.0
                reasons.append(f"alias_overlap:{alias}:{overlap:.2f}")
                break

    evidence_blob = normalize_alias("\n".join(evidence_texts[:20]))
    for flowchart in family.flowcharts:
        title_norm = normalize_alias(flowchart.title)
        if title_norm and title_norm in evidence_blob:
            score += 25.0
            reasons.append(f"evidence_refer_title:{flowchart.title}")
        elif _token_overlap(title_norm, evidence_blob) >= 0.55:
            score += 8.0
            reasons.append(f"evidence_flowchart_overlap:{flowchart.title}")

    return score, _dedupe(reasons)


def _token_overlap(left: str, right: str) -> float:
    left_terms = set(re.findall(r"[0-9A-Z]+", left or ""))
    right_terms = set(re.findall(r"[0-9A-Z]+", right or ""))
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / len(left_terms | right_terms)


def _dedupe(values: list[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        if value and value not in output:
            output.append(value)
    return output
