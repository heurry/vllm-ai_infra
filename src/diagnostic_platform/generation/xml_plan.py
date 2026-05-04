"""Generate XML intermediate DSL from Graph RAG evidence bundles."""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
import re

from diagnostic_platform.retrieval.graph_rag import extract_parameter_fields
from diagnostic_platform.schemas import (
    EvidenceMatch,
    EvidenceUnit,
    NodeEvidenceBundle,
    TaskEvidenceCandidate,
    XmlArg,
    XmlGenerationPlan,
    XmlPlanGenerateRequest,
    XmlPlannedNode,
    XmlScriptNodePlan,
    XmlScriptType,
    XmlSerialNodePlan,
)


ECU_KEYS = {"ADCU", "ASDM", "BCM", "CMD", "CMP", "ECM", "FAH", "HCML", "HCMR", "TCAM", "WPC", "ZCUD"}


@dataclass(frozen=True)
class ValueSelection:
    value: str = ""
    evidence_ids: tuple[str, ...] = ()
    score: float = 0.0
    score_breakdown: tuple[tuple[str, float], ...] = ()
    graph_paths: tuple[str, ...] = ()
    kg_paths: tuple[str, ...] = ()
    provenance_refs: tuple[str, ...] = ()
    raw_source_path: str = ""
    needs_review: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateSource:
    evidence: EvidenceUnit
    score: float
    matched_terms: tuple[str, ...]
    fields: dict[str, str]
    graph_paths: tuple[str, ...]
    kg_paths: tuple[str, ...]
    score_breakdown: dict[str, float]
    provenance_refs: tuple[str, ...]
    raw_source_path: str
    needs_review: tuple[str, ...]
    block_role: str
    notes: tuple[str, ...] = ()


def generate_xml_plan(request: XmlPlanGenerateRequest) -> XmlGenerationPlan:
    """Generate deterministic XML DSL from Graph RAG evidence bundles."""

    node_bundles = [
        (step.order, step.row, step.step_key, node_bundle)
        for step in request.evidence_response.bundles
        for node_bundle in step.node_bundles
    ]
    total_nodes = len(node_bundles)

    planned_nodes: list[XmlPlannedNode] = []
    scripts: list[XmlScriptNodePlan] = []
    for index, (order, row, step_key, node_bundle) in enumerate(node_bundles, start=1):
        planned = _plan_node(
            node_bundle=node_bundle,
            step_key=step_key,
            order=order,
            row=row,
            index=index,
            total_nodes=total_nodes,
            request=request,
        )
        planned_nodes.append(planned)
        scripts.append(planned.script)

    serial = XmlSerialNodePlan(name=request.serial_name, scripts=scripts)
    return XmlGenerationPlan(
        source_flow_path=request.evidence_response.source_flow_path,
        serial=serial,
        nodes=planned_nodes,
    )


def _plan_node(
    node_bundle: NodeEvidenceBundle,
    step_key: str,
    order: int,
    row: int,
    index: int,
    total_nodes: int,
    request: XmlPlanGenerateRequest,
) -> XmlPlannedNode:
    class_name = node_bundle.template_name or _safe_class_name(node_bundle.node_name)
    generated_args, missing_fields = _extract_xml_args(node_bundle)
    args = _merge_args(request.default_args, generated_args)
    evidence_ids = _evidence_ids(node_bundle)
    notes = [*node_bundle.notes]
    if not node_bundle.template_name:
        notes.append("template_name_missing_class_name_fallback_used")
    if not evidence_ids:
        missing_fields.append("evidence")
    if not generated_args:
        missing_fields.append("args")

    script = XmlScriptNodePlan(
        node_name=node_bundle.node_name,
        class_name=class_name,
        dead_ms=request.default_dead_ms,
        retry_times=request.default_retry_times,
        script_type=_script_type(index, total_nodes, request.boundary_script_types),
        args=args,
    )
    return XmlPlannedNode(
        step_key=step_key,
        order=order,
        row=node_bundle.row or row,
        column=node_bundle.column,
        node_name=node_bundle.node_name,
        template_name=node_bundle.template_name,
        script=script,
        evidence_ids=evidence_ids,
        missing_fields=_dedupe(missing_fields),
        notes=_dedupe(notes),
    )


def _extract_xml_args(node_bundle: NodeEvidenceBundle) -> tuple[list[XmlArg], list[str]]:
    args: list[XmlArg] = []
    missing: list[str] = []

    sources = _candidate_sources(node_bundle)
    ecu_selection = _select_ecu(node_bundle, sources)
    if ecu_selection.value:
        args.append(_xml_arg_from_selection("EcuBOMName", ecu_selection))
    else:
        missing.append("EcuBOMName")

    selections = _parameter_selections(node_bundle, sources)
    for arg_name in _ordered_arg_names(selections):
        args.append(_xml_arg_from_selection(arg_name, selections[arg_name], width=_hex_width(arg_name)))

    if not selections:
        missing.append("parameter_fields")
    return args, missing


def _candidate_sources(node_bundle: NodeEvidenceBundle) -> list[CandidateSource]:
    if node_bundle.candidates:
        return [_source_from_candidate(candidate) for candidate in node_bundle.candidates]
    return [_source_from_match(match) for match in node_bundle.matches]


def _source_from_candidate(candidate: TaskEvidenceCandidate) -> CandidateSource:
    fields = candidate.parameter_fields or extract_parameter_fields(candidate.evidence, candidate.matched_terms)
    return CandidateSource(
        evidence=candidate.evidence,
        score=candidate.score,
        matched_terms=tuple(candidate.matched_terms),
        fields=fields,
        graph_paths=tuple(candidate.graph_paths),
        kg_paths=tuple(candidate.kg_paths),
        score_breakdown=_numeric_score_breakdown(candidate.score_breakdown),
        provenance_refs=tuple(candidate.provenance_refs),
        raw_source_path=candidate.raw_source_path,
        needs_review=tuple(candidate.needs_review),
        block_role=candidate.block_role,
        notes=tuple(candidate.notes),
    )


def _source_from_match(match: EvidenceMatch) -> CandidateSource:
    fields = extract_parameter_fields(match.evidence, match.matched_terms)
    return CandidateSource(
        evidence=match.evidence,
        score=match.score,
        matched_terms=tuple(match.matched_terms),
        fields=fields,
        graph_paths=(),
        kg_paths=(),
        score_breakdown={"legacy_match_score": match.score},
        provenance_refs=(),
        raw_source_path=match.evidence.source_path or "",
        needs_review=(),
        block_role=str(match.evidence.metadata.get("block_role") or match.evidence.evidence_type),
        notes=("legacy_match_fallback",),
    )


def _select_ecu(node_bundle: NodeEvidenceBundle, sources: list[CandidateSource]) -> ValueSelection:
    node_ecu = _ecu_from_node(node_bundle)
    best: ValueSelection | None = None
    for source in sources:
        ecu = str(source.evidence.metadata.get("ecu") or "").strip()
        if not ecu:
            continue
        if node_ecu and not _ecu_matches_node(ecu, node_ecu):
            continue
        score = source.score + (8.0 if node_ecu else 0.0)
        selection = ValueSelection(
            value=_selected_ecu_value(ecu, node_ecu),
            evidence_ids=(source.evidence.evidence_id,),
            score=score,
            score_breakdown=(("graph_rag_score", source.score),),
            graph_paths=source.graph_paths,
            kg_paths=source.kg_paths,
            provenance_refs=source.provenance_refs,
            raw_source_path=source.raw_source_path,
            needs_review=source.needs_review,
            notes=(f"ecu_from_{source.block_role}",),
        )
        if best is None or (selection.score, selection.value) > (best.score, best.value):
            best = selection

    if best is not None:
        return best
    if node_ecu:
        return ValueSelection(
            value=node_ecu,
            score=12.0,
            score_breakdown=(("node_name_prior", 12.0),),
            notes=("node_ecu_fallback",),
        )
    return ValueSelection()


def _parameter_selections(
    node_bundle: NodeEvidenceBundle,
    sources: list[CandidateSource],
) -> dict[str, ValueSelection]:
    selections: dict[str, ValueSelection] = {}
    node_ecu = _ecu_from_node(node_bundle)
    for source in sources:
        if _source_ecu_conflicts(source, node_ecu):
            continue
        for field_name, raw_value in source.fields.items():
            arg_name = _arg_name_from_field(field_name)
            if not arg_name:
                continue
            if not _arg_allowed_for_template(arg_name, node_bundle.template_name):
                continue
            value = _normalize_arg_value(arg_name, raw_value, node_bundle)
            if not value:
                continue
            score = source.score + _field_priority(arg_name, source.block_role)
            selection = ValueSelection(
                value=value,
                evidence_ids=(source.evidence.evidence_id,),
                score=score,
                score_breakdown=(
                    *tuple(source.score_breakdown.items()),
                    ("field_priority", _field_priority(arg_name, source.block_role)),
                ),
                graph_paths=source.graph_paths,
                kg_paths=source.kg_paths,
                provenance_refs=source.provenance_refs,
                raw_source_path=source.raw_source_path,
                needs_review=source.needs_review,
                notes=(f"field:{field_name}", f"block_role:{source.block_role}", *source.notes),
            )
            current = selections.get(arg_name)
            if current is None or (selection.score, selection.value) > (current.score, current.value):
                selections[arg_name] = selection
    for arg_name, selection in _template_semantic_defaults(node_bundle).items():
        current = selections.get(arg_name)
        if current is None or arg_name in _template_default_override_args(node_bundle):
            selections[arg_name] = selection
    return selections


def _xml_arg_from_selection(name: str, selection: ValueSelection, width: int | None = None) -> XmlArg:
    value = _format_hex(selection.value, width) if width is not None else selection.value
    return XmlArg(
        name=name,
        value=value,
        evidence_ids=list(selection.evidence_ids),
        selection_score=round(selection.score, 6),
        score_breakdown=_numeric_score_breakdown(selection.score_breakdown),
        graph_paths=list(selection.graph_paths),
        kg_paths=list(selection.kg_paths),
        provenance_refs=list(selection.provenance_refs),
        raw_source_path=selection.raw_source_path,
        needs_review=list(selection.needs_review),
        selection_notes=list(selection.notes),
    )


def _numeric_score_breakdown(score_breakdown) -> dict[str, float]:
    if isinstance(score_breakdown, dict):
        items = score_breakdown.items()
    else:
        items = score_breakdown or ()
    normalized: dict[str, float] = {}
    for item in items:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        key, raw_score = item
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            continue
        normalized[str(key)] = round(score, 6)
    return normalized


def _arg_name_from_field(field_name: str) -> str:
    normalized = _normalize_field_key(field_name)
    mapping = {
        "DID": "DID",
        "RID": "RID",
        "DTC": "DTC",
        "SERVICE": "ServiceID",
        "SERVICE_ID": "ServiceID",
        "SERVICEID": "ServiceID",
        "REQUEST_PARAMETER": "RequestParameter",
        "PARAMETER": "RequestParameter",
        "SOURCE": "Source",
        "TARGET": "Target",
        "READ_DID": "ReadDID",
        "DID_FOR_IO_CONTROL_RESULT_CHECK": "ResultCheckDID",
        "EXPECTED_VALUE": "ExpectedValue",
        "DATA_TYPE": "DataType",
        "DATATYPE": "DataType",
        "DATA_INFO": "DataType",
        "DATA_LENGTH": "DataLength",
        "DATA_TYPE_AND_LENGTH": "DataLength",
        "LEN": "DataLength",
        "DESCRIPTION": "Description",
        "TIMEOUT": "Timeout",
        "TIME_OUT": "Timeout",
        "ALLOWED_NRC": "AllowedNRC",
        "WRITTENFLAG": "WrittenFlag",
        "WRITTEN_FLAG": "WrittenFlag",
        "CHECK_METHOD": "CheckMethod",
        "CHECEK_METHOD": "CheckMethod",
        "CHEC_K_METHOD": "CheckMethod",
        "COMMENT": "Comment",
        "REMARK": "Remark",
        "FLOWCHART_TITLE": "FlowchartTitle",
    }
    return mapping.get(normalized, "")


def _normalize_arg_value(arg_name: str, raw_value: str, node_bundle: NodeEvidenceBundle) -> str:
    value = _clean_value(raw_value)
    if not value:
        return ""
    if arg_name == "RequestParameter":
        return _select_request_parameter(value, node_bundle)
    if arg_name in {"DID", "RID", "ReadDID", "ResultCheckDID", "DTC", "ServiceID"}:
        return _identifier_or_raw(value)
    if arg_name == "FlowchartTitle":
        return value
    return value


def _select_request_parameter(value: str, node_bundle: NodeEvidenceBundle) -> str:
    if value.upper() == "NA":
        return "NA"
    direct = re.fullmatch(r"(?:0x)?[0-9A-Fa-f]{2,8}", value.strip())
    if direct:
        return _format_hex(direct.group(0), 2)

    options = _request_parameter_options(value)
    if not options:
        return value

    task_text = f"{node_bundle.node_name} {node_bundle.template_name}".lower().replace("_", " ")
    desired_labels: list[str] = []
    if "usage mode change" in task_text:
        desired_labels = ["active"]
    elif "car mode change" in task_text:
        desired_labels = ["normal"]

    for desired in desired_labels:
        for code, label in options:
            if re.search(rf"\b{re.escape(desired)}\b", label, flags=re.IGNORECASE):
                return _format_hex(code, 2)
    return _format_hex(options[0][0], 2)


def _request_parameter_options(value: str) -> list[tuple[str, str]]:
    markers = list(re.finditer(r"0x[0-9A-Fa-f]{2,8}\s*:", value))
    options: list[tuple[str, str]] = []
    for index, marker in enumerate(markers):
        code = marker.group(0).split(":", 1)[0].strip()
        label_start = marker.end()
        label_end = markers[index + 1].start() if index + 1 < len(markers) else len(value)
        label = value[label_start:label_end].strip(" ;,")
        options.append((code, label))
    return options


def _identifier_or_raw(value: str) -> str:
    identifiers = re.findall(r"\b(?:0x)?[0-9A-Fa-f]{4,8}\b", value)
    if len(identifiers) == 1:
        return identifiers[0]
    return value


def _field_priority(arg_name: str, block_role: str) -> float:
    priority = {
        "DID": 6.0,
        "RID": 6.0,
        "ServiceID": 6.0,
        "RequestParameter": 5.0,
        "Source": 4.0,
        "Target": 4.0,
        "FlowchartTitle": 3.0,
        "ExpectedValue": 2.0,
        "ReadDID": 2.0,
        "ResultCheckDID": 2.0,
    }.get(arg_name, 1.0)
    if block_role == "table_row":
        priority += 2.0
    elif block_role == "section_text":
        priority += 1.0
    return priority


def _ordered_arg_names(selections: dict[str, ValueSelection]) -> list[str]:
    preferred = [
        "DID",
        "RID",
        "ServiceID",
        "RequestParameter",
        "Source",
        "Target",
        "ReadDID",
        "ResultCheckDID",
        "ExpectedValue",
        "FlowchartTitle",
        "Description",
        "DataType",
        "DataLength",
        "Timeout",
        "DTC",
        "CheckMethod",
        "WrittenFlag",
        "AllowedNRC",
        "Comment",
        "Remark",
    ]
    ordered = [name for name in preferred if name in selections]
    ordered.extend(sorted(name for name in selections if name not in ordered))
    return ordered


def _hex_width(arg_name: str) -> int | None:
    return {
        "DID": 4,
        "RID": 4,
        "ReadDID": 4,
        "ResultCheckDID": 4,
        "DTC": 6,
        "RequestParameter": 2,
        "ServiceID": 2,
    }.get(arg_name)


def _merge_args(default_args: list[XmlArg], generated_args: list[XmlArg]) -> list[XmlArg]:
    merged: dict[str, XmlArg] = {}
    order: list[str] = []
    for arg in [*default_args, *generated_args]:
        if arg.name not in merged:
            order.append(arg.name)
        if arg.value is not None or arg.name not in merged:
            merged[arg.name] = arg
    return [merged[name] for name in order]


def _script_type(index: int, total_nodes: int, boundary_script_types: bool) -> XmlScriptType:
    if not boundary_script_types:
        return "NORMAL"
    if total_nodes == 1:
        return "NORMAL"
    if index == 1:
        return "START"
    if index == total_nodes:
        return "END"
    return "NORMAL"


def _evidence_ids(node_bundle: NodeEvidenceBundle) -> list[str]:
    ids: list[str] = []
    for candidate in node_bundle.candidates:
        _append_unique(ids, candidate.evidence.evidence_id)
    for match in node_bundle.matches:
        _append_unique(ids, match.evidence.evidence_id)
    return ids


def _ecu_from_node(node_bundle: NodeEvidenceBundle) -> str:
    for value in [node_bundle.node_name.split("_", 1)[0], node_bundle.template_name.split("_", 1)[0]]:
        if _looks_like_ecu(value):
            return value.upper()
    return ""


def _looks_like_ecu(value: str) -> bool:
    normalized = value.upper()
    if normalized.startswith("GEEA"):
        return False
    return normalized in ECU_KEYS or any(normalized.startswith(prefix) for prefix in ECU_KEYS)


def _ecu_matches_node(value: str, node_ecu: str) -> bool:
    normalized_value = value.upper()
    normalized_node_ecu = node_ecu.upper()
    return (
        normalized_value == normalized_node_ecu
        or normalized_value.startswith(normalized_node_ecu)
        or _normalize_field_key(normalized_node_ecu) in {_normalize_field_key(token) for token in re.split(r"[^0-9A-Za-z]+", normalized_value)}
    )


def _selected_ecu_value(source_ecu: str, node_ecu: str) -> str:
    if not node_ecu:
        return source_ecu
    source_tokens = {_normalize_field_key(token) for token in re.split(r"[^0-9A-Za-z]+", source_ecu) if token}
    normalized_node = _normalize_field_key(node_ecu)
    if normalized_node in source_tokens:
        return node_ecu.upper()
    return source_ecu


def _source_ecu_conflicts(source: CandidateSource, node_ecu: str) -> bool:
    source_ecu = str(source.evidence.metadata.get("ecu") or "").strip()
    return bool(node_ecu and source_ecu and not _ecu_matches_node(source_ecu, node_ecu))


def _template_semantic_defaults(node_bundle: NodeEvidenceBundle) -> dict[str, ValueSelection]:
    template = _normalize_field_key(node_bundle.template_name)
    defaults: dict[str, ValueSelection] = {}
    if template == "DTC_READ_TYPE1":
        defaults["DID"] = _template_default_selection("0x3101", "DTC_Read_Type1 DID semantic default")
    if template.startswith("DTC_READ_TYPE") or template.startswith("DTC_CLEAR_TYPE"):
        defaults["Position"] = _template_default_selection("", f"{node_bundle.template_name} empty Position placeholder")
    if template.startswith("DTC_CLEAR"):
        defaults["ServiceID"] = _template_default_selection("0x19", f"{node_bundle.template_name} UDS service semantic default")
    return defaults


def _arg_allowed_for_template(arg_name: str, template_name: str) -> bool:
    template = _normalize_field_key(template_name)
    if "DTC_READ_TYPE" in template:
        return arg_name == "FlowchartTitle"
    if "DTC_CLEAR_TYPE" in template:
        return arg_name == "FlowchartTitle"
    return True


def _template_default_override_args(node_bundle: NodeEvidenceBundle) -> set[str]:
    template = _normalize_field_key(node_bundle.template_name)
    if template == "DTC_READ_TYPE1":
        return {"DID"}
    return set()


def _template_default_selection(value: str, note: str) -> ValueSelection:
    return ValueSelection(
        value=value,
        score=10.0,
        score_breakdown=(("template_semantic_default", 10.0),),
        needs_review=("semantic_default_without_direct_parameter_row",),
        notes=(note,),
    )


def _safe_class_name(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z_]+", "_", value.strip())
    return value.strip("_") or "GenericDiagnosticScript"


def _format_hex(value: str, width: int | None) -> str:
    if width is None:
        return value
    value = value.strip().upper()
    if value.startswith("0X"):
        value = value[2:]
    if re.fullmatch(r"[0-9A-F]+", value):
        value = value.zfill(width)
        return f"0x{value}"
    return value


def _normalize_field_key(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", value).strip("_").upper()


def _clean_value(value: str) -> str:
    value = unescape(str(value))
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result
