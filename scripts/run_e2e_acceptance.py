#!/usr/bin/env python3
"""Run reproducible sparse/hybrid/LLM acceptance scenarios."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any
from urllib import error, parse, request

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from diagnostic_platform.evaluation.regression import run_regression_evaluation  # noqa: E402
from diagnostic_platform.indexing.vector_index import (  # noqa: E402
    default_vector_manifest_path,
    load_vector_manifest,
    validate_vector_index_manifest,
)
from diagnostic_platform.orchestration.pipeline import run_pipeline  # noqa: E402
from diagnostic_platform.orchestration.workload import (  # noqa: E402
    WorkloadManifest,
    inject_workload_manifest_path,
    load_workload_manifest,
    workload_to_pipeline_config,
    workload_to_regression_config,
)


SCENARIOS = [
    {
        "name": "sparse_deterministic",
        "description": "Sparse/graph retrieval with deterministic XML generation.",
        "enable_hybrid": False,
        "enable_llm": False,
        "generation_mode": "deterministic",
    },
    {
        "name": "hybrid_deterministic",
        "description": "Dense+sparse hybrid retrieval with deterministic XML generation.",
        "enable_hybrid": True,
        "enable_llm": False,
        "generation_mode": "deterministic",
    },
    {
        "name": "hybrid_llm_xml",
        "description": "Dense+sparse hybrid retrieval with LLM operation-level XML generation.",
        "enable_hybrid": True,
        "enable_llm": True,
        "generation_mode": "llm_node",
    },
]

OUTPUT_PATH_KEYS = {
    "xml_plan",
    "serial_xml",
    "operation_xml_dir",
    "flow_evidence_trace",
    "diagnostic_graph",
    "llm_xml_plan",
    "llm_serial_xml",
    "llm_operation_xml_dir",
    "llm_generation_trace",
    "llm_raw_source_dir",
    "fused_workflow",
    "arg_audit_report",
    "audit_resolution_report",
    "llm_resolution_report",
    "workload_replay_dataset",
    "tp2_comm_diagnosis",
    "regression_report",
    "pipeline_report",
}

INPUT_PATH_KEYS = {
    "flow_path",
    "index_dir",
    "base_workflow",
    "template_registry",
    "template_contracts",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", default="configs/workloads/treg_20260402_hybrid_llm.json")
    parser.add_argument("--output-path", default="")
    parser.add_argument("--summary-path", default="")
    parser.add_argument("--milvus-uri", default="")
    parser.add_argument("--vllm-base-url", default="")
    parser.add_argument("--embedding-model", default="")
    rebuild = parser.add_mutually_exclusive_group()
    rebuild.add_argument("--rebuild-vector-index", action="store_true")
    rebuild.add_argument("--no-rebuild-vector-index", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Write scenario manifests and commands without executing them.")
    parser.add_argument("--skip-preflight", action="store_true", help="Run scenarios even when service probes fail.")
    args = parser.parse_args()

    workload_path = Path(args.workload)
    manifest = load_workload_manifest(workload_path)
    milvus_uri = args.milvus_uri or _manifest_milvus_uri(manifest)
    vllm_base_url = args.vllm_base_url or _manifest_vllm_base_url(manifest)
    embedding_model = args.embedding_model or _manifest_embedding_model(manifest)
    rebuild_vector_index = _rebuild_value(args, manifest)
    output_path = Path(args.output_path or manifest.paths.get("acceptance_report") or _default_report_path(manifest))
    summary_path = Path(args.summary_path or manifest.paths.get("acceptance_summary") or output_path.with_suffix(".md"))

    started = time.monotonic()
    service_checks = _service_checks(
        milvus_uri=milvus_uri,
        vllm_base_url=vllm_base_url,
        embedding_model=embedding_model,
    )
    scenario_results = []
    scenario_manifest_dir = output_path.parent / "manifests" / manifest.workload
    for scenario in SCENARIOS:
        scenario_manifest = _scenario_manifest(
            manifest,
            workload_path=workload_path,
            scenario=scenario,
            milvus_uri=milvus_uri,
            vllm_base_url=vllm_base_url,
            embedding_model=embedding_model,
            rebuild_vector_index=rebuild_vector_index,
        )
        scenario_manifest_path = scenario_manifest_dir / f"{scenario['name']}.json"
        _write_json(scenario_manifest_path, scenario_manifest.model_dump(mode="json"))
        result = _run_scenario(
            scenario=scenario,
            manifest=scenario_manifest,
            manifest_path=scenario_manifest_path,
            service_checks=service_checks,
            dry_run=args.dry_run,
            skip_preflight=args.skip_preflight,
        )
        scenario_results.append(result)

    valid = bool(args.dry_run) or all(item.get("valid") for item in scenario_results)
    report = {
        "valid": valid,
        "dry_run": bool(args.dry_run),
        "workload": manifest.workload,
        "workload_path": str(workload_path),
        "duration_seconds": round(time.monotonic() - started, 6),
        "service_checks": service_checks,
        "scenarios": scenario_results,
        "summary": _acceptance_summary(scenario_results),
    }
    _write_json(output_path, report)
    _write_text(summary_path, _markdown_summary(report))
    print(
        json.dumps(
            {
                "valid": report["valid"],
                "dry_run": report["dry_run"],
                "workload": report["workload"],
                "output_path": str(output_path),
                "summary_path": str(summary_path),
                "summary": report["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["valid"] else 2


def _run_scenario(
    *,
    scenario: dict[str, Any],
    manifest: WorkloadManifest,
    manifest_path: Path,
    service_checks: dict[str, Any],
    dry_run: bool,
    skip_preflight: bool,
) -> dict[str, Any]:
    enable_llm = bool(scenario["enable_llm"])
    use_llm_xml = enable_llm and bool(manifest.generation.get("use_llm_xml_for_fusion"))
    pipeline_config = inject_workload_manifest_path(
        workload_to_pipeline_config(manifest, enable_llm=enable_llm),
        manifest_path,
    )
    regression_config = workload_to_regression_config(manifest, use_llm_xml=use_llm_xml)
    commands = _pipeline_commands(pipeline_config)
    blocked = [] if skip_preflight or dry_run else _blocked_reasons(scenario, service_checks)
    base_result: dict[str, Any] = {
        "name": scenario["name"],
        "description": scenario["description"],
        "valid": False,
        "status": "pending",
        "manifest_path": str(manifest_path),
        "commands": commands,
        "pipeline_report_path": manifest.paths.get("pipeline_report", ""),
        "regression_report_path": manifest.paths.get("regression_report", ""),
        "metrics": {},
        "failed_samples": [],
    }
    if dry_run:
        return {
            **base_result,
            "valid": True,
            "status": "dry_run",
            "pipeline": {"valid": None, "summary": {"steps": len(commands)}, "failed_steps": []},
            "regression": {"valid": None, "checks": 0, "failed_checks": []},
            "vector_manifest": _vector_manifest_report(manifest, include_collection=False),
        }
    if blocked:
        return {
            **base_result,
            "status": "blocked",
            "blocked_reasons": blocked,
            "pipeline": {"valid": False, "summary": {"blocked": True}, "failed_steps": []},
            "regression": {"valid": False, "checks": 0, "failed_checks": []},
            "vector_manifest": _vector_manifest_report(manifest, include_collection=False),
            "failed_samples": [{"stage": "preflight", "message": reason} for reason in blocked],
        }

    pipeline_report = run_pipeline(config=pipeline_config, repo_root=REPO_ROOT)
    pipeline_report_path_value = str(manifest.paths.get("pipeline_report") or "")
    if pipeline_report_path_value:
        pipeline_report_path = Path(pipeline_report_path_value)
        _write_json(pipeline_report_path, pipeline_report.model_dump(mode="json"))

    regression_report = run_regression_evaluation(regression_config)
    regression_report_path_value = str(manifest.paths.get("regression_report") or "")
    if regression_report_path_value:
        regression_report_path = Path(regression_report_path_value)
        _write_json(regression_report_path, regression_report.model_dump(mode="json"))

    pipeline_failed = _failed_steps(pipeline_report.model_dump(mode="json").get("steps") or [])
    regression_failed = [
        check.model_dump(mode="json")
        for check in regression_report.checks
        if check.status == "fail"
    ]
    metrics = dict(regression_report.metrics)
    critical_checks = [check for check in regression_report.checks if check.name.startswith("critical_arg")]
    metrics["critical_arg_checks"] = len(critical_checks)
    metrics["critical_arg_passed"] = sum(1 for check in critical_checks if check.status == "pass")
    result = {
        **base_result,
        "valid": bool(pipeline_report.valid and regression_report.valid),
        "status": "passed" if pipeline_report.valid and regression_report.valid else "failed",
        "pipeline": {
            "valid": pipeline_report.valid,
            "duration_seconds": pipeline_report.duration_seconds,
            "summary": pipeline_report.summary,
            "failed_steps": pipeline_failed,
        },
        "regression": {
            "valid": regression_report.valid,
            "checks": len(regression_report.checks),
            "failed_checks": regression_failed[:20],
        },
        "metrics": metrics,
        "vector_manifest": _vector_manifest_report(manifest, include_collection=bool(scenario["enable_hybrid"])),
    }
    result["failed_samples"] = _failed_samples(result)
    result["summary"] = _scenario_summary(result)
    return result


def _scenario_manifest(
    manifest: WorkloadManifest,
    *,
    workload_path: Path,
    scenario: dict[str, Any],
    milvus_uri: str,
    vllm_base_url: str,
    embedding_model: str,
    rebuild_vector_index: bool,
) -> WorkloadManifest:
    payload = copy.deepcopy(manifest.model_dump(mode="json"))
    scenario_name = str(scenario["name"])
    payload["workload"] = f"{manifest.workload}_{scenario_name}"
    payload["description"] = scenario["description"]
    payload["paths"] = _scenario_paths(manifest.paths, manifest.workload, scenario_name)
    expectations = dict(payload.get("regression_expectations") or {})
    if not scenario["enable_hybrid"]:
        for key in [
            "operation_dense_retrieval_coverage_min",
            "operation_hybrid_retrieval_coverage_min",
            "dense_recall_at_k_min",
            "hybrid_recall_at_k_min",
            "dense_mrr_min",
            "hybrid_mrr_min",
            "average_dense_latency_seconds_max",
        ]:
            expectations.pop(key, None)
    if not scenario["enable_llm"]:
        for key in ["llm_valid_generations_min", "llm_needs_review_max", "llm_repair_attempts_max"]:
            expectations.pop(key, None)
        expectations["audit_review_items_max"] = max(int(expectations.get("audit_review_items_max") or 0), 42)
        expectations["resolution_review_required_max"] = max(
            int(expectations.get("resolution_review_required_max") or 0),
            6,
        )
        expectations.pop("post_guardrail_critical_arg_accuracy_min", None)
        expectations.pop("guardrail_correction_count_max", None)
        payload["node_golden_checks"] = []
    payload["regression_expectations"] = expectations

    generation = dict(payload.get("generation") or {})
    generation["mode"] = scenario["generation_mode"]
    generation["use_llm_xml_for_fusion"] = bool(scenario["enable_llm"])
    llm_xml = dict(generation.get("llm_xml") or {})
    llm_xml["vllm_base_url"] = vllm_base_url
    generation["llm_xml"] = llm_xml
    payload["generation"] = generation

    vector_index = dict(payload.get("vector_index") or {})
    hybrid_retrieval = dict(payload.get("hybrid_retrieval") or {})
    if not vector_index.get("collection_name") and hybrid_retrieval.get("vector_collection"):
        vector_index["collection_name"] = hybrid_retrieval["vector_collection"]
    if not hybrid_retrieval.get("vector_collection") and vector_index.get("collection_name"):
        hybrid_retrieval["vector_collection"] = vector_index["collection_name"]
    vector_index["milvus_uri"] = milvus_uri
    hybrid_retrieval["milvus_uri"] = milvus_uri
    vector_index["embedding_model"] = embedding_model
    hybrid_retrieval["embedding_model"] = embedding_model
    vector_index["enabled"] = bool(scenario["enable_hybrid"] and rebuild_vector_index)
    hybrid_retrieval["enabled"] = bool(scenario["enable_hybrid"])
    payload["vector_index"] = vector_index
    payload["hybrid_retrieval"] = hybrid_retrieval

    payload["paths"]["acceptance_source_workload"] = str(workload_path)
    return WorkloadManifest.model_validate(payload)


def _scenario_paths(paths: dict[str, str], workload: str, scenario_name: str) -> dict[str, str]:
    rewritten = dict(paths)
    processed_root = Path("data/processed/acceptance") / workload / scenario_name
    reports_root = Path("data/reports/acceptance") / workload / scenario_name
    for key in list(rewritten):
        if key in INPUT_PATH_KEYS:
            continue
        if key == "acceptance_report":
            rewritten[key] = str(reports_root / "acceptance_report.json")
        elif key == "acceptance_summary":
            rewritten[key] = str(reports_root / "acceptance_summary.md")
        elif key in OUTPUT_PATH_KEYS:
            rewritten[key] = str(_rewritten_output_path(key, processed_root, reports_root))
    return rewritten


def _rewritten_output_path(key: str, processed_root: Path, reports_root: Path) -> Path:
    return {
        "xml_plan": processed_root / "deterministic" / "xml_plan.json",
        "serial_xml": processed_root / "deterministic" / "serial_node.xml",
        "operation_xml_dir": processed_root / "deterministic" / "operations",
        "flow_evidence_trace": processed_root / "deterministic" / "flow_evidence_trace.json",
        "diagnostic_graph": processed_root / "deterministic" / "diagnostic_graph.json",
        "llm_xml_plan": processed_root / "llm" / "llm_xml_plan.json",
        "llm_serial_xml": processed_root / "llm" / "serial_node.xml",
        "llm_operation_xml_dir": processed_root / "llm" / "operations",
        "llm_generation_trace": processed_root / "llm" / "llm_generation_trace.json",
        "llm_raw_source_dir": processed_root / "llm" / "raw_sources",
        "fused_workflow": processed_root / "workflow" / "fused_workflow.xml",
        "arg_audit_report": processed_root / "workflow" / "arg_audit_report.json",
        "audit_resolution_report": processed_root / "workflow" / "audit_resolution_report.json",
        "llm_resolution_report": processed_root / "workflow" / "llm_resolution_report.json",
        "workload_replay_dataset": processed_root / "replay" / "workload_replay.json",
        "tp2_comm_diagnosis": reports_root / "tp2_comm_diagnosis.json",
        "regression_report": reports_root / "regression_report.json",
        "pipeline_report": reports_root / "pipeline_report.json",
    }[key]


def _service_checks(*, milvus_uri: str, vllm_base_url: str, embedding_model: str) -> dict[str, Any]:
    return {
        "milvus": _tcp_check(milvus_uri, default_port=19530),
        "vllm": _vllm_check(vllm_base_url),
        "embedding_model": _embedding_model_check(embedding_model),
        "python_dependencies": _python_dependency_check(embedding_model),
    }


def _blocked_reasons(scenario: dict[str, Any], checks: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if scenario["enable_hybrid"]:
        if not checks.get("milvus", {}).get("ok"):
            reasons.append(f"Milvus unavailable: {checks.get('milvus', {}).get('message')}")
        if not checks.get("embedding_model", {}).get("ok"):
            reasons.append(f"Embedding model unavailable: {checks.get('embedding_model', {}).get('message')}")
        if not checks.get("python_dependencies", {}).get("ok"):
            reasons.append(f"Vector Python dependencies unavailable: {checks.get('python_dependencies', {}).get('message')}")
    if scenario["enable_llm"] and not checks.get("vllm", {}).get("ok"):
        reasons.append(f"vLLM unavailable: {checks.get('vllm', {}).get('message')}")
    return reasons


def _tcp_check(uri: str, *, default_port: int) -> dict[str, Any]:
    parsed = parse.urlparse(uri if "://" in uri else f"tcp://{uri}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or default_port
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=2.0):
            return {
                "ok": True,
                "uri": uri,
                "host": host,
                "port": port,
                "latency_seconds": round(time.monotonic() - started, 6),
                "message": "tcp connection succeeded",
            }
    except OSError as exc:
        return {
            "ok": False,
            "uri": uri,
            "host": host,
            "port": port,
            "latency_seconds": round(time.monotonic() - started, 6),
            "message": str(exc),
        }


def _vllm_check(base_url: str) -> dict[str, Any]:
    tcp = _tcp_check(base_url, default_port=8008)
    models_url = f"{base_url.rstrip('/')}/models"
    if not tcp["ok"]:
        return {**tcp, "models_url": models_url}
    started = time.monotonic()
    try:
        with request.urlopen(models_url, timeout=2.0) as response:
            payload = response.read(4096).decode("utf-8", errors="replace")
            return {
                **tcp,
                "ok": 200 <= int(response.status) < 300,
                "models_url": models_url,
                "http_status": int(response.status),
                "latency_seconds": round(time.monotonic() - started, 6),
                "message": "models endpoint succeeded",
                "body_preview": payload[:512],
            }
    except error.HTTPError as exc:
        return {
            **tcp,
            "ok": False,
            "models_url": models_url,
            "http_status": exc.code,
            "latency_seconds": round(time.monotonic() - started, 6),
            "message": str(exc),
        }
    except OSError as exc:
        return {
            **tcp,
            "ok": False,
            "models_url": models_url,
            "latency_seconds": round(time.monotonic() - started, 6),
            "message": str(exc),
        }


def _embedding_model_check(value: str) -> dict[str, Any]:
    if value.startswith("hashing:"):
        return {"ok": True, "path": value, "message": "hashing test embedding"}
    path = Path(value)
    if path.exists():
        return {"ok": True, "path": value, "message": "path exists"}
    if path.is_absolute():
        return {"ok": False, "path": value, "message": "absolute model path does not exist"}
    return {"ok": True, "path": value, "message": "model id will be resolved by embedding backend"}


def _python_dependency_check(embedding_model: str) -> dict[str, Any]:
    required = ["pymilvus"]
    if not embedding_model.startswith("hashing:"):
        required.append("sentence_transformers")
    availability = {name: importlib.util.find_spec(name) is not None for name in required}
    missing = [name for name, available in availability.items() if not available]
    return {
        "ok": not missing,
        "required": required,
        "availability": availability,
        "message": "all required packages importable" if not missing else f"missing packages: {', '.join(missing)}",
    }


def _pipeline_commands(pipeline_config: dict[str, Any]) -> list[dict[str, Any]]:
    commands = []
    for step in pipeline_config.get("steps") or []:
        commands.append(
            {
                "name": step.get("name"),
                "enabled": bool(step.get("enabled", True)),
                "required": bool(step.get("required", True)),
                "command": _resolve_command(step.get("command") or []),
            }
        )
    return commands


def _resolve_command(command: list[Any]) -> list[str]:
    resolved = []
    for part in command:
        value = str(part)
        if value == "{python}":
            resolved.append(sys.executable)
        elif value == "{repo_root}":
            resolved.append(str(REPO_ROOT))
        else:
            resolved.append(value)
    return resolved


def _failed_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failed = []
    for step in steps:
        if step.get("status") != "failed":
            continue
        failed.append(
            {
                "name": step.get("name"),
                "return_code": step.get("return_code"),
                "message": step.get("message"),
                "stderr_tail": str(step.get("stderr") or "")[-2000:],
            }
        )
    return failed


def _failed_samples(result: dict[str, Any]) -> list[dict[str, Any]]:
    samples = []
    for step in result.get("pipeline", {}).get("failed_steps") or []:
        samples.append({"stage": "pipeline", **step})
    for check in result.get("regression", {}).get("failed_checks") or []:
        samples.append(
            {
                "stage": "regression",
                "name": check.get("name"),
                "actual": check.get("actual"),
                "expected": check.get("expected"),
                "message": check.get("message"),
            }
        )
    return samples[:30]


def _scenario_summary(result: dict[str, Any]) -> dict[str, Any]:
    metrics = result.get("metrics") or {}
    failed_checks = result.get("regression", {}).get("failed_checks") or []
    critical_failures = [item for item in failed_checks if str(item.get("name") or "").startswith("critical_arg")]
    critical_total = int(metrics.get("critical_arg_checks") or len(critical_failures))
    critical_passed = int(metrics.get("critical_arg_passed") or max(critical_total - len(critical_failures), 0))
    return {
        "critical_arg_accuracy": None if critical_total == 0 else round(critical_passed / critical_total, 6),
        "raw_llm_critical_arg_accuracy": metrics.get("raw_llm_critical_arg_accuracy"),
        "post_guardrail_critical_arg_accuracy": metrics.get("post_guardrail_critical_arg_accuracy"),
        "guardrail_correction_count": metrics.get("guardrail_correction_count"),
        "guardrail_corrections_by_arg": metrics.get("guardrail_corrections_by_arg"),
        "semantic_default_usage_count": metrics.get("semantic_default_usage_count"),
        "review_items": (metrics.get("audit_summary") or {}).get("review_items"),
        "resolution_review_required": (metrics.get("resolution_summary") or {}).get("review_required"),
        "dense_recall_at_k": metrics.get("dense_recall_at_k"),
        "hybrid_recall_at_k": metrics.get("hybrid_recall_at_k"),
        "dense_mrr": metrics.get("dense_mrr"),
        "hybrid_mrr": metrics.get("hybrid_mrr"),
        "average_dense_latency_seconds": metrics.get("average_dense_latency_seconds"),
        "llm_valid_generations": metrics.get("llm_valid_generations"),
        "llm_needs_review": metrics.get("llm_needs_review"),
        "llm_repair_attempts": metrics.get("llm_repair_attempts"),
        "prompt_estimated_tokens_total": metrics.get("prompt_estimated_tokens_total"),
        "output_estimated_tokens_total": metrics.get("output_estimated_tokens_total"),
        "average_generation_latency_seconds": metrics.get("average_generation_latency_seconds"),
    }


def _acceptance_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "scenarios": len(results),
        "passed": sum(1 for item in results if item.get("status") == "passed"),
        "failed": sum(1 for item in results if item.get("status") == "failed"),
        "blocked": sum(1 for item in results if item.get("status") == "blocked"),
        "dry_run": sum(1 for item in results if item.get("status") == "dry_run"),
    }


def _vector_manifest_report(manifest: WorkloadManifest, *, include_collection: bool) -> dict[str, Any]:
    vector_path = _vector_manifest_path(manifest)
    if not vector_path.exists():
        return {"exists": False, "path": str(vector_path)}
    try:
        vector_manifest = load_vector_manifest(vector_path)
        validation = (
            _validate_vector_collection_in_subprocess(vector_path)
            if include_collection
            else validate_vector_index_manifest(vector_manifest)
        )
        return {
            "exists": True,
            "path": str(vector_path),
            "manifest": {
                "collection_name": vector_manifest.collection_name,
                "milvus_uri": vector_manifest.milvus_uri,
                "embedding_model": vector_manifest.embedding_model,
                "embedding_dimension": vector_manifest.embedding_dimension,
                "metric_type": vector_manifest.metric_type,
                "vector_count": vector_manifest.vector_count,
                "source_hashes": vector_manifest.source_hashes,
                "built_at": vector_manifest.built_at,
            },
            "validation": validation,
        }
    except Exception as exc:  # noqa: BLE001 - report validation errors instead of hiding them.
        return {"exists": True, "path": str(vector_path), "error": str(exc)}


def _validate_vector_collection_in_subprocess(vector_path: Path) -> dict[str, Any]:
    code = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path.cwd() / 'src'))\n"
        "from diagnostic_platform.indexing.vector_index import load_vector_manifest, validate_vector_index_manifest\n"
        "from diagnostic_platform.retrieval.vector_store import MilvusVectorStore\n"
        "manifest = load_vector_manifest(Path(sys.argv[1]))\n"
        "store = MilvusVectorStore(uri=manifest.milvus_uri, collection_name=manifest.collection_name)\n"
        "print(json.dumps(validate_vector_index_manifest(manifest, vector_store=store), ensure_ascii=False))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code, str(vector_path)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        return {
            "valid": False,
            "errors": ["collection_validation_subprocess_failed"],
            "checks": {},
            "stderr_tail": completed.stderr[-2000:],
        }
    return json.loads(completed.stdout)


def _vector_manifest_path(manifest: WorkloadManifest) -> Path:
    configured = (
        manifest.hybrid_retrieval.get("vector_manifest_path")
        or manifest.vector_index.get("manifest_path")
        or manifest.vector_index.get("vector_manifest_path")
    )
    if configured:
        return Path(str(configured))
    return default_vector_manifest_path(manifest.paths["index_dir"])


def _manifest_milvus_uri(manifest: WorkloadManifest) -> str:
    return str(
        manifest.vector_index.get("milvus_uri")
        or manifest.hybrid_retrieval.get("milvus_uri")
        or "http://127.0.0.1:19530"
    )


def _manifest_vllm_base_url(manifest: WorkloadManifest) -> str:
    llm_xml = manifest.generation.get("llm_xml") or {}
    return str(llm_xml.get("vllm_base_url") or manifest.generation.get("vllm_base_url") or "http://127.0.0.1:8008/v1")


def _manifest_embedding_model(manifest: WorkloadManifest) -> str:
    return str(
        manifest.vector_index.get("embedding_model")
        or manifest.hybrid_retrieval.get("embedding_model")
        or "/home/xdu/LLM/models/Qwen3-Embedding-0.6B"
    )


def _rebuild_value(args: argparse.Namespace, manifest: WorkloadManifest) -> bool:
    if args.rebuild_vector_index:
        return True
    if args.no_rebuild_vector_index:
        return False
    return bool(manifest.vector_index.get("enabled", False))


def _default_report_path(manifest: WorkloadManifest) -> str:
    return f"data/reports/acceptance/{manifest.workload}_acceptance.json"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _markdown_summary(report: dict[str, Any]) -> str:
    lines = [
        "# E2E Acceptance Summary",
        "",
        f"- workload: `{report['workload']}`",
        f"- valid: `{report['valid']}`",
        f"- dry_run: `{report['dry_run']}`",
        f"- duration_seconds: `{report['duration_seconds']}`",
        "",
        "## Service Checks",
        "",
        "| service | ok | message |",
        "| --- | --- | --- |",
    ]
    for name, check in (report.get("service_checks") or {}).items():
        lines.append(f"| {name} | {check.get('ok')} | {_md_escape(str(check.get('message') or ''))} |")
    lines.extend(
        [
            "",
            "## Scenarios",
            "",
            "| scenario | status | pipeline | regression | dense recall | hybrid recall | review items | raw crit | post crit | guardrails | llm valid | failed samples |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for scenario in report.get("scenarios") or []:
        metrics = scenario.get("metrics") or {}
        summary = scenario.get("summary") or {}
        lines.append(
            "| {name} | {status} | {pipeline} | {regression} | {dense} | {hybrid} | {review} | {raw} | {post} | {guardrails} | {llm_valid} | {failed} |".format(
                name=scenario.get("name"),
                status=scenario.get("status"),
                pipeline=scenario.get("pipeline", {}).get("valid"),
                regression=scenario.get("regression", {}).get("valid"),
                dense=_metric(metrics, "dense_recall_at_k"),
                hybrid=_metric(metrics, "hybrid_recall_at_k"),
                review=summary.get("review_items"),
                raw=summary.get("raw_llm_critical_arg_accuracy"),
                post=summary.get("post_guardrail_critical_arg_accuracy"),
                guardrails=summary.get("guardrail_correction_count"),
                llm_valid=summary.get("llm_valid_generations"),
                failed=len(scenario.get("failed_samples") or []),
            )
        )
    lines.extend(["", "## Failed Samples", ""])
    for scenario in report.get("scenarios") or []:
        samples = scenario.get("failed_samples") or []
        if not samples:
            continue
        lines.append(f"### {scenario.get('name')}")
        for sample in samples[:10]:
            lines.append(f"- `{sample.get('stage')}` `{sample.get('name') or sample.get('message') or ''}`")
        lines.append("")
    if lines[-1] != "":
        lines.append("")
    return "\n".join(lines)


def _metric(metrics: dict[str, Any], key: str) -> Any:
    value = metrics.get(key)
    return "" if value is None else value


def _md_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    raise SystemExit(main())
