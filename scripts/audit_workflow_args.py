#!/usr/bin/env python3
"""Audit XML plan parameters against base and fused workflow XML."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from diagnostic_platform.evaluation.workflow_audit import audit_workflow  # noqa: E402
from diagnostic_platform.schemas import WorkflowAuditRequest, XmlGenerationPlan  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-path", default="data/processed/xml_plan/treg_20260402/xml_plan.json")
    parser.add_argument("--base-xml-path", default="pdf-loop/20260402_new_v2/0402/TREG/P177__FHC（主流程）.xml")
    parser.add_argument("--fused-xml-path", default="data/processed/workflow/treg_20260402/fused_workflow.xml")
    parser.add_argument("--output-path", default="data/processed/workflow/treg_20260402/arg_audit_report.json")
    parser.add_argument("--required-arg", action="append", default=[], help="Required Arg name to check in fused XML.")
    parser.add_argument("--plan-only", action="store_true", help="Only audit args present in the XML plan.")
    args = parser.parse_args()

    report = audit_workflow(
        WorkflowAuditRequest(
            plan=_load_plan(Path(args.plan_path)),
            base_xml_path=args.base_xml_path,
            fused_xml_path=args.fused_xml_path,
            output_path=args.output_path,
            required_arg_names=args.required_arg,
            include_base_only_args=not args.plan_only,
        )
    )
    print(
        json.dumps(
            {
                "valid": report.valid,
                "summary": report.summary,
                "issues": len(report.issues),
                "output_path": report.output_path,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report.valid else 2


def _load_plan(path: Path) -> XmlGenerationPlan:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "plan" in payload:
        payload = payload["plan"]
    return XmlGenerationPlan.model_validate(payload)


if __name__ == "__main__":
    raise SystemExit(main())

