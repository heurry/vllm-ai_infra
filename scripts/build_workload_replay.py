#!/usr/bin/env python3
"""Build a real workload replay dataset from pipeline artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from diagnostic_platform.evaluation.replay import (  # noqa: E402
    build_replay_dataset_from_artifacts,
    default_replay_output_path,
    write_replay_dataset,
)
from diagnostic_platform.orchestration.workload import load_workload_manifest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", default="configs/workloads/treg_20260402.json")
    parser.add_argument("--llm-xml-trace-path", default="")
    parser.add_argument("--output-path")
    args = parser.parse_args()

    manifest_path = (REPO_ROOT / args.workload).resolve() if not Path(args.workload).is_absolute() else Path(args.workload)
    manifest = load_workload_manifest(manifest_path)
    paths = manifest.paths

    flow_trace_path = _resolve_path(paths["flow_evidence_trace"])
    resolution_path = _resolve_path(paths["audit_resolution_report"])
    llm_resolution_path = _resolve_optional_path(paths.get("llm_resolution_report"))
    llm_xml_trace_path = _resolve_optional_path(
        args.llm_xml_trace_path
        or paths.get("llm_generation_trace")
        or paths.get("llm_xml_generation_trace")
    )
    output_path = (
        _resolve_path(args.output_path)
        if args.output_path
        else _resolve_path(paths.get("workload_replay_dataset") or str(default_replay_output_path(manifest.workload)))
    )

    dataset = build_replay_dataset_from_artifacts(
        workload=manifest.workload,
        flow_trace_payload=_load_json(flow_trace_path),
        resolution_payload=_load_json(resolution_path),
        llm_resolution_payload=_load_json(llm_resolution_path) if llm_resolution_path else None,
        llm_xml_payload=_load_json(llm_xml_trace_path) if llm_xml_trace_path and llm_xml_trace_path.exists() else None,
        source_manifest=str(manifest_path),
    )
    write_replay_dataset(dataset, output_path)

    print(
        json.dumps(
            {
                "workload": manifest.workload,
                "output_path": str(output_path.relative_to(REPO_ROOT)),
                "items": len(dataset.items),
                "summary": dataset.summary,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


def _resolve_optional_path(value: str | None) -> Path | None:
    if not value:
        return None
    return _resolve_path(value)


if __name__ == "__main__":
    raise SystemExit(main())
