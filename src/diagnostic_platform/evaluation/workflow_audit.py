"""Audit workflow XML parameters against an XML generation plan."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from xml.etree import ElementTree as ET

from diagnostic_platform.schemas import (
    ValidationIssue,
    WorkflowArgAuditItem,
    WorkflowArgAuditStatus,
    WorkflowAuditReport,
    WorkflowAuditRequest,
    WorkflowNodeAudit,
    XmlArg,
    XmlGenerationPlan,
    XmlScriptNodePlan,
)


REVIEW_STATUSES = {
    "plan_conflict_kept_base",
    "plan_conflict_fused_diff",
    "plan_not_applied",
    "plan_arg_missing_in_fused",
    "workflow_changed_without_plan",
    "node_missing_in_base",
    "node_missing_in_fused",
}


def audit_workflow(request: WorkflowAuditRequest) -> WorkflowAuditReport:
    """Compare planned args with base and fused workflow XML."""

    base_nodes = _load_workflow_nodes(request.base_xml, request.base_xml_path, "base")
    fused_nodes = _load_workflow_nodes(request.fused_xml, request.fused_xml_path, "fused")

    node_reports: list[WorkflowNodeAudit] = []
    issues: list[ValidationIssue] = []
    status_counter: Counter[str] = Counter()

    for step_index, script_plan in enumerate(request.plan.serial.scripts):
        node_report = _audit_node(
            script_plan=script_plan,
            base_node=base_nodes.get(script_plan.node_name),
            fused_node=fused_nodes.get(script_plan.node_name),
            required_arg_names=request.required_arg_names,
            include_base_only_args=request.include_base_only_args,
        )
        node_reports.append(node_report)
        status_counter.update(node_report.summary)
        _collect_node_issues(node_report, step_index, issues)

    summary = {
        "planned_nodes": len(request.plan.serial.scripts),
        "audited_nodes": len(node_reports),
        "review_items": sum(1 for node in node_reports for arg in node.args if arg.requires_review),
        "status_counts": dict(sorted(status_counter.items())),
    }
    report = WorkflowAuditReport(
        valid=not any(issue.severity == "error" for issue in issues),
        summary=summary,
        nodes=node_reports,
        issues=issues,
    )
    if request.output_path:
        _write_report(report, request.output_path)
        report.output_path = request.output_path
    return report


def _audit_node(
    script_plan: XmlScriptNodePlan,
    base_node: "_WorkflowNode | None",
    fused_node: "_WorkflowNode | None",
    required_arg_names: list[str],
    include_base_only_args: bool,
) -> WorkflowNodeAudit:
    if base_node is None or fused_node is None:
        status: WorkflowArgAuditStatus = "node_missing_in_fused" if fused_node is None else "node_missing_in_base"
        item = WorkflowArgAuditItem(
            node_name=script_plan.node_name,
            arg_name="*",
            status=status,
            requires_review=True,
            notes=[status],
        )
        return WorkflowNodeAudit(
            node_name=script_plan.node_name,
            class_name=script_plan.class_name,
            matched_in_base=base_node is not None,
            matched_in_fused=fused_node is not None,
            args=[item],
            summary={status: 1},
        )

    plan_args = {arg.name: arg for arg in script_plan.args if arg.name}
    arg_names = set(plan_args)
    arg_names.update(required_arg_names)
    if include_base_only_args:
        arg_names.update(base_node.args)
        arg_names.update(fused_node.args)

    items = [
        _audit_arg(
            node_name=script_plan.node_name,
            arg_name=arg_name,
            plan_arg=plan_args.get(arg_name),
            base_value=base_node.args.get(arg_name),
            fused_value=fused_node.args.get(arg_name),
            required=arg_name in required_arg_names,
        )
        for arg_name in sorted(arg_names)
    ]
    summary = Counter(item.status for item in items)
    return WorkflowNodeAudit(
        node_name=script_plan.node_name,
        class_name=fused_node.class_name or script_plan.class_name,
        matched_in_base=True,
        matched_in_fused=True,
        args=items,
        summary=dict(sorted(summary.items())),
    )


def _audit_arg(
    node_name: str,
    arg_name: str,
    plan_arg: XmlArg | None,
    base_value: str | None,
    fused_value: str | None,
    required: bool,
) -> WorkflowArgAuditItem:
    plan_value = _clean_value(plan_arg.value if plan_arg else None)
    base_value = _clean_value(base_value)
    fused_value = _clean_value(fused_value)
    status = _arg_status(plan_value, base_value, fused_value)
    notes: list[str] = []
    if plan_arg is not None and fused_value is None and not required:
        status = "extra_plan_arg_ignored"
        notes.append("extra_plan_arg_not_present_in_fused_workflow")
    if required and fused_value is None:
        notes.append("required_arg_empty_or_missing")
        status = "plan_arg_missing_in_fused" if plan_arg else "workflow_changed_without_plan"
    elif required and not fused_value and plan_value not in {None, ""}:
        notes.append("required_arg_empty_or_missing")
        status = "plan_arg_missing_in_fused" if plan_arg else "workflow_changed_without_plan"
    if plan_arg is not None and not plan_arg.evidence_ids:
        notes.append("plan_arg_without_evidence")

    return WorkflowArgAuditItem(
        node_name=node_name,
        arg_name=arg_name,
        status=status,
        plan_value=plan_value,
        base_value=base_value,
        fused_value=fused_value,
        evidence_ids=plan_arg.evidence_ids if plan_arg else [],
        requires_review=status in REVIEW_STATUSES or bool(notes and required),
        notes=notes,
    )


def _arg_status(
    plan_value: str | None,
    base_value: str | None,
    fused_value: str | None,
) -> WorkflowArgAuditStatus:
    if plan_value is None:
        if base_value == fused_value:
            return "base_only_unchanged"
        return "workflow_changed_without_plan"

    if fused_value is None:
        return "plan_arg_missing_in_fused"

    if base_value is None:
        if fused_value == plan_value:
            return "appended_from_plan"
        return "plan_conflict_fused_diff"

    if not base_value:
        if fused_value == plan_value:
            return "filled_from_plan"
        if not fused_value:
            return "plan_not_applied"
        return "plan_conflict_fused_diff"

    if fused_value == base_value:
        if plan_value == base_value:
            return "confirmed_by_plan"
        return "plan_conflict_kept_base"

    if fused_value == plan_value:
        return "overwritten_from_plan"
    return "plan_conflict_fused_diff"


def _collect_node_issues(node_report: WorkflowNodeAudit, step_index: int, issues: list[ValidationIssue]) -> None:
    for item in node_report.args:
        if item.status == "node_missing_in_base":
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="WORKFLOW_NODE_MISSING_IN_BASE",
                    message=f"Planned node is not present in base workflow: {node_report.node_name}.",
                    step_index=step_index,
                )
            )
        elif item.status == "node_missing_in_fused":
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="WORKFLOW_NODE_MISSING_IN_FUSED",
                    message=f"Planned node is not present in fused workflow: {node_report.node_name}.",
                    step_index=step_index,
                )
            )
        elif item.requires_review:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="WORKFLOW_ARG_REVIEW_REQUIRED",
                    message=(
                        f"{node_report.node_name}.{item.arg_name}: {item.status} "
                        f"(base={item.base_value!r}, plan={item.plan_value!r}, fused={item.fused_value!r})."
                    ),
                    step_index=step_index,
                )
            )


def _load_workflow_nodes(xml: str | None, xml_path: str | None, label: str) -> dict[str, "_WorkflowNode"]:
    if xml is None:
        if not xml_path:
            raise ValueError(f"Either {label}_xml or {label}_xml_path is required.")
        xml = Path(xml_path).read_text(encoding="utf-8")
    root = ET.fromstring(xml)
    nodes: dict[str, _WorkflowNode] = {}
    for element in root.iter():
        if _local_tag(element.tag) != "ScriptNode":
            continue
        name = element.get("Name")
        if not name or name in nodes:
            continue
        nodes[name] = _WorkflowNode(
            name=name,
            class_name=element.get("ClassName") or "",
            args=_read_args(element),
        )
    return nodes


def _read_args(script: ET.Element) -> dict[str, str | None]:
    args: dict[str, str | None] = {}
    for element in script.iter():
        if _local_tag(element.tag) != "Arg":
            continue
        name = element.get("ArgName")
        if name and name not in args:
            args[name] = element.text if element.text is not None else ""
    return args


def _write_report(report: WorkflowAuditReport, output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _clean_value(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip()


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class _WorkflowNode:
    def __init__(self, name: str, class_name: str, args: dict[str, str | None]) -> None:
        self.name = name
        self.class_name = class_name
        self.args = args
