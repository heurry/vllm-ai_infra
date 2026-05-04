#!/usr/bin/env python3
"""Resolve workflow audit findings with template argument contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from diagnostic_platform.resolution.audit_resolver import resolve_workflow_audit  # noqa: E402
from diagnostic_platform.schemas import WorkflowResolveAuditRequest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-report-path", default="data/processed/workflow/treg_20260402/arg_audit_report.json")
    parser.add_argument(
        "--contract-registry-path",
        default="data/processed/xml_template_registry/treg_20260402_contracts.json",
    )
    parser.add_argument("--output-path", default="data/processed/workflow/treg_20260402/audit_resolution_report.json")
    args = parser.parse_args()

    report = resolve_workflow_audit(
        WorkflowResolveAuditRequest(
            audit_report_path=args.audit_report_path,
            contract_registry_path=args.contract_registry_path,
            output_path=args.output_path,
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


if __name__ == "__main__":
    raise SystemExit(main())

