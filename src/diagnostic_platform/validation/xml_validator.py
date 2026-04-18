"""Validate generated XML fragments before they are used as workflow code."""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from diagnostic_platform.schemas import ValidationIssue, XmlValidationRequest, XmlValidationResult


SCRIPT_NODE_REQUIRED_ATTRS = {
    "Name",
    "DeadMS",
    "RetryTimes",
    "ScriptType",
    "InteruptStart",
    "ClassName",
}
ALLOWED_ROOT_TAGS = {"ScriptNode", "SerialNode", "PhaseNode", "TaskNode"}
ALLOWED_SCRIPT_TYPES = {"START", "NORMAL", "END"}


def validate_xml(request: XmlValidationRequest) -> XmlValidationResult:
    """Validate XML syntax, structure, placeholders, and required arguments."""

    issues: list[ValidationIssue] = []
    stats: dict[str, int | str] = {}

    try:
        root = ET.fromstring(request.xml)
    except ET.ParseError as exc:
        return XmlValidationResult(
            valid=False,
            issues=[
                ValidationIssue(
                    severity="error",
                    code="XML_PARSE_ERROR",
                    message=str(exc),
                )
            ],
            stats={},
        )

    root_tag = _local_tag(root.tag)
    stats["root_tag"] = root_tag
    stats["script_nodes"] = len(_elements(root, "ScriptNode"))
    stats["serial_nodes"] = len(_elements(root, "SerialNode"))
    stats["parallel_nodes"] = len(_elements(root, "ParallelNode"))
    stats["args"] = len(_elements(root, "Arg"))

    if root_tag not in ALLOWED_ROOT_TAGS:
        issues.append(
            ValidationIssue(
                severity="error",
                code="UNSUPPORTED_ROOT_TAG",
                message=f"Unsupported root tag: {root_tag}",
            )
        )

    if re.search(r"\{\{[^}]+\}\}", request.xml):
        issues.append(
            ValidationIssue(
                severity="error",
                code="PLACEHOLDER_REMAINS",
                message="XML still contains template placeholders.",
            )
        )

    if stats["script_nodes"] < request.min_script_nodes:
        issues.append(
            ValidationIssue(
                severity="error",
                code="SCRIPT_NODE_COUNT_TOO_LOW",
                message=(
                    f"ScriptNode count {stats['script_nodes']} is below "
                    f"required minimum {request.min_script_nodes}."
                ),
            )
        )

    _validate_script_nodes(root, issues)
    _validate_args(root, request.required_arg_names, issues)
    _validate_required_script_types(root, request.require_script_types, issues)

    return XmlValidationResult(
        valid=not any(issue.severity == "error" for issue in issues),
        issues=issues,
        stats=stats,
    )


def _validate_script_nodes(root: ET.Element, issues: list[ValidationIssue]) -> None:
    for node in _elements(root, "ScriptNode"):
        node_name = node.get("Name", "<unnamed>")
        missing_attrs = sorted(attr for attr in SCRIPT_NODE_REQUIRED_ATTRS if not node.get(attr))
        if missing_attrs:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="SCRIPT_NODE_ATTR_MISSING",
                    message=f"ScriptNode {node_name} is missing attributes: {', '.join(missing_attrs)}.",
                )
            )

        script_type = node.get("ScriptType")
        if script_type and script_type not in ALLOWED_SCRIPT_TYPES:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="INVALID_SCRIPT_TYPE",
                    message=f"ScriptNode {node_name} has invalid ScriptType: {script_type}.",
                )
            )


def _validate_args(
    root: ET.Element,
    required_arg_names: list[str],
    issues: list[ValidationIssue],
) -> None:
    arg_values: dict[str, str] = {}
    for arg in _elements(root, "Arg"):
        name = arg.get("ArgName")
        if not name:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="ARG_NAME_MISSING",
                    message="Arg node is missing ArgName.",
                )
            )
            continue
        value = (arg.text or "").strip()
        if name not in arg_values:
            arg_values[name] = value

    for required in required_arg_names:
        if required not in arg_values:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="REQUIRED_ARG_MISSING",
                    message=f"Required Arg is missing: {required}.",
                )
            )
        elif not arg_values[required]:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="REQUIRED_ARG_EMPTY",
                    message=f"Required Arg is empty: {required}.",
                )
            )


def _validate_required_script_types(
    root: ET.Element,
    required_types: list[str],
    issues: list[ValidationIssue],
) -> None:
    present = {node.get("ScriptType") for node in _elements(root, "ScriptNode")}
    for script_type in required_types:
        if script_type not in present:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="REQUIRED_SCRIPT_TYPE_MISSING",
                    message=f"Required ScriptType is missing: {script_type}.",
                )
            )


def _elements(root: ET.Element, tag: str) -> list[ET.Element]:
    return [element for element in root.iter() if _local_tag(element.tag) == tag]


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
