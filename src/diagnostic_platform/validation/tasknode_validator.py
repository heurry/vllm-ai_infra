"""Generic structural validation for generated TaskNode XML."""

from __future__ import annotations

from xml.etree import ElementTree as ET

from diagnostic_platform.schemas import ValidationIssue, ValidationResult


def validate_tasknode_xml(xml: str) -> ValidationResult:
    """Validate XML that is intended to be one ClassName TaskNode implementation."""

    issues: list[ValidationIssue] = []
    if not (xml or "").strip():
        return ValidationResult(
            valid=False,
            issues=[
                ValidationIssue(
                    severity="error",
                    code="TASKNODE_XML_EMPTY",
                    message="TaskNode XML is empty.",
                )
            ],
        )
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        return ValidationResult(
            valid=False,
            issues=[
                ValidationIssue(
                    severity="error",
                    code="TASKNODE_XML_PARSE_ERROR",
                    message=str(exc),
                )
            ],
        )

    root_tag = _local_tag(root.tag)
    if root_tag != "TaskNode":
        issues.append(
            ValidationIssue(
                severity="error",
                code="TASKNODE_ROOT_MISMATCH",
                message=f"Expected TaskNode root, got {root_tag!r}.",
            )
        )

    for tag in ["MainNodes", "MainNode", "CVNodes", "FlowsNodes"]:
        if not _find_any(root, tag):
            issues.append(
                ValidationIssue(
                    severity="error",
                    code=f"TASKNODE_MISSING_{tag.upper()}",
                    message=f"TaskNode is missing required {tag} section.",
                )
            )

    if not _find_any(root, "NormalBoxNode"):
        issues.append(
            ValidationIssue(
                severity="error",
                code="TASKNODE_MISSING_FLOW_BOX",
                message="TaskNode must contain at least one NormalBoxNode.",
            )
        )
    if _find_any(root, "ScriptNode"):
        issues.append(
            ValidationIssue(
                severity="error",
                code="TASKNODE_CONTAINS_SCRIPTNODE",
                message="ClassName implementation XML must be TaskNode logic, not nested ScriptNode invocation XML.",
            )
        )

    return ValidationResult(valid=not any(issue.severity == "error" for issue in issues), issues=issues)


def _find_any(root: ET.Element, tag: str) -> bool:
    return any(_local_tag(element.tag) == tag for element in root.iter())


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
