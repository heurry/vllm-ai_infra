"""LLM-assisted XML ScriptNode generation with deterministic guardrails."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Protocol
from xml.etree import ElementTree as ET

from diagnostic_platform.generation.vllm_client import VllmClient, extract_json_object
from diagnostic_platform.retrieval.prompt_budget import (
    PromptBudgetConfig,
    PromptSection,
    budget_text_sections,
    estimate_tokens,
)
from diagnostic_platform.schemas import (
    BaseWorkflowNodeReference,
    ChatMessage,
    EvidenceMatch,
    EvidenceUnit,
    LlmGuardrailCorrection,
    LlmNodeXmlGeneration,
    LlmXmlRepairAttempt,
    NodeEvidenceBundle,
    ReferenceResolution,
    TaskEvidenceCandidate,
    ValidationIssue,
    VllmModelConfig,
    XmlArg,
    XmlGenerationContext,
    XmlGenerationPlan,
    XmlPlannedNode,
    XmlPromptContextItem,
    XmlScriptNodePlan,
    XmlScriptType,
    XmlSerialNodePlan,
    XmlTemplateClassContract,
    XmlTemplateContractRegistry,
    XmlTemplateEntry,
    XmlTemplateExample,
    XmlTemplateRegistry,
    XmlValidationRequest,
)
from diagnostic_platform.serving.profiles import RequestProfile
from diagnostic_platform.serving.router import estimate_prompt_tokens_from_messages
from diagnostic_platform.validation.xml_validator import validate_xml


HIGH_RISK_ARG_NAMES = {"EcuBOMName", "DID", "RID", "ServiceID", "RequestParameter"}
REPAIR_RAW_RESPONSE_MAX_CHARS = 1200
COMMON_ARG_ORDER = [
    "EcuBOMName",
    "SourceAddress",
    "TargetAddress",
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


class TextChatClient(Protocol):
    """Protocol used by tests and the vLLM client."""

    def chat(self, messages, extra_body=None, profile=None) -> str: ...


def _json_response_extra_body(extra_body: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(extra_body or {})
    payload.setdefault("response_format", {"type": "json_object"})
    return payload


def build_xml_generation_context(
    node_bundle: NodeEvidenceBundle,
    template_registry: XmlTemplateRegistry | str | Path | None,
    base_workflow: str | Path | None,
    *,
    contract_registry: XmlTemplateContractRegistry | str | Path | None = None,
    order: int = 0,
    script_type: XmlScriptType = "NORMAL",
    prompt_budget: PromptBudgetConfig | None = None,
    raw_source_dir: str | Path | None = None,
    max_template_examples: int = 3,
    reference_resolutions: list[ReferenceResolution] | None = None,
) -> XmlGenerationContext:
    """Pack one operation's evidence, templates, contracts, and base node for LLM XML generation."""

    registry = _load_template_registry(template_registry)
    contracts = _load_contract_registry(contract_registry)
    base_node = _find_base_workflow_node(base_workflow, node_bundle.node_name)
    class_name = node_bundle.template_name or (base_node.class_name if base_node else "") or _safe_class_name(
        node_bundle.node_name
    )
    template_contract = _contract_for_class(contracts, class_name)
    template_examples = _select_template_examples(
        node_bundle=node_bundle,
        class_name=class_name,
        registry=registry,
        max_examples=max_template_examples,
    )

    context_items, budget_report = _build_prompt_context_items(
        node_bundle=node_bundle,
        prompt_budget=prompt_budget or PromptBudgetConfig(max_prompt_tokens=8192, reserved_output_tokens=1536),
        raw_source_dir=Path(raw_source_dir) if raw_source_dir else None,
    )
    allowed_arg_names = _allowed_arg_names(
        contract=template_contract,
        examples=template_examples,
        base_node=base_node,
        node_bundle=node_bundle,
    )
    graph_paths = _dedupe(
        [
            *node_bundle.graph_paths,
            *(path for candidate in node_bundle.candidates for path in candidate.graph_paths),
        ]
    )
    kg_paths = _dedupe(path for candidate in node_bundle.candidates for path in candidate.kg_paths)
    prompt_context_ids = [
        *[item.context_id for item in context_items],
        *[f"template:{example.template_id}" for example in template_examples],
    ]
    trace = node_bundle.retrieval_trace or {}
    prompt_context_ids.extend(
        f"reference_hop:{index}:{hop.get('status', 'unknown')}"
        for index, hop in enumerate(trace.get("reference_hops") or [], start=1)
    )
    if trace.get("kept_context"):
        prompt_context_ids.append(f"kept_context:{len(trace.get('kept_context') or [])}")
    if trace.get("demoted_context"):
        prompt_context_ids.append(f"demoted_context:{len(trace.get('demoted_context') or [])}")
    if trace.get("dropped_context"):
        prompt_context_ids.append(f"dropped_context:{len(trace.get('dropped_context') or [])}")
    if base_node is not None:
        prompt_context_ids.append(f"base_workflow:{base_node.node_name}")

    return XmlGenerationContext(
        step_key=node_bundle.step_key,
        order=order,
        row=node_bundle.row,
        column=node_bundle.column,
        node_name=node_bundle.node_name,
        template_name=node_bundle.template_name,
        class_name=class_name,
        script_type=script_type,
        relation="operation_level_script_node",
        node_bundle=node_bundle,
        prompt_context_ids=prompt_context_ids,
        context_items=context_items,
        graph_paths=graph_paths,
        kg_paths=kg_paths,
        reference_resolutions=reference_resolutions or [],
        template_examples=template_examples,
        template_contract=template_contract,
        base_workflow_node=base_node,
        allowed_arg_names=allowed_arg_names,
        required_arg_names=list(template_contract.required_args if template_contract else []),
        high_risk_arg_names=sorted(HIGH_RISK_ARG_NAMES),
        prompt_budget_report=budget_report,
    )


def generate_node_xml_with_llm(
    context: XmlGenerationContext,
    vllm_config: VllmModelConfig,
    *,
    client: TextChatClient | None = None,
    request_profile: RequestProfile = "xml_generation",
    extra_body: dict[str, Any] | None = None,
) -> LlmNodeXmlGeneration:
    """Call an OpenAI-compatible vLLM endpoint and parse one operation-level XML generation."""

    llm_client = client or VllmClient(vllm_config, profile=request_profile)
    messages = build_node_xml_generation_messages(context)
    prompt_tokens = estimate_prompt_tokens_from_messages(messages)
    started = time.monotonic()
    raw_response = llm_client.chat(
        messages,
        extra_body=_json_response_extra_body(extra_body),
        profile=request_profile,
    )
    latency_seconds = round(time.monotonic() - started, 6)
    output_tokens = estimate_tokens(raw_response)
    try:
        payload = extract_json_object(raw_response)
    except Exception as exc:  # noqa: BLE001 - malformed model output enters repair/review flow.
        recovered = _generation_from_malformed_response(
            context=context,
            raw_response=raw_response,
            exc=exc,
            prompt_estimated_tokens=prompt_tokens,
            output_estimated_tokens=output_tokens,
            generation_latency_seconds=latency_seconds,
        )
        if recovered is not None:
            return recovered
        return _generation_parse_failed(
            context=context,
            raw_response=raw_response,
            exc=exc,
            prompt_estimated_tokens=prompt_tokens,
            output_estimated_tokens=output_tokens,
            generation_latency_seconds=latency_seconds,
        )
    return _generation_from_payload(
        payload,
        context=context,
        raw_response=raw_response,
        prompt_estimated_tokens=prompt_tokens,
        output_estimated_tokens=output_tokens,
        generation_latency_seconds=latency_seconds,
    )


def validate_and_repair_node_xml(
    generation: LlmNodeXmlGeneration,
    context: XmlGenerationContext,
    *,
    vllm_config: VllmModelConfig | None = None,
    client: TextChatClient | None = None,
    max_repair_attempts: int = 2,
    request_profile: RequestProfile = "xml_repair",
    extra_body: dict[str, Any] | None = None,
) -> LlmNodeXmlGeneration:
    """Validate a generated ScriptNode and optionally ask the LLM to repair bounded errors."""

    checked = _validate_generation(generation, context)
    if checked.valid:
        return checked

    if max_repair_attempts <= 0 or (client is None and vllm_config is None):
        return checked.model_copy(update={"needs_review": True})

    llm_client = client or VllmClient(vllm_config or VllmModelConfig(), profile=request_profile)
    current = checked
    repair_attempts = list(current.repair_attempts)
    repair_latency_seconds = current.repair_latency_seconds
    for attempt in range(1, max_repair_attempts + 1):
        raw_response = ""
        attempt_latency = 0.0
        try:
            started = time.monotonic()
            raw_response = llm_client.chat(
                build_node_xml_repair_messages(context, current),
                extra_body=_json_response_extra_body(extra_body),
                profile=request_profile,
            )
            attempt_latency = round(time.monotonic() - started, 6)
            repair_latency_seconds = round(repair_latency_seconds + attempt_latency, 6)
            repaired_payload = extract_json_object(raw_response)
            repaired = _generation_from_payload(
                repaired_payload,
                context=context,
                raw_response=raw_response,
                prompt_estimated_tokens=current.prompt_estimated_tokens,
                output_estimated_tokens=estimate_tokens(raw_response),
                generation_latency_seconds=current.generation_latency_seconds,
            )
            repaired = repaired.model_copy(
                update={
                    "repair_attempts": repair_attempts,
                    "repair_latency_seconds": repair_latency_seconds,
                }
            )
            repaired = _validate_generation(repaired, context)
            repair_attempts.append(
                LlmXmlRepairAttempt(
                    attempt=attempt,
                    raw_response=raw_response,
                    validation_errors=repaired.xml_validation_errors,
                )
            )
            current = repaired.model_copy(
                update={
                    "repair_attempts": repair_attempts,
                    "repair_latency_seconds": repair_latency_seconds,
                }
            )
            if current.valid:
                return current
        except Exception as exc:  # noqa: BLE001 - keep failed repair trace for audit.
            if not attempt_latency:
                attempt_latency = round(time.monotonic() - started, 6) if "started" in locals() else 0.0
                repair_latency_seconds = round(repair_latency_seconds + attempt_latency, 6)
            issue = ValidationIssue(
                severity="error",
                code="LLM_XML_REPAIR_PARSE_FAILED",
                message=str(exc),
            )
            repair_attempts.append(
                LlmXmlRepairAttempt(
                    attempt=attempt,
                    raw_response=raw_response,
                    validation_errors=[issue],
                )
            )
            current = current.model_copy(
                update={
                    "repair_attempts": repair_attempts,
                    "xml_validation_errors": [*current.xml_validation_errors, issue],
                    "needs_review": True,
                    "repair_latency_seconds": repair_latency_seconds,
                }
            )
    return current.model_copy(
        update={
            "needs_review": True,
            "repair_attempts": repair_attempts,
            "repair_latency_seconds": repair_latency_seconds,
        }
    )


def assemble_llm_xml_plan(
    node_generations: list[LlmNodeXmlGeneration],
    *,
    serial_name: str = "MAC_ALL",
    source_flow_path: str = "",
    boundary_script_types: bool = True,
) -> XmlGenerationPlan:
    """Assemble validated or review-marked operation generations into the standard XML plan DSL."""

    ordered = sorted(node_generations, key=lambda item: (item.order, item.row, item.column, item.node_name))
    scripts: list[XmlScriptNodePlan] = []
    nodes: list[XmlPlannedNode] = []
    total = len(ordered)
    for index, generation in enumerate(ordered, start=1):
        script = generation.script or _script_from_generation_fields(generation)
        if boundary_script_types:
            script = script.model_copy(update={"script_type": _boundary_script_type(index, total)})
        scripts.append(script)
        missing_fields = []
        if not generation.valid:
            missing_fields.append("llm_xml_generation_invalid")
        if generation.needs_review:
            missing_fields.append("needs_review")
        notes = [
            "llm_generated",
            f"model_confidence:{generation.confidence:.3f}",
            *[issue.code for issue in generation.xml_validation_errors],
        ]
        nodes.append(
            XmlPlannedNode(
                step_key=generation.step_key,
                order=generation.order,
                row=generation.row,
                column=generation.column,
                node_name=script.node_name,
                template_name=generation.template_name,
                script=script,
                evidence_ids=_generation_evidence_ids(generation),
                missing_fields=_dedupe(missing_fields),
                notes=_dedupe(notes),
            )
        )
    return XmlGenerationPlan(
        source_flow_path=source_flow_path,
        serial=XmlSerialNodePlan(name=serial_name, scripts=scripts),
        nodes=nodes,
    )


def build_node_xml_generation_messages(context: XmlGenerationContext) -> list[ChatMessage]:
    """Build the stable-prefix generation prompt for one operation."""

    return [
        ChatMessage(role="system", content=_system_prompt()),
        ChatMessage(role="developer", content=_developer_prompt()),
        ChatMessage(role="user", content=_generation_user_prompt(context)),
    ]


def build_node_xml_repair_messages(
    context: XmlGenerationContext,
    generation: LlmNodeXmlGeneration,
) -> list[ChatMessage]:
    """Build a bounded repair prompt using only errors, original output, and relevant context."""

    payload = {
        "task": "repair_invalid_operation_scriptnode_xml",
        "operation": _operation_payload(context),
        "validation_errors": [issue.model_dump(mode="json") for issue in generation.xml_validation_errors],
        "original_output": {
            "node_name": generation.node_name,
            "class_name": generation.class_name,
            "args": [arg.model_dump(mode="json") for arg in generation.args],
            "xml": generation.xml,
            "evidence_map": generation.evidence_map,
            "confidence": generation.confidence,
            "needs_review": generation.needs_review,
            "missing_or_conflict_reason": generation.missing_or_conflict_reason,
            "raw_response": _clip(generation.llm_raw_response, REPAIR_RAW_RESPONSE_MAX_CHARS),
        },
        "evidence_context": _context_payload(context),
        "template_contract": _contract_payload(context.template_contract),
        "base_workflow_node": context.base_workflow_node.model_dump(mode="json") if context.base_workflow_node else None,
        "output_schema": _output_schema_payload(),
    }
    return [
        ChatMessage(role="system", content=_system_prompt()),
        ChatMessage(role="developer", content=_developer_prompt()),
        ChatMessage(
            role="user",
            content=(
                "Repair the JSON/XML once. Do not change valid values unless an error requires it. "
                "Return only one JSON object.\n"
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            ),
        ),
    ]


def _generation_parse_failed(
    *,
    context: XmlGenerationContext,
    raw_response: str,
    exc: Exception,
    prompt_estimated_tokens: int = 0,
    output_estimated_tokens: int = 0,
    generation_latency_seconds: float = 0.0,
) -> LlmNodeXmlGeneration:
    issue = ValidationIssue(
        severity="error",
        code="LLM_XML_JSON_PARSE_FAILED",
        message=f"{context.node_name}: model response was not valid JSON: {exc}",
    )
    return LlmNodeXmlGeneration(
        step_key=context.step_key,
        order=context.order,
        row=context.row,
        column=context.column,
        node_name=context.node_name,
        class_name=context.class_name,
        template_name=context.template_name,
        confidence=0.0,
        needs_review=True,
        missing_or_conflict_reason="model response was not valid JSON",
        prompt_context_ids=context.prompt_context_ids,
        template_examples=[example.template_id for example in context.template_examples],
        llm_raw_response=raw_response,
        xml_validation_errors=[issue],
        valid=False,
        prompt_estimated_tokens=prompt_estimated_tokens,
        output_estimated_tokens=output_estimated_tokens,
        generation_latency_seconds=generation_latency_seconds,
    )


def _generation_from_malformed_response(
    *,
    context: XmlGenerationContext,
    raw_response: str,
    exc: Exception,
    prompt_estimated_tokens: int = 0,
    output_estimated_tokens: int = 0,
    generation_latency_seconds: float = 0.0,
) -> LlmNodeXmlGeneration | None:
    xml = _extract_xml_from_malformed_response(raw_response)
    if not xml:
        return None
    payload = {
        "node_name": _extract_string_field(raw_response, "node_name") or context.node_name,
        "class_name": _extract_string_field(raw_response, "class_name") or context.class_name,
        "args": _extract_args_from_malformed_response(raw_response),
        "xml": xml,
        "evidence_map": _evidence_map_from_malformed_response(raw_response),
        "confidence": _extract_number_field(raw_response, "confidence") or 0.75,
        "needs_review": _extract_bool_field(raw_response, "needs_review", default=False),
        "missing_or_conflict_reason": _extract_string_field(raw_response, "missing_or_conflict_reason") or "",
    }
    generation = _generation_from_payload(
        payload,
        context=context,
        raw_response=raw_response,
        prompt_estimated_tokens=prompt_estimated_tokens,
        output_estimated_tokens=output_estimated_tokens,
        generation_latency_seconds=generation_latency_seconds,
    )
    issue = ValidationIssue(
        severity="warning",
        code="LLM_XML_JSON_RECOVERED",
        message=f"{context.node_name}: recovered ScriptNode XML from malformed JSON: {exc}",
    )
    return generation.model_copy(update={"xml_validation_errors": [issue]})


def _generation_from_payload(
    payload: dict[str, Any],
    *,
    context: XmlGenerationContext,
    raw_response: str,
    prompt_estimated_tokens: int = 0,
    output_estimated_tokens: int = 0,
    generation_latency_seconds: float = 0.0,
) -> LlmNodeXmlGeneration:
    evidence_map = _normalize_evidence_map(payload.get("evidence_map") or payload.get("arg_evidence_map") or {})
    args = _args_from_payload(payload.get("args"), evidence_map)
    confidence = _clamp_float(payload.get("confidence"), default=0.0)
    missing_reason = str(payload.get("missing_or_conflict_reason") or "").strip()
    needs_review = bool(payload.get("needs_review", False)) or bool(missing_reason)
    node_name = str(payload.get("node_name") or context.node_name).strip() or context.node_name
    class_name = str(payload.get("class_name") or context.class_name).strip() or context.class_name
    xml = _normalize_scriptnode_xml(
        _unescape_xml_delimiters(_strip_code_fence(str(payload.get("xml") or ""))),
        node_name=node_name,
        class_name=class_name,
        context=context,
    )
    return LlmNodeXmlGeneration(
        step_key=context.step_key,
        order=context.order,
        row=context.row,
        column=context.column,
        node_name=node_name,
        class_name=class_name,
        template_name=context.template_name,
        args=args,
        xml=xml,
        evidence_map=evidence_map,
        arg_evidence_map=evidence_map,
        confidence=confidence,
        needs_review=needs_review,
        missing_or_conflict_reason=missing_reason,
        prompt_context_ids=context.prompt_context_ids,
        template_examples=[example.template_id for example in context.template_examples],
        llm_raw_response=raw_response,
        valid=False,
        raw_llm_args=args,
        post_guardrail_args=args,
        prompt_estimated_tokens=prompt_estimated_tokens,
        output_estimated_tokens=output_estimated_tokens,
        generation_latency_seconds=generation_latency_seconds,
    )


def _validate_generation(
    generation: LlmNodeXmlGeneration,
    context: XmlGenerationContext,
) -> LlmNodeXmlGeneration:
    generation = _apply_deterministic_high_risk_guardrails(generation, context)
    generation = _enrich_missing_high_risk_evidence(generation, context)
    issues: list[ValidationIssue] = list(generation.xml_validation_errors)
    script: XmlScriptNodePlan | None = None

    if not generation.xml.strip():
        issues.append(
            ValidationIssue(
                severity="error",
                code="LLM_XML_MISSING",
                message=f"{context.node_name}: generated payload does not include xml.",
            )
        )
    else:
        xml_result = validate_xml(
            XmlValidationRequest(
                xml=generation.xml,
                required_arg_names=_required_arg_names_requiring_value(context),
                min_script_nodes=1,
            )
        )
        issues.extend(xml_result.issues)
        try:
            script = _script_plan_from_xml(generation.xml, context, generation)
        except ET.ParseError as exc:
            script = None
            if not any(issue.code == "XML_PARSE_ERROR" for issue in issues):
                issues.append(ValidationIssue(severity="error", code="XML_PARSE_ERROR", message=str(exc)))
        except ValueError as exc:
            script = None
            issues.append(ValidationIssue(severity="error", code="LLM_XML_SCRIPT_NODE_INVALID", message=str(exc)))

    if script is not None:
        _validate_script_against_context(script, generation, context, issues)
    else:
        _validate_payload_args_against_context(generation, context, issues)

    errors = [issue for issue in issues if issue.severity == "error"]
    needs_review = generation.needs_review or any(
        issue.code in {"LLM_XML_BASE_WORKFLOW_CONFLICT", "LLM_XML_LOW_CONFIDENCE"} for issue in issues
    )
    if generation.confidence and generation.confidence < 0.65:
        needs_review = True
        issues.append(
            ValidationIssue(
                severity="warning",
                code="LLM_XML_LOW_CONFIDENCE",
                message=f"{context.node_name}: model confidence below review threshold.",
            )
        )
    return generation.model_copy(
        update={
            "xml_validation_errors": issues,
            "valid": not errors,
            "needs_review": needs_review or bool(errors),
            "script": script,
            "args": script.args if script is not None else generation.args,
            "post_guardrail_args": script.args if script is not None else generation.args,
            "arg_evidence_map": _arg_evidence_map_from_args(script.args if script is not None else generation.args),
        }
    )


def _validate_script_against_context(
    script: XmlScriptNodePlan,
    generation: LlmNodeXmlGeneration,
    context: XmlGenerationContext,
    issues: list[ValidationIssue],
) -> None:
    if script.node_name != context.node_name:
        issues.append(
            ValidationIssue(
                severity="error",
                code="LLM_XML_NODE_NAME_MISMATCH",
                message=f"Generated node_name {script.node_name!r} must match flow node {context.node_name!r}.",
            )
        )

    allowed_classes = _allowed_class_names(context)
    if allowed_classes and script.class_name not in allowed_classes:
        issues.append(
            ValidationIssue(
                severity="error",
                code="LLM_XML_CLASS_NAME_MISMATCH",
                message=(
                    f"Generated ClassName {script.class_name!r} is not in allowed classes: "
                    f"{', '.join(sorted(allowed_classes))}."
                ),
            )
        )

    args_by_name = {arg.name: arg for arg in script.args}
    for required in context.required_arg_names:
        arg = args_by_name.get(required)
        if arg is None:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="LLM_XML_CONTRACT_ARG_MISSING",
                    message=f"{script.node_name}: required contract Arg is missing: {required}.",
                )
            )
        elif not (arg.value or "").strip() and not _arg_allows_empty(context, required):
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="LLM_XML_CONTRACT_ARG_EMPTY",
                    message=f"{script.node_name}: required contract Arg is empty: {required}.",
                )
            )

    allowed_args = set(context.allowed_arg_names)
    for arg in script.args:
        if allowed_args and arg.name not in allowed_args:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="LLM_XML_ARG_NOT_ALLOWED",
                    message=f"{script.node_name}: ArgName {arg.name!r} is not allowed by contract or evidence.",
                )
            )
        _validate_critical_arg_evidence(arg, generation, context, issues)

        _audit_base_workflow_conflicts(script, context, issues)


def _required_arg_names_requiring_value(context: XmlGenerationContext) -> list[str]:
    return [name for name in context.required_arg_names if not _arg_allows_empty(context, name)]


def _arg_allows_empty(context: XmlGenerationContext, arg_name: str) -> bool:
    base = context.base_workflow_node
    if base is not None and arg_name in base.args and not str(base.args.get(arg_name) or "").strip():
        return True
    for example in context.template_examples:
        if arg_name in example.arg_values and not str(example.arg_values.get(arg_name) or "").strip():
            return True
    return False


def _validate_payload_args_against_context(
    generation: LlmNodeXmlGeneration,
    context: XmlGenerationContext,
    issues: list[ValidationIssue],
) -> None:
    allowed_args = set(context.allowed_arg_names)
    for arg in generation.args:
        if allowed_args and arg.name not in allowed_args:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="LLM_XML_ARG_NOT_ALLOWED",
                    message=f"{context.node_name}: ArgName {arg.name!r} is not allowed by contract or evidence.",
                )
            )
        _validate_critical_arg_evidence(arg, generation, context, issues)


def _validate_critical_arg_evidence(
    arg: XmlArg,
    generation: LlmNodeXmlGeneration,
    context: XmlGenerationContext,
    issues: list[ValidationIssue],
) -> None:
    if arg.name not in HIGH_RISK_ARG_NAMES or not (arg.value or "").strip():
        return
    known_ids = _known_evidence_ids(context)
    unknown_ids = [evidence_id for evidence_id in arg.evidence_ids if evidence_id not in known_ids]
    if unknown_ids:
        issues.append(
            ValidationIssue(
                severity="error",
                code="LLM_XML_ARG_EVIDENCE_UNKNOWN",
                message=f"{context.node_name}.{arg.name}: unknown evidence ids: {', '.join(unknown_ids)}.",
            )
        )
    if not arg.evidence_ids and not _fallback_reason_for_arg(generation, arg.name):
        issues.append(
            ValidationIssue(
                severity="error",
                code="LLM_XML_ARG_EVIDENCE_MISSING",
                message=f"{context.node_name}.{arg.name}: high-risk Arg needs evidence_id or fallback reason.",
            )
        )


def _enrich_missing_high_risk_evidence(
    generation: LlmNodeXmlGeneration,
    context: XmlGenerationContext,
) -> LlmNodeXmlGeneration:
    args: list[XmlArg] = []
    evidence_map = {key: list(values) for key, values in generation.arg_evidence_map.items()}
    corrections = list(generation.guardrail_corrections)
    changed = False
    for arg in generation.args:
        if arg.name in HIGH_RISK_ARG_NAMES and (arg.value or "").strip() and not arg.evidence_ids:
            inferred = _infer_arg_evidence_ids(arg, context)
            if inferred:
                corrections.append(
                    LlmGuardrailCorrection(
                        node_name=context.node_name,
                        arg_name=arg.name,
                        raw_value=arg.value,
                        corrected_value=arg.value,
                        raw_evidence_ids=[],
                        corrected_evidence_ids=inferred,
                        correction_reason="missing_high_risk_evidence_id",
                        correction_source="evidence_context",
                    )
                )
                arg = arg.model_copy(
                    update={
                        "evidence_ids": inferred,
                        "selection_notes": _dedupe([*arg.selection_notes, "high_risk_evidence_inferred_from_context"]),
                    }
                )
                evidence_map[arg.name] = inferred
                changed = True
        args.append(arg)
    if not changed:
        return generation
    return generation.model_copy(
        update={
            "args": args,
            "evidence_map": evidence_map,
            "arg_evidence_map": evidence_map,
            "guardrail_corrections": corrections,
        }
    )


def _apply_deterministic_high_risk_guardrails(
    generation: LlmNodeXmlGeneration,
    context: XmlGenerationContext,
) -> LlmNodeXmlGeneration:
    guardrail_args = _deterministic_high_risk_args(context)
    if not guardrail_args:
        return generation

    args_by_name = {arg.name: arg for arg in generation.args}
    changed_args: list[XmlArg] = []
    corrections = list(generation.guardrail_corrections)
    changed = False
    for guardrail in guardrail_args:
        existing = args_by_name.get(guardrail.name)
        if existing is not None and _values_match(existing.value, guardrail.value):
            continue
        corrections.append(
            LlmGuardrailCorrection(
                node_name=context.node_name,
                arg_name=guardrail.name,
                raw_value=existing.value if existing else None,
                corrected_value=guardrail.value,
                raw_evidence_ids=list(existing.evidence_ids) if existing else [],
                corrected_evidence_ids=list(guardrail.evidence_ids),
                correction_reason=(
                    "missing_high_risk_arg_from_llm" if existing is None else "high_risk_arg_value_replaced"
                ),
                correction_source=_guardrail_correction_source(guardrail),
            )
        )
        notes = _dedupe([*guardrail.selection_notes, "deterministic_high_risk_guardrail"])
        changed_args.append(guardrail.model_copy(update={"selection_notes": notes}))
        changed = True

    if not changed:
        return generation

    merged_args = _merge_guardrail_args(generation.args, changed_args)
    evidence_map = {key: list(values) for key, values in generation.arg_evidence_map.items()}
    for arg in changed_args:
        if arg.evidence_ids:
            evidence_map[arg.name] = list(arg.evidence_ids)
    xml = _upsert_scriptnode_args(generation.xml, changed_args)
    return generation.model_copy(
        update={
            "args": merged_args,
            "xml": xml,
            "evidence_map": evidence_map,
            "arg_evidence_map": evidence_map,
            "guardrail_corrections": corrections,
        }
    )


def _guardrail_correction_source(arg: XmlArg) -> str:
    notes = {str(value) for value in [*arg.selection_notes, *arg.needs_review, *arg.score_breakdown.keys()]}
    if any("semantic_default" in value for value in notes):
        return "semantic_default"
    if arg.evidence_ids:
        return "deterministic_plan"
    return "deterministic_plan"


def _deterministic_high_risk_args(context: XmlGenerationContext) -> list[XmlArg]:
    try:
        from diagnostic_platform.generation.xml_plan import _extract_xml_args  # noqa: PLC0415

        args, _missing = _extract_xml_args(context.node_bundle)
    except Exception:  # noqa: BLE001 - guardrail must not make LLM generation unavailable.
        return []
    allowed = set(context.allowed_arg_names) | HIGH_RISK_ARG_NAMES
    output = []
    for arg in args:
        if arg.name not in HIGH_RISK_ARG_NAMES or arg.name not in allowed:
            continue
        if not (arg.value or "").strip():
            continue
        output.append(arg)
    return output


def _merge_guardrail_args(existing: list[XmlArg], guardrail_args: list[XmlArg]) -> list[XmlArg]:
    guardrail_by_name = {arg.name: arg for arg in guardrail_args}
    merged: list[XmlArg] = []
    seen: set[str] = set()
    for arg in existing:
        replacement = guardrail_by_name.get(arg.name)
        if replacement is not None:
            merged.append(replacement)
            seen.add(arg.name)
        else:
            merged.append(arg)
            seen.add(arg.name)
    for arg in guardrail_args:
        if arg.name not in seen:
            merged.append(arg)
    return merged


def _upsert_scriptnode_args(xml: str, args: list[XmlArg]) -> str:
    if not args or not xml.strip():
        return xml
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return xml
    if _local_tag(root.tag) != "ScriptNode":
        return xml
    args_element = next((child for child in root if _local_tag(child.tag) == "Args"), None)
    if args_element is None:
        args_element = ET.SubElement(root, "Args")
    existing = {
        element.get("ArgName"): element
        for element in args_element
        if _local_tag(element.tag) == "Arg" and element.get("ArgName")
    }
    for arg in args:
        element = existing.get(arg.name)
        if element is None:
            element = ET.SubElement(args_element, "Arg", {"ArgName": arg.name})
        element.text = arg.value
    return ET.tostring(root, encoding="unicode")


def _infer_arg_evidence_ids(arg: XmlArg, context: XmlGenerationContext) -> list[str]:
    for item in context.context_items:
        if _arg_matches_context_item(arg, item):
            return [item.evidence_id]
    return []


def _arg_matches_context_item(arg: XmlArg, item: XmlPromptContextItem) -> bool:
    target = str(arg.value or "").strip()
    if not target:
        return False
    for field_name, field_value in item.parameter_fields.items():
        if _arg_name_from_field(field_name) == arg.name and _values_match(target, field_value):
            return True
    if arg.name == "EcuBOMName":
        haystack = " ".join([item.evidence_id, item.source_path, item.content]).upper()
        return target.upper() in haystack
    return False


def _values_match(left: str, right: str) -> bool:
    left_norm = _normalize_value_for_match(left)
    right_norm = _normalize_value_for_match(right)
    if left_norm == right_norm:
        return True
    return _normalize_hex_for_match(left_norm) == _normalize_hex_for_match(right_norm)


def _normalize_value_for_match(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().upper())


def _normalize_hex_for_match(value: str) -> str:
    stripped = value[2:] if value.startswith("0X") else value
    if re.fullmatch(r"[0-9A-F]+", stripped or ""):
        stripped = stripped.lstrip("0") or "0"
        return f"0X{stripped}"
    return value


def _audit_base_workflow_conflicts(
    script: XmlScriptNodePlan,
    context: XmlGenerationContext,
    issues: list[ValidationIssue],
) -> None:
    base = context.base_workflow_node
    if base is None:
        return
    if base.class_name and script.class_name != base.class_name:
        issues.append(
            ValidationIssue(
                severity="warning",
                code="LLM_XML_BASE_WORKFLOW_CONFLICT",
                message=(
                    f"{script.node_name}: generated ClassName {script.class_name!r} differs from "
                    f"base workflow {base.class_name!r}."
                ),
            )
        )
    for arg in script.args:
        base_value = base.args.get(arg.name)
        generated_value = (arg.value or "").strip()
        if base_value is None or not str(base_value).strip() or not generated_value:
            continue
        if str(base_value).strip() != generated_value:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="LLM_XML_BASE_WORKFLOW_CONFLICT",
                    message=(
                        f"{script.node_name}.{arg.name}: generated value {generated_value!r} differs from "
                        f"base workflow {str(base_value).strip()!r}; audit must decide final value."
                    ),
                )
            )


def _normalize_scriptnode_xml(
    xml: str,
    *,
    node_name: str,
    class_name: str,
    context: XmlGenerationContext,
) -> str:
    """Fill deterministic ScriptNode defaults that are also present in structured fields."""

    if not xml.strip():
        return xml
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return xml
    if _local_tag(root.tag) != "ScriptNode":
        return xml

    defaults = {
        "Name": node_name or context.node_name,
        "DeadMS": "0",
        "RetryTimes": "0",
        "ScriptType": context.script_type,
        "InteruptStart": "false",
        "ClassName": class_name or context.class_name,
    }
    for key, value in defaults.items():
        if value and not root.get(key):
            root.set(key, str(value))
    return ET.tostring(root, encoding="unicode")


def _script_plan_from_xml(
    xml: str,
    context: XmlGenerationContext,
    generation: LlmNodeXmlGeneration,
) -> XmlScriptNodePlan:
    root = ET.fromstring(xml)
    if _local_tag(root.tag) != "ScriptNode":
        raise ValueError(f"operation generation root must be ScriptNode, got {_local_tag(root.tag)!r}")
    nested_scripts = [element for element in root.iter() if _local_tag(element.tag) == "ScriptNode"]
    if len(nested_scripts) != 1:
        raise ValueError("operation generation must contain exactly one ScriptNode")
    args = []
    for element in root.iter():
        if _local_tag(element.tag) != "Arg":
            continue
        name = element.get("ArgName") or ""
        if not name:
            continue
        evidence_ids = _evidence_for_arg(generation, name)
        args.append(
            XmlArg(
                name=name,
                value=(element.text if element.text is not None else ""),
                evidence_ids=evidence_ids,
                selection_score=generation.confidence,
                needs_review=["llm_generation_needs_review"] if generation.needs_review else [],
                selection_notes=["llm_xml_generation"],
            )
        )
    return XmlScriptNodePlan(
        node_name=root.get("Name") or generation.node_name or context.node_name,
        class_name=root.get("ClassName") or generation.class_name or context.class_name,
        dead_ms=_int_attr(root, "DeadMS", 0),
        retry_times=_int_attr(root, "RetryTimes", 0),
        script_type=_script_type(root.get("ScriptType") or context.script_type),
        interupt_start=str(root.get("InteruptStart") or "false").lower() == "true",
        expression=root.get("Expression"),
        args=args,
    )


def _script_from_generation_fields(generation: LlmNodeXmlGeneration) -> XmlScriptNodePlan:
    return XmlScriptNodePlan(
        node_name=generation.node_name,
        class_name=generation.class_name,
        args=generation.args,
    )


def _build_prompt_context_items(
    *,
    node_bundle: NodeEvidenceBundle,
    prompt_budget: PromptBudgetConfig,
    raw_source_dir: Path | None,
) -> tuple[list[XmlPromptContextItem], dict[str, Any]]:
    candidates = [_item_from_candidate(index, candidate, node_bundle, raw_source_dir) for index, candidate in enumerate(node_bundle.candidates, start=1)]
    matches = [
        _item_from_match(index, match, raw_source_dir)
        for index, match in enumerate(node_bundle.matches, start=1)
        if match.evidence.evidence_id not in {item.evidence_id for item in candidates}
    ]
    items = candidates or matches
    if candidates:
        items.extend(matches)
    sections = [PromptSection(label=item.context_id, content=item.content, score=item.score) for item in items]
    kept_sections, report = budget_text_sections(sections, config=prompt_budget)
    kept_by_label = {section.label: section for section in kept_sections}
    kept_items: list[XmlPromptContextItem] = []
    for item in items:
        kept = kept_by_label.get(item.context_id)
        if kept is None:
            continue
        kept_items.append(item.model_copy(update={"content": kept.content}))
    return kept_items, _budget_report_dict(report)


def _item_from_candidate(
    index: int,
    candidate: TaskEvidenceCandidate,
    node_bundle: NodeEvidenceBundle,
    raw_source_dir: Path | None,
) -> XmlPromptContextItem:
    evidence = candidate.evidence
    raw_source_path = _save_raw_source(evidence, raw_source_dir)
    score = candidate.score + _context_priority(candidate, node_bundle)
    content = _evidence_prompt_content(
        evidence=evidence,
        matched_terms=candidate.matched_terms,
        parameter_fields=candidate.parameter_fields,
        graph_paths=candidate.graph_paths,
        kg_paths=candidate.kg_paths,
        notes=candidate.notes,
        raw_source_path=raw_source_path,
    )
    return XmlPromptContextItem(
        context_id=f"candidate:{index}:{evidence.evidence_id}",
        kind=candidate.block_role or evidence.evidence_type,
        score=score,
        evidence_id=evidence.evidence_id,
        source_path=evidence.source_path or "",
        page_idx=evidence.page_idx,
        block_role=candidate.block_role,
        parameter_fields={str(key): str(value) for key, value in candidate.parameter_fields.items()},
        graph_paths=candidate.graph_paths,
        kg_paths=candidate.kg_paths,
        provenance_refs=candidate.provenance_refs,
        raw_source_path=raw_source_path,
        retrieval_source=candidate.retrieval_source,
        retrieval_score=candidate.retrieval_score,
        retrieval_metadata=candidate.retrieval_metadata,
        content=content,
    )


def _item_from_match(index: int, match: EvidenceMatch, raw_source_dir: Path | None) -> XmlPromptContextItem:
    evidence = match.evidence
    raw_source_path = _save_raw_source(evidence, raw_source_dir)
    fields = {str(key): str(value) for key, value in evidence.metadata.items() if _arg_name_from_field(str(key))}
    content = _evidence_prompt_content(
        evidence=evidence,
        matched_terms=match.matched_terms,
        parameter_fields=fields,
        graph_paths=[],
        kg_paths=[],
        notes=["legacy_retrieval_match"],
        raw_source_path=raw_source_path,
    )
    return XmlPromptContextItem(
        context_id=f"match:{index}:{evidence.evidence_id}",
        kind=evidence.evidence_type,
        score=match.score,
        evidence_id=evidence.evidence_id,
        source_path=evidence.source_path or "",
        page_idx=evidence.page_idx,
        block_role=str(evidence.metadata.get("block_role") or evidence.evidence_type),
        parameter_fields=fields,
        raw_source_path=raw_source_path,
        content=content,
    )


def _context_priority(candidate: TaskEvidenceCandidate, node_bundle: NodeEvidenceBundle) -> float:
    priority = 0.0
    if candidate.block_role == "table_row":
        priority += 8.0
    if str(candidate.evidence.metadata.get("ecu") or "") and _node_ecu(node_bundle):
        if _node_ecu(node_bundle).upper() in str(candidate.evidence.metadata.get("ecu") or "").upper():
            priority += 6.0
    if candidate.flowchart_title or candidate.parameter_fields.get("FLOWCHART_TITLE"):
        priority += 5.0
    if candidate.kg_paths:
        priority += 3.0
    if candidate.parameter_fields:
        priority += min(len(candidate.parameter_fields), 8)
    return priority


def _evidence_prompt_content(
    *,
    evidence: EvidenceUnit,
    matched_terms: list[str],
    parameter_fields: dict[str, str],
    graph_paths: list[str],
    kg_paths: list[str],
    notes: list[str],
    raw_source_path: str,
) -> str:
    payload = {
        "evidence_id": evidence.evidence_id,
        "type": evidence.evidence_type,
        "doc_id": evidence.doc_id,
        "source_path": evidence.source_path,
        "page_idx": evidence.page_idx,
        "bbox": evidence.bbox,
        "metadata": evidence.metadata,
        "matched_terms": matched_terms,
        "parameter_fields": parameter_fields,
        "graph_paths": graph_paths[:3],
        "kg_paths": kg_paths[:3],
        "raw_source_path": raw_source_path,
        "notes": notes,
        "content": evidence.content.strip(),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _generation_user_prompt(context: XmlGenerationContext) -> str:
    lines = [
        "Generate exactly one operation-level ScriptNode for the operation below.",
        "Return only one JSON object. Do not copy the input text.",
        (
            "Required JSON keys: node_name, class_name, args, xml, evidence_map, "
            "confidence, needs_review, missing_or_conflict_reason."
        ),
        (
            f"operation: step_key={context.step_key}; order={context.order}; row={context.row}; "
            f"column={context.column}; node_name={context.node_name}; template_name={context.template_name}; "
            f"class_name={context.class_name}; script_type={context.script_type}; relation={context.relation}"
        ),
        f"allowed_arg_names: {', '.join(context.allowed_arg_names)}",
        f"required_arg_names: {', '.join(context.required_arg_names)}",
        f"high_risk_arg_names: {', '.join(context.high_risk_arg_names)}",
    ]
    if context.template_contract is not None:
        lines.append(
            "template_contract: "
            f"class_name={context.template_contract.class_name}; "
            f"required_args={', '.join(context.template_contract.required_args)}; "
            f"optional_args={', '.join(context.template_contract.optional_args)}"
        )
    if context.base_workflow_node is not None:
        lines.append(
            "base_workflow_reference: "
            f"node_name={context.base_workflow_node.node_name}; "
            f"class_name={context.base_workflow_node.class_name}; "
            f"args={json.dumps(context.base_workflow_node.args, ensure_ascii=False, separators=(',', ':'))}; "
            f"xml={_clip(_xml_for_prompt(context.base_workflow_node.xml), 700)}"
        )
    if context.template_examples:
        for index, example in enumerate(context.template_examples[:1], start=1):
            lines.append(
                f"template_example_{index}: node_name={example.node_name}; class_name={example.class_name}; "
                f"args={json.dumps(example.arg_values, ensure_ascii=False, separators=(',', ':'))}; "
                f"xml={_clip(_xml_for_prompt(example.xml), 700)}"
            )
    lines.append("evidence_context:")
    for item in context.context_items:
        lines.append(
            "- "
            f"context_id={item.context_id}; evidence_id={item.evidence_id}; kind={item.kind}; "
            f"source_path={item.source_path}; page_idx={item.page_idx}; block_role={item.block_role}; "
            f"retrieval_source={item.retrieval_source}; retrieval_score={item.retrieval_score}; "
            f"parameter_fields={json.dumps(item.parameter_fields, ensure_ascii=False, separators=(',', ':'))}; "
            f"graph_paths={json.dumps(item.graph_paths[:2], ensure_ascii=False, separators=(',', ':'))}; "
            f"raw_source_path={item.raw_source_path}; content={_clip(item.content, 1200)}"
        )
    lines.extend(
        [
            "Output rules:",
            "- args must be a list of {name,value,evidence_ids}.",
            "- xml must be one well-formed ScriptNode string with Args/Arg children.",
            "- Keep xml compact on one line; do not add indentation, tabs, repeated whitespace, or copied formatting gaps.",
            "- Keep the whole JSON compact enough to finish; omit optional low-value args before exceeding the output limit.",
            "- evidence_map must map high-risk ArgName values to supplied evidence_id values.",
            "- Set needs_review=true only when evidence is missing or conflicting.",
        ]
    )
    return "\n".join(lines)


def _system_prompt() -> str:
    return (
        "You generate auditable automotive diagnostic workflow XML from bounded evidence. "
        "Use only the supplied evidence, template examples, base workflow reference, and contract. "
        "Do not invent ECU names, DID/RID/service values, request parameters, or ArgName fields."
    )


def _developer_prompt() -> str:
    return "\n".join(
        [
            "XML rules:",
            "- Output one ScriptNode only, not a full workflow.",
            "- Preserve ScriptNode attributes: Name, DeadMS, RetryTimes, ScriptType, InteruptStart, ClassName.",
            "- The Args element contains Arg children with ArgName attributes.",
            "- ArgName must come from allowed_arg_names or a supplied evidence parameter field.",
            "- For EcuBOMName, DID, RID, ServiceID, and RequestParameter, provide evidence_id in evidence_map.",
            "- If a required value is not present in evidence, leave it empty only when you set needs_review=true and explain why.",
            "- The xml field must be a well-formed XML string inside JSON; do not HTML-escape tag delimiters like <ScriptNode>.",
            "- The xml field must be compact one-line XML; no indentation, no tab padding, no decorative whitespace.",
            "- Return JSON only. No Markdown fences.",
        ]
    )


def _output_schema_payload() -> dict[str, Any]:
    return {
        "node_name": "string; must equal operation.node_name",
        "class_name": "string; must match operation.class_name/template candidate",
        "args": [{"name": "ArgName", "value": "string", "evidence_ids": ["evidence_id"]}],
        "xml": "<?xml version=\"1.0\"?><ScriptNode ...><Args>...</Args></ScriptNode>",
        "evidence_map": {"ArgName": ["evidence_id"]},
        "confidence": "number from 0 to 1",
        "needs_review": "boolean",
        "missing_or_conflict_reason": "string; required when evidence is missing or base workflow conflicts",
    }


def _operation_payload(context: XmlGenerationContext) -> dict[str, Any]:
    return {
        "step_key": context.step_key,
        "order": context.order,
        "row": context.row,
        "column": context.column,
        "node_name": context.node_name,
        "template_name": context.template_name,
        "class_name": context.class_name,
        "script_type": context.script_type,
        "relation": context.relation,
    }


def _context_payload(context: XmlGenerationContext) -> list[dict[str, Any]]:
    return [
        {
            "context_id": item.context_id,
            "kind": item.kind,
            "score": item.score,
            "evidence_id": item.evidence_id,
            "source_path": item.source_path,
            "page_idx": item.page_idx,
            "block_role": item.block_role,
            "parameter_fields": item.parameter_fields,
            "graph_paths": item.graph_paths[:3],
            "kg_paths": item.kg_paths[:3],
            "provenance_refs": item.provenance_refs[:5],
            "raw_source_path": item.raw_source_path,
            "retrieval_source": item.retrieval_source,
            "retrieval_score": item.retrieval_score,
            "content": _clip(item.content, 900),
        }
        for item in context.context_items
    ]


def _contract_payload(contract: XmlTemplateClassContract | None) -> dict[str, Any] | None:
    if contract is None:
        return None
    return {
        "class_name": contract.class_name,
        "template_count": contract.template_count,
        "required_args": contract.required_args,
        "optional_args": contract.optional_args,
        "node_names": contract.node_names[:10],
        "source_paths": contract.source_paths[:10],
    }


def _select_template_examples(
    *,
    node_bundle: NodeEvidenceBundle,
    class_name: str,
    registry: XmlTemplateRegistry | None,
    max_examples: int,
) -> list[XmlTemplateExample]:
    if registry is None:
        return []
    scored: list[tuple[float, XmlTemplateEntry, list[str]]] = []
    for entry in registry.templates:
        score, reasons = _template_match_score(entry, node_bundle, class_name)
        if score <= 0:
            continue
        scored.append((score, entry, reasons))
    examples = []
    for score, entry, reasons in sorted(scored, key=lambda item: (-item[0], item[1].template_id))[:max_examples]:
        xml = entry.xml or _extract_script_xml_from_source(entry)
        examples.append(
            XmlTemplateExample(
                template_id=entry.template_id,
                node_name=entry.node_name,
                class_name=entry.class_name,
                source_path=entry.source_path,
                arg_names=entry.arg_names,
                arg_values=entry.arg_values,
                xml=_clip(xml, 4000),
                match_reasons=reasons,
                similarity_score=round(score, 6),
            )
        )
    return examples


def _template_match_score(
    entry: XmlTemplateEntry,
    node_bundle: NodeEvidenceBundle,
    class_name: str,
) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    if entry.class_name and entry.class_name == class_name:
        score += 100.0
        reasons.append("same_class_name")
        if entry.root_tag == "TaskNode":
            score += 80.0
            reasons.append("same_class_tasknode_template")
    if entry.node_name and entry.node_name == node_bundle.node_name:
        score += 50.0
        reasons.append("same_node_name")
    overlap = _token_overlap(entry.node_name, node_bundle.node_name)
    if overlap:
        score += overlap * 10.0
        reasons.append(f"node_token_overlap:{overlap:.3f}")
    if node_bundle.template_name and entry.class_name and _token_overlap(entry.class_name, node_bundle.template_name):
        score += 5.0
        reasons.append("template_token_overlap")
    return score, reasons


def _allowed_arg_names(
    *,
    contract: XmlTemplateClassContract | None,
    examples: list[XmlTemplateExample],
    base_node: BaseWorkflowNodeReference | None,
    node_bundle: NodeEvidenceBundle,
) -> list[str]:
    names: set[str] = {"EcuBOMName"}
    if contract is not None:
        names.update(contract.required_args)
        names.update(contract.optional_args)
    for example in examples:
        names.update(example.arg_names)
    if base_node is not None:
        names.update(base_node.args)
    for candidate in node_bundle.candidates:
        for field in candidate.parameter_fields:
            arg_name = _arg_name_from_field(field)
            if arg_name:
                names.add(arg_name)
    for match in node_bundle.matches:
        for key in match.evidence.metadata:
            arg_name = _arg_name_from_field(str(key))
            if arg_name:
                names.add(arg_name)
    template_key = _normalize_template_key(node_bundle.template_name)
    if template_key == "DTC_READ_TYPE1":
        names.add("DID")
    if template_key.startswith("DTC_CLEAR"):
        names.add("ServiceID")
    if not names - {"EcuBOMName"}:
        names.update(["DID", "RID", "ServiceID", "RequestParameter"])
    ordered = [name for name in COMMON_ARG_ORDER if name in names]
    ordered.extend(sorted(name for name in names if name not in ordered))
    return ordered


def _normalize_template_key(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(value or "").upper()).strip("_")


def _find_base_workflow_node(
    base_workflow: str | Path | None,
    node_name: str,
) -> BaseWorkflowNodeReference | None:
    if base_workflow is None:
        return None
    source_path = ""
    xml_text = ""
    if isinstance(base_workflow, Path) or _looks_like_existing_path(base_workflow):
        path = Path(base_workflow)
        source_path = str(path)
        xml_text = path.read_text(encoding="utf-8")
    else:
        xml_text = str(base_workflow)
    if not xml_text.strip():
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    for element in root.iter():
        if _local_tag(element.tag) != "ScriptNode":
            continue
        if element.get("Name") != node_name:
            continue
        return BaseWorkflowNodeReference(
            node_name=node_name,
            class_name=element.get("ClassName") or "",
            args=_read_arg_values(element),
            xml=ET.tostring(element, encoding="unicode"),
            source_path=source_path,
        )
    return None


def _looks_like_existing_path(value: str | Path | None) -> bool:
    if value is None or isinstance(value, Path):
        return False
    if str(value).lstrip().startswith("<"):
        return False
    try:
        return Path(value).exists()
    except OSError:
        return False


def _read_arg_values(script: ET.Element) -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    for element in script.iter():
        if _local_tag(element.tag) != "Arg":
            continue
        name = element.get("ArgName")
        if name and name not in values:
            values[name] = element.text if element.text is not None else ""
    return values


def _load_template_registry(value: XmlTemplateRegistry | str | Path | None) -> XmlTemplateRegistry | None:
    if value is None:
        return None
    if isinstance(value, XmlTemplateRegistry):
        return value
    payload = json.loads(Path(value).read_text(encoding="utf-8"))
    return XmlTemplateRegistry.model_validate(payload)


def _load_contract_registry(
    value: XmlTemplateContractRegistry | str | Path | None,
) -> XmlTemplateContractRegistry | None:
    if value is None:
        return None
    if isinstance(value, XmlTemplateContractRegistry):
        return value
    payload = json.loads(Path(value).read_text(encoding="utf-8"))
    return XmlTemplateContractRegistry.model_validate(payload)


def _contract_for_class(
    contracts: XmlTemplateContractRegistry | None,
    class_name: str,
) -> XmlTemplateClassContract | None:
    if contracts is None:
        return None
    if class_name in contracts.by_class_name:
        return contracts.by_class_name[class_name]
    for contract in contracts.class_contracts:
        if contract.class_name == class_name:
            return contract
    return None


def _extract_script_xml_from_source(entry: XmlTemplateEntry) -> str:
    if not entry.source_path:
        return ""
    path = Path(entry.source_path)
    if not path.exists():
        return ""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return ""
    for element in root.iter():
        if _local_tag(element.tag) != "ScriptNode":
            continue
        if entry.node_name and element.get("Name") != entry.node_name:
            continue
        if entry.class_name and element.get("ClassName") != entry.class_name:
            continue
        return ET.tostring(element, encoding="unicode")
    return ""


def _args_from_payload(value: Any, evidence_map: dict[str, list[str]]) -> list[XmlArg]:
    args: list[XmlArg] = []
    if isinstance(value, dict):
        iterator = [{"name": key, "value": item} for key, item in value.items()]
    elif isinstance(value, list):
        iterator = value
    else:
        iterator = []
    for item in iterator:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("ArgName") or item.get("arg_name") or "").strip()
        if not name:
            continue
        raw_value = item.get("value")
        evidence_ids = _string_list(item.get("evidence_ids") or item.get("evidence_id") or evidence_map.get(name) or [])
        args.append(
            XmlArg(
                name=name,
                value="" if raw_value is None else str(raw_value),
                evidence_ids=evidence_ids,
                selection_notes=["llm_payload"],
            )
        )
    return args


def _normalize_evidence_map(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    output: dict[str, list[str]] = {}
    for key, item in value.items():
        name = str(key)
        if isinstance(item, dict):
            output[name] = _string_list(item.get("evidence_ids") or item.get("evidence_id") or [])
        else:
            output[name] = _string_list(item)
    return output


def _fallback_reason_for_arg(generation: LlmNodeXmlGeneration, arg_name: str) -> str:
    raw_map = {}
    try:
        raw_map = extract_json_object(generation.llm_raw_response).get("evidence_map") or {}
    except Exception:  # noqa: BLE001 - raw model text is not trusted.
        raw_map = {}
    value = raw_map.get(arg_name) if isinstance(raw_map, dict) else None
    if isinstance(value, dict):
        reason = str(value.get("fallback_reason") or value.get("reason") or "").strip()
        if reason:
            return reason
    for arg in generation.args:
        if arg.name != arg_name:
            continue
        reasons = [*arg.needs_review, *arg.selection_notes]
        fallback_reasons = [
            reason
            for reason in reasons
            if "fallback" in reason or "semantic_default" in reason or "guardrail" in reason
        ]
        if fallback_reasons:
            return "; ".join(fallback_reasons)
    return generation.missing_or_conflict_reason


def _evidence_for_arg(generation: LlmNodeXmlGeneration, arg_name: str) -> list[str]:
    if generation.evidence_map.get(arg_name):
        return generation.evidence_map[arg_name]
    for arg in generation.args:
        if arg.name == arg_name and arg.evidence_ids:
            return arg.evidence_ids
    return []


def _known_evidence_ids(context: XmlGenerationContext) -> set[str]:
    values = {
        item.evidence_id
        for item in context.context_items
        if item.evidence_id
    }
    values.update(candidate.evidence.evidence_id for candidate in context.node_bundle.candidates)
    values.update(match.evidence.evidence_id for match in context.node_bundle.matches)
    return values


def _allowed_class_names(context: XmlGenerationContext) -> set[str]:
    values = {context.class_name, context.template_name}
    if context.base_workflow_node is not None:
        values.add(context.base_workflow_node.class_name)
    values.update(example.class_name for example in context.template_examples)
    return {value for value in values if value}


def _generation_evidence_ids(generation: LlmNodeXmlGeneration) -> list[str]:
    return _dedupe(evidence_id for arg in generation.args for evidence_id in arg.evidence_ids)


def _arg_evidence_map_from_args(args: list[XmlArg]) -> dict[str, list[str]]:
    return {arg.name: list(arg.evidence_ids) for arg in args if arg.evidence_ids}


def _boundary_script_type(index: int, total: int) -> XmlScriptType:
    if total <= 1:
        return "NORMAL"
    if index == 1:
        return "START"
    if index == total:
        return "END"
    return "NORMAL"


def _script_type(value: str) -> XmlScriptType:
    return value if value in {"START", "NORMAL", "END"} else "NORMAL"  # type: ignore[return-value]


def _int_attr(element: ET.Element, name: str, default: int) -> int:
    try:
        return int(element.get(name) or default)
    except ValueError:
        return default


def _budget_report_dict(report) -> dict[str, Any]:
    return {
        "original_sections": report.original_matches,
        "kept_sections": report.kept_matches,
        "dropped_sections": report.dropped_matches,
        "estimated_tokens": report.estimated_tokens,
        "budget_tokens": report.budget_tokens,
        "dropped_reasons": report.dropped_reasons,
    }


def _save_raw_source(evidence: EvidenceUnit, output_dir: Path | None) -> str:
    if output_dir is None:
        return ""
    output_dir.mkdir(parents=True, exist_ok=True)
    content_hash = hashlib.sha256(evidence.content.encode("utf-8")).hexdigest()
    path = output_dir / f"{_slug(evidence.evidence_id)}_{content_hash[:12]}.md"
    if not path.exists():
        header = {
            "evidence_id": evidence.evidence_id,
            "doc_id": evidence.doc_id,
            "source_path": evidence.source_path or "",
            "page_idx": evidence.page_idx,
            "bbox": evidence.bbox,
            "metadata": evidence.metadata,
            "content_hash": content_hash,
        }
        path.write_text(
            "---\n"
            + json.dumps(header, ensure_ascii=False, indent=2)
            + "\n---\n\n"
            + evidence.content
            + "\n",
            encoding="utf-8",
        )
    return str(path)


def _arg_name_from_field(field_name: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z]+", "_", field_name).strip("_").upper()
    mapping = {
        "ECU": "EcuBOMName",
        "ECUBOMNAME": "EcuBOMName",
        "ECU_BOM_NAME": "EcuBOMName",
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


def _node_ecu(node_bundle: NodeEvidenceBundle) -> str:
    for value in [node_bundle.node_name.split("_", 1)[0], node_bundle.template_name.split("_", 1)[0]]:
        if value and not value.upper().startswith("GEEA") and re.match(r"^[A-Za-z]{2,5}[0-9A-Za-z]*$", value):
            return value.upper()
    return ""


def _token_overlap(left: str, right: str) -> float:
    left_tokens = set(_tokens(left))
    right_tokens = set(_tokens(right))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(len(left_tokens | right_tokens), 1)


def _tokens(value: str) -> list[str]:
    return [token.lower() for token in re.split(r"[^0-9A-Za-z]+", value) if token]


def _safe_class_name(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z_]+", "_", value.strip())
    return value.strip("_") or "GenericDiagnosticScript"


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value if str(item)]
    return [str(value)]


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:xml)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def _xml_for_prompt(xml: str) -> str:
    text = _strip_code_fence(str(xml or ""))
    if not text.strip():
        return ""
    try:
        root = ET.fromstring(text)
        return ET.tostring(root, encoding="unicode", short_empty_elements=True)
    except ET.ParseError:
        return re.sub(r"\s+", " ", text).strip()


def _extract_xml_from_malformed_response(raw_response: str) -> str:
    text = _strip_code_fence(str(raw_response or ""))
    match = re.search(r'"xml"\s*:\s*"((?:\\.|[^"\\])*)"', text, flags=re.S)
    if match:
        try:
            return _unescape_xml_delimiters(json.loads(f'"{match.group(1)}"')).strip()
        except json.JSONDecodeError:
            pass
    match = re.search(r"<ScriptNode\b[\s\S]*?</ScriptNode>", text)
    return _unescape_xml_delimiters(match.group(0)).strip() if match else ""


def _extract_args_from_malformed_response(raw_response: str) -> list[dict[str, Any]]:
    text = str(raw_response or "")
    match = re.search(r'"args"\s*:\s*(\[[\s\S]*?\])\s*,\s*"xml"', text)
    if not match:
        return []
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _evidence_map_from_malformed_response(raw_response: str) -> dict[str, Any]:
    text = str(raw_response or "")
    match = re.search(r'"evidence_map"\s*:\s*(\{[\s\S]*?\})\s*,\s*"confidence"', text)
    if match:
        try:
            value = json.loads(match.group(1))
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}
    evidence: dict[str, list[str]] = {}
    for arg in _extract_args_from_malformed_response(raw_response):
        if not isinstance(arg, dict):
            continue
        name = str(arg.get("name") or "")
        evidence_ids = _string_list(arg.get("evidence_ids"))
        if name and evidence_ids:
            evidence[name] = evidence_ids
    return evidence


def _extract_string_field(raw_response: str, field_name: str) -> str:
    match = re.search(rf'"{re.escape(field_name)}"\s*:\s*"((?:\\.|[^"\\])*)"', str(raw_response or ""), flags=re.S)
    if not match:
        return ""
    try:
        return str(json.loads(f'"{match.group(1)}"')).strip()
    except json.JSONDecodeError:
        return ""


def _extract_number_field(raw_response: str, field_name: str) -> float | None:
    match = re.search(rf'"{re.escape(field_name)}"\s*:\s*(-?\d+(?:\.\d+)?)', str(raw_response or ""))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _extract_bool_field(raw_response: str, field_name: str, *, default: bool = False) -> bool:
    match = re.search(rf'"{re.escape(field_name)}"\s*:\s*(true|false)', str(raw_response or ""), flags=re.I)
    if not match:
        return default
    return match.group(1).lower() == "true"


def _unescape_xml_delimiters(text: str) -> str:
    """Accept XML strings where the model HTML-escaped tag delimiters inside JSON."""

    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#34;", '"')
        .replace("&apos;", "'")
        .replace("&#39;", "'")
    )


def _clamp_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(number, 1.0))


def _clip(value: str, limit: int) -> str:
    value = value.strip()
    if limit <= 0 or len(value) <= limit:
        return value
    return value[:limit].rstrip() + "\n...[truncated]"


def _slug(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z_.-]+", "_", value.strip())
    return value.strip("._-") or "evidence"


def _dedupe(values) -> list:
    output = []
    seen = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
