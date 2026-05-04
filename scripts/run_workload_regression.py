#!/usr/bin/env python3
"""Run regression evaluation from a workload manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from diagnostic_platform.evaluation.regression import run_regression_evaluation  # noqa: E402
from diagnostic_platform.orchestration.workload import (  # noqa: E402
    load_workload_manifest,
    workload_to_regression_config,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", default="configs/workloads/treg_20260402.json")
    parser.add_argument(
        "--output-path",
        default="data/reports/regression/treg_20260402_regression_report.json",
    )
    parser.add_argument("--use-llm-xml", action="store_true", help="Evaluate the LLM-generated XML plan paths from the workload.")
    parser.add_argument("--print-checks", action="store_true")
    args = parser.parse_args()

    manifest = load_workload_manifest(Path(args.workload))
    report = run_regression_evaluation(workload_to_regression_config(manifest, use_llm_xml=args.use_llm_xml))

    output_path = Path(args.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    failed = [check for check in report.checks if check.status == "fail"]
    payload = {
        "valid": report.valid,
        "workload": report.workload,
        "checks": len(report.checks),
        "failed": len(failed),
        "output_path": str(output_path),
        "metrics": report.metrics,
    }
    if args.print_checks:
        payload["check_results"] = [check.model_dump(mode="json") for check in report.checks]
    else:
        payload["failed_checks"] = [check.model_dump(mode="json") for check in failed]
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if report.valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
