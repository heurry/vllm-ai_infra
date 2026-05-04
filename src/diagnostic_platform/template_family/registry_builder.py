"""Build TemplateFamily registries from TaskNode XML examples and flowchart markdown."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re

from diagnostic_platform.schemas import (
    TemplateFamilyEntry,
    TemplateFamilyRegistry,
    TemplateFamilyRegistryBuildRequest,
    XmlFlowchartSpec,
    XmlTaskNodeSignature,
)
from diagnostic_platform.template_family.xml_signature import (
    extract_tasknode_signature,
    family_id_from_class_name,
    normalize_alias,
)


def build_template_family_registry(request: TemplateFamilyRegistryBuildRequest) -> TemplateFamilyRegistry:
    """Build an offline registry for resolving flow nodes to TaskNode template families."""

    xml_dir = Path(request.xml_template_dir)
    if not xml_dir.exists():
        raise FileNotFoundError(f"XML template directory does not exist: {xml_dir}")
    flowchart_path = Path(request.flowchart_markdown_path) if request.flowchart_markdown_path else None
    flowcharts = _parse_flowchart_markdown(flowchart_path) if flowchart_path and flowchart_path.exists() else []

    grouped_signatures: dict[str, list[XmlTaskNodeSignature]] = defaultdict(list)
    for xml_path in sorted(xml_dir.rglob("*.xml")):
        if _is_macos_metadata(xml_path):
            continue
        signature = extract_tasknode_signature(xml_path)
        if signature is None:
            continue
        grouped_signatures[family_id_from_class_name(signature.class_name)].append(signature)

    families: list[TemplateFamilyEntry] = []
    for family_id, signatures in sorted(grouped_signatures.items()):
        aliases = _family_aliases(family_id, signatures)
        matched_flowcharts = _match_flowcharts(family_id, aliases, flowcharts)
        example_paths = sorted({signature.source_path for signature in signatures})
        families.append(
            TemplateFamilyEntry(
                family_id=family_id,
                aliases=aliases,
                template_class_names=sorted({signature.class_name for signature in signatures}),
                xml_example_paths=example_paths,
                xml_examples=_load_xml_examples(example_paths, request),
                flowcharts=matched_flowcharts,
                signatures=signatures,
                merged_signature=_merge_signatures(family_id, signatures),
                required_evidence=_required_evidence(signatures, matched_flowcharts),
                validation_hints=_validation_hints(signatures),
            )
        )

    registry = _registry_from_families(
        families=families,
        xml_template_dir=str(xml_dir),
        flowchart_markdown_path=str(flowchart_path) if flowchart_path else "",
        flowchart_count=len(flowcharts),
    )
    if request.output_path:
        output_path = Path(request.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(registry.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return registry


def _registry_from_families(
    *,
    families: list[TemplateFamilyEntry],
    xml_template_dir: str,
    flowchart_markdown_path: str,
    flowchart_count: int,
) -> TemplateFamilyRegistry:
    by_family_id = {family.family_id: family for family in families}
    by_class_name: dict[str, str] = {}
    by_alias: dict[str, list[str]] = defaultdict(list)
    for family in families:
        for class_name in family.template_class_names:
            by_class_name[class_name] = family.family_id
        for alias in family.aliases:
            normalized = normalize_alias(alias)
            if normalized and family.family_id not in by_alias[normalized]:
                by_alias[normalized].append(family.family_id)
    return TemplateFamilyRegistry(
        xml_template_dir=xml_template_dir,
        flowchart_markdown_path=flowchart_markdown_path,
        families=families,
        by_family_id=by_family_id,
        by_class_name=dict(sorted(by_class_name.items())),
        by_alias={key: sorted(value) for key, value in sorted(by_alias.items())},
        summary={
            "family_count": len(families),
            "tasknode_template_count": sum(len(family.signatures) for family in families),
            "flowchart_section_count": flowchart_count,
            "families_with_flowcharts": sum(1 for family in families if family.flowcharts),
        },
    )


def _family_aliases(family_id: str, signatures: list[XmlTaskNodeSignature]) -> list[str]:
    aliases = {
        family_id,
        family_id.replace("_", " "),
        *[signature.class_name for signature in signatures],
        *[signature.class_name.replace("_", " ") for signature in signatures],
    }
    for signature in signatures:
        aliases.add(family_id_from_class_name(signature.class_name).replace("_", " "))
    return sorted(alias for alias in aliases if alias)


def _match_flowcharts(
    family_id: str,
    aliases: list[str],
    flowcharts: list[XmlFlowchartSpec],
) -> list[XmlFlowchartSpec]:
    family_terms = [_meaningful_terms(family_id), *[_meaningful_terms(alias) for alias in aliases]]
    family_norm = normalize_alias(family_id)
    family_type = _type_token(family_norm)
    family_operation = _operation_token(family_norm)
    matched: list[tuple[float, XmlFlowchartSpec]] = []
    for flowchart in flowcharts:
        title_norm = normalize_alias(flowchart.title)
        if family_type and family_type not in title_norm:
            continue
        if family_operation and family_operation not in title_norm:
            continue
        title_terms = _meaningful_terms(flowchart.title)
        content_terms = _meaningful_terms(flowchart.content[:1200])
        best_score = 0.0
        for terms in family_terms:
            if not terms:
                continue
            title_overlap = len(terms & title_terms) / len(terms)
            content_overlap = len(terms & content_terms) / len(terms)
            best_score = max(best_score, title_overlap + content_overlap * 0.25)
        if best_score >= 0.55:
            matched.append((best_score, flowchart))
    return [flowchart for _score, flowchart in sorted(matched, key=lambda item: (-item[0], item[1].start_line))[:5]]


def _type_token(value: str) -> str:
    match = re.search(r"\bTYPE\s*\d+\b", value)
    return re.sub(r"\s+", " ", match.group(0)) if match else ""


def _operation_token(value: str) -> str:
    operation_patterns = [
        "DTC CLEAR",
        "DTC READ",
        "IO CONTROL",
        "ROUTINE CONTROL",
        "INFORMATION READ",
        "INFORMATION WRITE",
        "REQUEST OPEN HV BATTERY",
        "VMM CHANGE",
    ]
    for pattern in operation_patterns:
        if pattern in value:
            return pattern
    return ""


def _parse_flowchart_markdown(path: Path | None) -> list[XmlFlowchartSpec]:
    if path is None or not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    headings: list[tuple[int, str]] = []
    heading_pattern = re.compile(r"^\s*(?:#{1,6}\s*)?(?:(?:\d+\.)+\d+\s+)?(.+?Type\d+[^\n]*)\s*$", re.IGNORECASE)
    for index, line in enumerate(lines, start=1):
        match = heading_pattern.match(line)
        if match:
            title = match.group(1).strip()
            if len(title) <= 140:
                headings.append((index, title))
    sections: list[XmlFlowchartSpec] = []
    for offset, (start_line, title) in enumerate(headings):
        end_line = headings[offset + 1][0] - 1 if offset + 1 < len(headings) else len(lines)
        content = "\n".join(lines[start_line - 1 : end_line]).strip()
        flowchart_id = _stable_id(path, start_line, title)
        sections.append(
            XmlFlowchartSpec(
                flowchart_id=flowchart_id,
                title=title,
                normalized_title=normalize_alias(title),
                source_path=str(path),
                start_line=start_line,
                end_line=end_line,
                content=content,
                aliases=[title, re.sub(r"\s+", " ", title.replace("-", " ")).strip()],
            )
        )
    return sections


def _merge_signatures(family_id: str, signatures: list[XmlTaskNodeSignature]) -> XmlTaskNodeSignature:
    create_vars: dict[str, str] = {}
    tag_counts: dict[str, int] = {}
    for signature in signatures:
        create_vars.update(signature.create_vars)
        for tag, count in signature.tag_counts.items():
            tag_counts[tag] = max(tag_counts.get(tag, 0), count)
    return XmlTaskNodeSignature(
        root_tag="TaskNode",
        class_name=family_id,
        source_path=";".join(sorted(signature.source_path for signature in signatures)),
        create_vars=dict(sorted(create_vars.items())),
        arg_names=_union(signature.arg_names for signature in signatures),
        function_names=_union(signature.function_names for signature in signatures),
        assigned_vars=_union(signature.assigned_vars for signature in signatures),
        service_ids=_union(signature.service_ids for signature in signatures),
        positive_responses=_union(signature.positive_responses for signature in signatures),
        request_literals=_union(signature.request_literals for signature in signatures),
        response_literals=_union(signature.response_literals for signature in signatures),
        hex_literals=_union(signature.hex_literals for signature in signatures),
        logic_markers=_union(signature.logic_markers for signature in signatures),
        tag_counts=dict(sorted(tag_counts.items())),
    )


def _required_evidence(signatures: list[XmlTaskNodeSignature], flowcharts: list[XmlFlowchartSpec]) -> list[str]:
    markers = set(_union(signature.logic_markers for signature in signatures))
    service_ids = set(_union(signature.service_ids for signature in signatures))
    evidence = {"template_xml_example"}
    if flowcharts:
        evidence.add("flowchart_description")
    if "clear_dtc_loop" in markers:
        evidence.add("dtc_table")
        evidence.add("dtc_hex_values")
    if "allowed_nrc" in markers:
        evidence.add("nrc_allowed_values")
    if "did_based" in markers:
        evidence.add("did_or_identifier")
    if "request_parameter" in markers:
        evidence.add("request_parameter")
    if "security_access" in markers:
        evidence.add("security_access_level")
    if service_ids:
        evidence.add("uds_service_flow")
    return sorted(evidence)


def _validation_hints(signatures: list[XmlTaskNodeSignature]) -> list[str]:
    markers = set(_union(signature.logic_markers for signature in signatures))
    hints = ["well_formed_tasknode", "contains_mainnodes", "contains_flowsnodes"]
    if "clear_dtc_loop" in markers:
        hints.extend(["dtc_hex_values_must_come_from_evidence", "clear_dtc_count_should_match_evidence_rows"])
    if "allowed_nrc" in markers:
        hints.append("allowed_nrc_values_must_come_from_evidence")
    if "retry_count" in markers:
        hints.append("retry_logic_should_be_preserved")
    return sorted(set(hints))


def _load_xml_examples(paths: list[str], request: TemplateFamilyRegistryBuildRequest) -> list[str]:
    if not request.include_xml_examples:
        return []
    examples: list[str] = []
    for path_text in paths:
        path = Path(path_text)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if request.max_example_chars and len(text) > request.max_example_chars:
            text = text[: request.max_example_chars] + "\n<!-- clipped -->"
        examples.append(text)
    return examples


def _union(values: list[str] | list[list[str]] | object) -> list[str]:
    output: list[str] = []
    for value in values:  # type: ignore[assignment]
        if isinstance(value, list):
            iterable = value
        else:
            iterable = [value]
        for item in iterable:
            text = str(item)
            if text and text not in output:
                output.append(text)
    return sorted(output)


def _meaningful_terms(value: str) -> set[str]:
    normalized = normalize_alias(value)
    stop = {"THE", "EOL", "PROCESS", "FUNCTION", "SINGLE", "ALLOWED", "NO", "RETURN", "CONTROL"}
    return {token for token in normalized.split() if len(token) >= 2 and token not in stop}


def _stable_id(path: Path, start_line: int, title: str) -> str:
    digest = hashlib.sha1(f"{path}:{start_line}:{title}".encode("utf-8")).hexdigest()[:12]
    return f"flowchart_{digest}"


def _is_macos_metadata(path: Path) -> bool:
    return any(part == "__MACOSX" for part in path.parts) or path.name.startswith("._")
