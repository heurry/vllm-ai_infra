"""Validate XML intermediate DSL before rendering."""

from __future__ import annotations

import re

from diagnostic_platform.schemas import (
    ValidationIssue,
    ValidationResult,
    XmlArg,
    XmlGenerationPlan,
    XmlPlanValidationRequest,
    XmlScriptNodePlan,
)


def validate_xml_plan(request: XmlPlanValidationRequest) -> ValidationResult:
    """Validate XML DSL structure, required args, placeholders, and evidence links."""

    issues: list[ValidationIssue] = []
    plan = request.plan

    if len(plan.serial.scripts) < request.min_script_nodes:
        issues.append(
            ValidationIssue(
                severity="error",
                code="SCRIPT_NODE_COUNT_TOO_LOW",
                message=(
                    f"Script plan count {len(plan.serial.scripts)} is below "
                    f"required minimum {request.min_script_nodes}."
                ),
            )
        )

    if len(plan.serial.scripts) != len(plan.nodes):
        issues.append(
            ValidationIssue(
                severity="error",
                code="PLAN_NODE_COUNT_MISMATCH",
                message="serial.scripts count must match planned nodes count.",
            )
        )

    _validate_duplicate_node_names(plan, issues)
    for index, script in enumerate(plan.serial.scripts):
        _validate_script(
            script=script,
            index=index,
            required_arg_names=request.required_arg_names,
            require_arg_evidence=request.require_arg_evidence,
            issues=issues,
        )

    for index, node in enumerate(plan.nodes):
        for field in node.missing_fields:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="XML_PLAN_FIELD_MISSING",
                    message=f"Node {node.node_name} has missing extracted field: {field}.",
                    step_index=index,
                )
            )

    return ValidationResult(
        valid=not any(issue.severity == "error" for issue in issues),
        issues=issues,
    )


def _validate_duplicate_node_names(plan: XmlGenerationPlan, issues: list[ValidationIssue]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for script in plan.serial.scripts:
        if script.node_name in seen:
            duplicates.add(script.node_name)
        seen.add(script.node_name)

    for node_name in sorted(duplicates):
        issues.append(
            ValidationIssue(
                severity="error",
                code="DUPLICATE_SCRIPT_NODE",
                message=f"Duplicate ScriptNode name in XML plan: {node_name}.",
            )
        )


def _validate_script(
    script: XmlScriptNodePlan,
    index: int,
    required_arg_names: list[str],
    require_arg_evidence: bool,
    issues: list[ValidationIssue],
) -> None:
    if not script.node_name.strip():
        issues.append(
            ValidationIssue(
                severity="error",
                code="SCRIPT_NODE_NAME_MISSING",
                message="Script node name is missing.",
                step_index=index,
            )
        )
    if not script.class_name.strip():
        issues.append(
            ValidationIssue(
                severity="error",
                code="SCRIPT_CLASS_NAME_MISSING",
                message=f"Script node {script.node_name} is missing ClassName.",
                step_index=index,
            )
        )

    if _has_placeholder(script.node_name) or _has_placeholder(script.class_name):
        issues.append(
            ValidationIssue(
                severity="error",
                code="XML_PLAN_PLACEHOLDER_REMAINS",
                message=f"Script node {script.node_name} still contains placeholders.",
                step_index=index,
            )
        )

    args_by_name = {arg.name: arg for arg in script.args}
    for required in required_arg_names:
        arg = args_by_name.get(required)
        if arg is None:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="XML_PLAN_REQUIRED_ARG_MISSING",
                    message=f"Script node {script.node_name} is missing required Arg: {required}.",
                    step_index=index,
                )
            )
        elif not (arg.value or "").strip():
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="XML_PLAN_REQUIRED_ARG_EMPTY",
                    message=f"Script node {script.node_name} has empty required Arg: {required}.",
                    step_index=index,
                )
            )

    for arg in script.args:
        _validate_arg(script, arg, index, require_arg_evidence, issues)


def _validate_arg(
    script: XmlScriptNodePlan,
    arg: XmlArg,
    index: int,
    require_arg_evidence: bool,
    issues: list[ValidationIssue],
) -> None:
    if not arg.name.strip():
        issues.append(
            ValidationIssue(
                severity="error",
                code="XML_PLAN_ARG_NAME_MISSING",
                message=f"Script node {script.node_name} contains an Arg without name.",
                step_index=index,
            )
        )
    if arg.value is not None and _has_placeholder(arg.value):
        issues.append(
            ValidationIssue(
                severity="error",
                code="XML_PLAN_PLACEHOLDER_REMAINS",
                message=f"Arg {arg.name} in script node {script.node_name} still contains placeholders.",
                step_index=index,
            )
        )
    if require_arg_evidence and not arg.evidence_ids:
        issues.append(
            ValidationIssue(
                severity="error",
                code="XML_PLAN_ARG_EVIDENCE_MISSING",
                message=f"Arg {arg.name} in script node {script.node_name} has no evidence id.",
                step_index=index,
            )
        )


def _has_placeholder(value: str) -> bool:
    return bool(re.search(r"\{\{[^}]+\}\}", value))

