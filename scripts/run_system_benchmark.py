#!/usr/bin/env python3
"""Run system benchmark scenarios."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from diagnostic_platform.evaluation.benchmark import run_system_benchmark  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/benchmark/treg_20260402_system.json")
    parser.add_argument("--output-path", default="data/reports/benchmark/treg_20260402_system_benchmark.json")
    parser.add_argument("--enable-scenario", action="append", default=[])
    parser.add_argument("--skip-scenario", action="append", default=[])
    parser.add_argument("--iterations", type=int, default=None)
    parser.add_argument("--print-samples", action="store_true")
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.iterations is not None:
        config["iterations"] = args.iterations
    _apply_scenario_overrides(config, set(args.enable_scenario), set(args.skip_scenario))

    report = run_system_benchmark(config, repo_root=REPO_ROOT)
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
        "scenarios": [
            {
                "name": scenario.name,
                "valid": scenario.valid,
                "metrics": scenario.metrics,
                "checks": scenario.checks,
                **({"samples": [sample.model_dump(mode="json") for sample in scenario.samples]} if args.print_samples else {}),
            }
            for scenario in report.scenarios
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if report.valid else 2


def _apply_scenario_overrides(config: dict, enable: set[str], skip: set[str]) -> None:
    for scenario in config.get("scenarios") or []:
        name = str(scenario.get("name") or "")
        if name in enable:
            scenario["enabled"] = True
        if name in skip:
            scenario["enabled"] = False


if __name__ == "__main__":
    raise SystemExit(main())
