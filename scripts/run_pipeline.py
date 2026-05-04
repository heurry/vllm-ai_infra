#!/usr/bin/env python3
"""Run the configured end-to-end XML generation pipeline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from diagnostic_platform.orchestration.pipeline import run_pipeline  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="configs/pipeline/treg_20260402_pipeline.json",
        help="Pipeline config JSON.",
    )
    parser.add_argument(
        "--output-path",
        default="data/reports/pipeline/treg_20260402_pipeline_report.json",
        help="Where to write the pipeline report JSON.",
    )
    parser.add_argument("--skip-step", action="append", default=[], help="Step name to skip.")
    parser.add_argument("--enable-step", action="append", default=[], help="Disabled step name to force-enable.")
    parser.add_argument("--print-steps", action="store_true", help="Print all step details.")
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    report = run_pipeline(
        config=config,
        repo_root=REPO_ROOT,
        skip_steps=set(args.skip_step),
        enable_steps=set(args.enable_step),
    )

    output_path = Path(args.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    payload = {
        "valid": report.valid,
        "workload": report.workload,
        "duration_seconds": report.duration_seconds,
        "summary": report.summary,
        "output_path": str(output_path),
    }
    if args.print_steps:
        payload["steps"] = [step.model_dump(mode="json") for step in report.steps]
    else:
        payload["failed_steps"] = [
            step.model_dump(mode="json")
            for step in report.steps
            if step.status == "failed"
        ]

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if report.valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
