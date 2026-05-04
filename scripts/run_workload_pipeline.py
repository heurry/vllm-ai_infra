#!/usr/bin/env python3
"""Run the end-to-end pipeline from a workload manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from diagnostic_platform.orchestration.pipeline import run_pipeline  # noqa: E402
from diagnostic_platform.orchestration.workload import (  # noqa: E402
    inject_workload_manifest_path,
    load_workload_manifest,
    workload_to_pipeline_config,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", default="configs/workloads/treg_20260402.json")
    parser.add_argument("--output-path", default="")
    parser.add_argument("--enable-llm", action="store_true")
    parser.add_argument("--skip-step", action="append", default=[])
    parser.add_argument("--print-steps", action="store_true")
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Print step progress and stream child process output to stderr.",
    )
    parser.add_argument(
        "--enable-dense",
        action="store_true",
        help="Enable dense/hybrid retrieval for generated child commands.",
    )
    parser.add_argument("--vector-config", default="", help="Vector retrieval config path override.")
    parser.add_argument("--vector-collection", default="", help="Milvus collection override for dense retrieval.")
    parser.add_argument("--milvus-uri", default="", help="Milvus URI override.")
    parser.add_argument("--embedding-model", default="", help="Embedding model path/name override.")
    args = parser.parse_args()

    workload_path = Path(args.workload)
    manifest = load_workload_manifest(workload_path)
    manifest = _apply_retrieval_overrides(manifest, args)
    pipeline_config = inject_workload_manifest_path(
        workload_to_pipeline_config(manifest, enable_llm=args.enable_llm),
        workload_path,
    )
    report = run_pipeline(
        config=pipeline_config,
        repo_root=REPO_ROOT,
        skip_steps=set(args.skip_step),
        progress_callback=_print_progress if args.progress else None,
        stream_output=args.progress,
    )

    output_path = Path(args.output_path or manifest.paths["pipeline_report"])
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


def _apply_retrieval_overrides(manifest: Any, args: argparse.Namespace) -> Any:
    """Apply CLI dense-retrieval overrides without changing workload schema."""

    has_retrieval_override = any(
        [
            args.enable_dense,
            args.vector_config,
            args.vector_collection,
            args.milvus_uri,
            args.embedding_model,
        ]
    )
    if not has_retrieval_override:
        return manifest

    payload = manifest.model_dump(mode="json")
    hybrid_retrieval = dict(payload.get("hybrid_retrieval") or {})
    vector_index = dict(payload.get("vector_index") or {})

    if args.enable_dense:
        hybrid_retrieval["enabled"] = True
    if args.vector_config:
        hybrid_retrieval["vector_config"] = args.vector_config
        vector_index["vector_config"] = args.vector_config
    if args.vector_collection:
        hybrid_retrieval["vector_collection"] = args.vector_collection
        if vector_index.get("enabled"):
            vector_index["collection_name"] = args.vector_collection
    if args.milvus_uri:
        hybrid_retrieval["milvus_uri"] = args.milvus_uri
        if vector_index.get("enabled"):
            vector_index["milvus_uri"] = args.milvus_uri
    if args.embedding_model:
        hybrid_retrieval["embedding_model"] = args.embedding_model
        if vector_index.get("enabled"):
            vector_index["embedding_model"] = args.embedding_model

    payload["hybrid_retrieval"] = hybrid_retrieval
    payload["vector_index"] = vector_index
    return type(manifest).model_validate(payload)


def _print_progress(payload: dict[str, Any]) -> None:
    event = payload.get("event")
    step = f"{payload.get('step_index')}/{payload.get('total_steps')}"
    name = payload.get("step_name")
    if event == "pipeline_step_start":
        message = f"[pipeline] {step} START {name}"
    elif event == "pipeline_step_skipped":
        message = f"[pipeline] {step} SKIP  {name}"
    else:
        status = str(payload.get("status") or "").upper()
        duration = float(payload.get("duration_seconds") or 0.0)
        return_code = payload.get("return_code")
        suffix = f" rc={return_code}" if return_code not in (None, 0) else ""
        message = f"[pipeline] {step} {status:<6} {name} ({duration:.1f}s){suffix}"
    print(message, file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
