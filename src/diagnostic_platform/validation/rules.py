"""Rule validation for the diagnostic intermediate DSL."""

from __future__ import annotations

import re

from diagnostic_platform.schemas import DiagnosticPlan, ValidationIssue, ValidationResult


def validate_plan(plan: DiagnosticPlan) -> ValidationResult:
    """Validate the generated diagnostic DSL."""

    issues: list[ValidationIssue] = []
    seen_session = False
    security_seed_requested: set[int] = set()

    for index, step in enumerate(plan.steps):
        send_bytes = _parse_hex_bytes(step.send)
        expect_bytes = _parse_hex_bytes(step.expect)
        if not send_bytes or not expect_bytes:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="UNPARSEABLE_STEP",
                    message="Step cannot be parsed as hex bytes.",
                    step_index=index,
                )
            )
            continue

        service = send_bytes[0]
        positive_service = (service + 0x40) & 0xFF
        if expect_bytes[0] != positive_service:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="POSITIVE_RESPONSE_MISMATCH",
                    message=(
                        f"Expected positive response service {positive_service:02X}, "
                        f"got {expect_bytes[0]:02X}."
                    ),
                    step_index=index,
                )
            )

        if service == 0x10:
            seen_session = True

        if service == 0x27:
            if not seen_session:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="SECURITY_WITHOUT_SESSION",
                        message="Security access is used before any diagnostic session step.",
                        step_index=index,
                    )
                )

            if len(send_bytes) < 2:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="SECURITY_LEVEL_MISSING",
                        message="Security access step is missing a sub-function level.",
                        step_index=index,
                    )
                )
                continue

            level = send_bytes[1]
            if level % 2 == 1:
                security_seed_requested.add(level)
            else:
                if (level - 1) not in security_seed_requested:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            code="SECURITY_PAIR_MISSING",
                            message="Security key step appears without a prior seed request.",
                            step_index=index,
                        )
                    )

        if service in {0x22, 0x2E, 0x2F, 0x31} and not seen_session:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="SERVICE_WITHOUT_SESSION",
                    message="Diagnostic service is used before any session step.",
                    step_index=index,
                )
            )

    return ValidationResult(
        valid=not any(issue.severity == "error" for issue in issues),
        issues=issues,
    )


def _parse_hex_bytes(payload: str) -> list[int]:
    tokens = re.findall(r"\b[0-9A-Fa-f]{2}\b", payload)
    return [int(token, 16) for token in tokens]

