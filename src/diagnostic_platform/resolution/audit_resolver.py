"""Resolve workflow audit findings using XML template contracts."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

from diagnostic_platform.schemas import (
    ValidationIssue,
    WorkflowArgAuditItem,
    WorkflowAuditReport,
    WorkflowNodeAudit,
    WorkflowResolveAction,
    WorkflowResolveAuditReport,
    WorkflowResolveAuditRequest,
    WorkflowResolveDecision,
    XmlTemplateClassContract,
    XmlTemplateContractRegistry,
)


PASS_THROUGH_STATUSES = {
    "confirmed_by_plan",
    "base_only_unchanged",
    "filled_from_plan",
    "appended_from_plan",
    "overwritten_from_plan",
    "extra_plan_arg_ignored",
}


def resolve_workflow_audit(request: WorkflowResolveAuditRequest) -> WorkflowResolveAuditReport:
    """Classify audit findings into safe actions and review items."""

    audit_report = _load_audit_report(request)
    contract_registry = _load_contract_registry(request)

    decisions: list[WorkflowResolveDecision] = []
    issues: list[ValidationIssue] = []
    for node in audit_report.nodes:
        contract = contract_registry.by_class_name.get(node.class_name) if contract_registry else None
        for item in node.args:
            decision = _resolve_item(node, item, contract)
            decisions.append(decision)
            if decision.requires_review:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code="WORKFLOW_RESOLUTION_REVIEW_REQUIRED",
                        message=(
                            f"{decision.node_name}.{decision.arg_name}: {decision.reason} "
                            f"(action={decision.action})."
                        ),
                    )
                )

    action_counts = Counter(decision.action for decision in decisions)
    review_count = sum(1 for decision in decisions if decision.requires_review)
    summary = {
        "decisions": len(decisions),
        "auto_resolved": len(decisions) - review_count,
        "review_required": review_count,
        "action_counts": dict(sorted(action_counts.items())),
    }
    report = WorkflowResolveAuditReport(
        valid=not any(issue.severity == "error" for issue in issues),
        summary=summary,
        decisions=decisions,
        issues=issues,
    )
    if request.output_path:
        _write_report(report, request.output_path)
        report.output_path = request.output_path
    return report


def _resolve_item(
    node: WorkflowNodeAudit,
    item: WorkflowArgAuditItem,
    contract: XmlTemplateClassContract | None,
) -> WorkflowResolveDecision:
    allowed = _allowed_by_contract(item.arg_name, contract)
    required = _required_by_contract(item.arg_name, contract)

    action: WorkflowResolveAction
    reason: str
    requires_review = False
    confidence = 0.0

    if item.status in PASS_THROUGH_STATUSES:
        action = "ignore_plan_arg" if item.status == "extra_plan_arg_ignored" else "no_action"
        reason = "extra_plan_arg_not_present_in_fused_workflow" if item.status == "extra_plan_arg_ignored" else f"audit_status_{item.status}"
        confidence = 0.9 if item.status == "extra_plan_arg_ignored" else 1.0
    elif item.status == "plan_arg_missing_in_fused":
        if contract is not None and not allowed:
            action = "ignore_plan_arg"
            reason = "plan_arg_not_in_script_class_contract"
            requires_review = False
            confidence = 0.9
        elif contract is not None and allowed:
            action = "append_plan_arg" if required else "needs_review"
            reason = "plan_arg_allowed_by_contract_but_not_in_fused"
            requires_review = not required
            confidence = 0.75 if required else 0.4
        else:
            action = "needs_review"
            reason = "missing_contract_for_plan_arg"
            requires_review = True
            confidence = 0.2
    elif item.status == "plan_conflict_kept_base":
        action = "keep_base"
        reason = "plan_conflicts_with_existing_workflow_value"
        requires_review = True
        confidence = 0.65 if allowed else 0.8
    elif item.status in {"node_missing_in_base", "node_missing_in_fused"}:
        action = "needs_review"
        reason = item.status
        requires_review = True
        confidence = 0.0
    elif item.status in {"plan_conflict_fused_diff", "plan_not_applied", "workflow_changed_without_plan"}:
        action = "needs_review"
        reason = item.status
        requires_review = True
        confidence = 0.25
    else:
        action = "needs_review"
        reason = f"unhandled_audit_status_{item.status}"
        requires_review = True
        confidence = 0.0

    return WorkflowResolveDecision(
        node_name=node.node_name,
        class_name=node.class_name,
        arg_name=item.arg_name,
        audit_status=item.status,
        action=action,
        requires_review=requires_review,
        reason=reason,
        confidence=confidence,
        allowed_by_contract=allowed,
        required_by_contract=required,
        plan_value=item.plan_value,
        base_value=item.base_value,
        fused_value=item.fused_value,
        evidence_ids=item.evidence_ids,
    )


def _allowed_by_contract(arg_name: str, contract: XmlTemplateClassContract | None) -> bool:
    if contract is None:
        return False
    return arg_name in set(contract.required_args).union(contract.optional_args)


def _required_by_contract(arg_name: str, contract: XmlTemplateClassContract | None) -> bool:
    if contract is None:
        return False
    return arg_name in set(contract.required_args)


def _load_audit_report(request: WorkflowResolveAuditRequest) -> WorkflowAuditReport:
    if request.audit_report is not None:
        return request.audit_report
    if request.audit_report_path:
        payload = json.loads(Path(request.audit_report_path).read_text(encoding="utf-8"))
        return WorkflowAuditReport.model_validate(payload)
    raise ValueError("Either audit_report or audit_report_path is required.")


def _load_contract_registry(request: WorkflowResolveAuditRequest) -> XmlTemplateContractRegistry | None:
    if request.contract_registry is not None:
        return request.contract_registry
    if request.contract_registry_path:
        payload = json.loads(Path(request.contract_registry_path).read_text(encoding="utf-8"))
        return XmlTemplateContractRegistry.model_validate(payload)
    return None


def _write_report(report: WorkflowResolveAuditReport, output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
