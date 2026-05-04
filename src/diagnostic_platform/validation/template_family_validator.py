"""Template-family-level validation for generated TaskNode XML."""

from __future__ import annotations

from diagnostic_platform.schemas import TemplateFamilyEntry, ValidationIssue, ValidationResult
from diagnostic_platform.template_family.xml_signature import extract_tasknode_signature
from diagnostic_platform.validation.tasknode_validator import validate_tasknode_xml


def validate_tasknode_against_family(xml: str, family: TemplateFamilyEntry) -> ValidationResult:
    """Validate that a generated TaskNode preserves the resolved template family's core signature."""

    issues = list(validate_tasknode_xml(xml).issues)
    signature = _signature_from_xml_text(xml)
    if signature is None:
        return ValidationResult(valid=False, issues=issues)
    expected = family.merged_signature
    if expected is None:
        return ValidationResult(valid=not _has_errors(issues), issues=issues)

    for service_id in expected.service_ids:
        if service_id not in signature.service_ids:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="TEMPLATE_FAMILY_SERVICE_ID_MISSING",
                    message=f"{family.family_id}: generated XML is missing expected request service 0x{service_id}.",
                )
            )
    for response_id in expected.positive_responses:
        if response_id not in signature.positive_responses:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="TEMPLATE_FAMILY_POSITIVE_RESPONSE_MISSING",
                    message=f"{family.family_id}: generated XML may be missing expected positive response 0x{response_id}.",
                )
            )
    for marker in expected.logic_markers:
        if marker in {"fault_handling"}:
            continue
        if marker not in signature.logic_markers:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="TEMPLATE_FAMILY_LOGIC_MARKER_MISSING",
                    message=f"{family.family_id}: generated XML may be missing logic marker {marker!r}.",
                )
            )
    for arg_name in expected.arg_names:
        if arg_name not in signature.arg_names:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="TEMPLATE_FAMILY_ARG_SOURCE_MISSING",
                    message=f"{family.family_id}: generated XML does not read ArgName {arg_name!r} from _argLibrary.",
                )
            )

    return ValidationResult(valid=not _has_errors(issues), issues=issues)


def _signature_from_xml_text(xml: str):
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".xml", delete=False) as handle:
        handle.write(xml)
        temp_path = Path(handle.name)
    try:
        return extract_tasknode_signature(temp_path)
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass


def _has_errors(issues: list[ValidationIssue]) -> bool:
    return any(issue.severity == "error" for issue in issues)
