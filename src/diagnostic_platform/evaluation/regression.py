"""Regression evaluation for the PDF-to-workflow XML pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from diagnostic_platform.schemas import XmlValidationRequest
from diagnostic_platform.validation.xml_validator import validate_xml


CheckStatus = Literal["pass", "fail"]


class RegressionCheck(BaseModel):
    """One regression assertion result."""

    name: str
    status: CheckStatus
    actual: Any = None
    expected: Any = None
    message: str = ""


class RegressionReport(BaseModel):
    """Machine-readable regression evaluation report."""

    valid: bool
    workload: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    checks: list[RegressionCheck] = Field(default_factory=list)


def run_regression_evaluation(config: dict[str, Any]) -> RegressionReport:
    """Evaluate generated artifacts for one fixed workload."""

    workload = str(config.get("workload") or "default")
    paths = config.get("paths") or {}
    generation = config.get("generation") or {}
    expectations = config.get("expectations") or {}
    retrieval_eval = _load_retrieval_eval(config.get("retrieval_eval") or {})
    critical_args = config.get("critical_args") or []
    node_golden_checks = config.get("node_golden_checks") or []
    active_plan = str(generation.get("active_plan") or "xml_plan")
    use_llm_xml = active_plan == "llm_xml_plan"

    checks: list[RegressionCheck] = []
    metrics: dict[str, Any] = {}

    required_paths = [
        "xml_plan",
        "flow_evidence_trace",
        "diagnostic_graph",
        "serial_xml",
        "fused_workflow",
        "arg_audit_report",
        "audit_resolution_report",
        "operation_xml_dir",
    ]
    resolved_paths = {name: Path(str(paths.get(name) or "")) for name in required_paths}
    for name, path in resolved_paths.items():
        _check(checks, f"artifact_exists:{name}", path.exists(), actual=str(path), expected="existing path")
    llm_trace_value = str(paths.get("llm_generation_trace") or "")
    llm_trace_path = Path(llm_trace_value)
    if use_llm_xml:
        _check(
            checks,
            "artifact_exists:llm_generation_trace",
            bool(llm_trace_value) and llm_trace_path.exists(),
            actual=llm_trace_value,
            expected="existing path",
        )

    if any(check.status == "fail" for check in checks):
        return RegressionReport(valid=False, workload=workload, metrics=metrics, checks=checks)

    xml_plan_payload = _load_json(resolved_paths["xml_plan"])
    trace_payload = _load_json(resolved_paths["flow_evidence_trace"])
    graph_payload = _load_json(resolved_paths["diagnostic_graph"])
    arg_audit_payload = _load_json(resolved_paths["arg_audit_report"])
    resolution_payload = _load_json(resolved_paths["audit_resolution_report"])

    plan = xml_plan_payload["plan"]
    flow_plan = xml_plan_payload.get("flow_plan") or {}
    validation = xml_plan_payload.get("validation") or xml_plan_payload.get("plan_validation") or {}
    nodes = plan.get("nodes") or []
    scripts = (plan.get("serial") or {}).get("scripts") or []
    operation_xml_files = xml_plan_payload.get("operation_xml_files") or []
    trace_steps = trace_payload.get("steps") or []
    trace_operations = [operation for step in trace_steps for operation in step.get("operations", [])]
    plan_args = [arg for script in scripts for arg in script.get("args", [])]

    serial_xml_result = validate_xml(
        XmlValidationRequest(
            xml=resolved_paths["serial_xml"].read_text(encoding="utf-8"),
            min_script_nodes=int(expectations.get("serial_script_nodes", 1)),
        )
    )
    fused_xml_result = validate_xml(
        XmlValidationRequest(
            xml=resolved_paths["fused_workflow"].read_text(encoding="utf-8"),
            min_script_nodes=int(expectations.get("fused_script_nodes_min", 1)),
        )
    )

    op_xml_paths = sorted(resolved_paths["operation_xml_dir"].glob("*.xml"))
    operation_xml_valid = 0
    for path in op_xml_paths:
        result = validate_xml(XmlValidationRequest(xml=path.read_text(encoding="utf-8"), min_script_nodes=1))
        if result.valid:
            operation_xml_valid += 1

    args_with_scores = sum(1 for arg in plan_args if arg.get("selection_score") is not None)
    operations_with_graph_paths = sum(1 for operation in trace_operations if operation.get("graph_paths"))
    operations_with_matches = sum(1 for operation in trace_operations if operation.get("retrieval_matches"))
    operations_with_dense_matches = sum(1 for operation in trace_operations if _operation_has_retrieval_source(operation, "dense"))
    operations_with_hybrid_matches = sum(1 for operation in trace_operations if _operation_has_retrieval_source(operation, "hybrid"))
    graph_path_count = sum(len(operation.get("graph_paths") or []) for operation in trace_operations)
    arg_score_coverage = _safe_ratio(args_with_scores, len(plan_args))
    operation_graph_path_coverage = _safe_ratio(operations_with_graph_paths, len(trace_operations))
    operation_retrieval_coverage = _safe_ratio(operations_with_matches, len(trace_operations))
    operation_dense_retrieval_coverage = _safe_ratio(operations_with_dense_matches, len(trace_operations))
    operation_hybrid_retrieval_coverage = _safe_ratio(operations_with_hybrid_matches, len(trace_operations))

    metrics.update(
        {
            "flow_steps": len(flow_plan.get("steps") or []),
            "planned_nodes": len(nodes),
            "script_nodes": len(scripts),
            "plan_args": len(plan_args),
            "args_with_selection_scores": args_with_scores,
            "arg_selection_score_coverage": round(arg_score_coverage, 6),
            "trace_steps": len(trace_steps),
            "trace_operations": len(trace_operations),
            "operations_with_retrieval_matches": operations_with_matches,
            "operation_retrieval_coverage": round(operation_retrieval_coverage, 6),
            "operations_with_dense_retrieval_matches": operations_with_dense_matches,
            "operation_dense_retrieval_coverage": round(operation_dense_retrieval_coverage, 6),
            "operations_with_hybrid_retrieval_matches": operations_with_hybrid_matches,
            "operation_hybrid_retrieval_coverage": round(operation_hybrid_retrieval_coverage, 6),
            "operations_with_graph_paths": operations_with_graph_paths,
            "operation_graph_path_coverage": round(operation_graph_path_coverage, 6),
            "graph_paths": graph_path_count,
            "graph_entities": len(graph_payload.get("entities") or []),
            "graph_relations": len(graph_payload.get("relations") or []),
            "operation_xml_files": len(op_xml_paths),
            "operation_xml_valid": operation_xml_valid,
            "serial_xml_stats": serial_xml_result.stats,
            "fused_xml_stats": fused_xml_result.stats,
            "audit_summary": arg_audit_payload.get("summary") or {},
            "resolution_summary": resolution_payload.get("summary") or {},
            "generation_mode": str(generation.get("mode") or "deterministic"),
            "configured_generation_mode": str(generation.get("configured_mode") or generation.get("mode") or "deterministic"),
            "active_plan": active_plan,
        }
    )
    if use_llm_xml:
        llm_trace_payload = _load_json(llm_trace_path)
        llm_metrics = _llm_generation_metrics(llm_trace_payload)
        llm_metrics.update(_llm_guardrail_metrics(llm_trace_payload, critical_args))
        metrics.update(llm_metrics)
        _check_min(
            checks,
            "llm_valid_generations_min",
            llm_metrics.get("llm_valid_generations"),
            expectations.get("llm_valid_generations_min"),
        )
        _check_max(
            checks,
            "llm_needs_review_max",
            llm_metrics.get("llm_needs_review"),
            expectations.get("llm_needs_review_max"),
        )
        _check_max(
            checks,
            "llm_repair_attempts_max",
            llm_metrics.get("llm_repair_attempts"),
            expectations.get("llm_repair_attempts_max"),
        )
        _check_min(
            checks,
            "raw_llm_critical_arg_accuracy_min",
            llm_metrics.get("raw_llm_critical_arg_accuracy"),
            expectations.get("raw_llm_critical_arg_accuracy_min"),
        )
        _check_min(
            checks,
            "post_guardrail_critical_arg_accuracy_min",
            llm_metrics.get("post_guardrail_critical_arg_accuracy"),
            expectations.get("post_guardrail_critical_arg_accuracy_min"),
        )
        _check_max(
            checks,
            "guardrail_correction_count_max",
            llm_metrics.get("guardrail_correction_count"),
            expectations.get("guardrail_correction_count_max"),
        )
    metrics.update(_retrieval_eval_metrics(trace_operations, retrieval_eval))
    evidence_chain_value = str(paths.get("evidence_chain_report") or "")
    evidence_chain_path = Path(evidence_chain_value)
    if evidence_chain_value and evidence_chain_path.is_file():
        metrics.update(_evidence_chain_metrics(_load_json(evidence_chain_path)))

    _check(checks, "xml_plan_validation", bool(validation.get("valid")), actual=validation.get("valid"), expected=True)
    _check_equal(checks, "flow_steps", metrics["flow_steps"], expectations.get("flow_steps"))
    _check_equal(checks, "planned_nodes", metrics["planned_nodes"], expectations.get("planned_nodes"))
    _check_equal(checks, "trace_operations", metrics["trace_operations"], expectations.get("trace_operations"))
    _check_equal(checks, "operation_xml_files", metrics["operation_xml_files"], expectations.get("operation_xml_files"))
    _check_equal(checks, "operation_xml_valid", operation_xml_valid, expectations.get("operation_xml_files"))
    _check(checks, "serial_xml_valid", serial_xml_result.valid, actual=serial_xml_result.stats, expected="valid XML")
    _check_equal(
        checks,
        "serial_script_nodes",
        serial_xml_result.stats.get("script_nodes"),
        expectations.get("serial_script_nodes"),
    )
    _check_equal(
        checks,
        "serial_parallel_nodes",
        serial_xml_result.stats.get("parallel_nodes"),
        expectations.get("serial_parallel_nodes"),
    )
    _check(checks, "fused_xml_valid", fused_xml_result.valid, actual=fused_xml_result.stats, expected="valid XML")
    _check_min(
        checks,
        "fused_script_nodes_min",
        fused_xml_result.stats.get("script_nodes"),
        expectations.get("fused_script_nodes_min"),
    )
    _check_min(
        checks,
        "fused_parallel_nodes_min",
        fused_xml_result.stats.get("parallel_nodes"),
        expectations.get("fused_parallel_nodes_min"),
    )
    _check_min(checks, "graph_entities_min", metrics["graph_entities"], expectations.get("graph_entities_min"))
    _check_min(checks, "graph_relations_min", metrics["graph_relations"], expectations.get("graph_relations_min"))
    _check_min(checks, "graph_paths_min", graph_path_count, expectations.get("graph_paths_min"))
    _check_min(
        checks,
        "arg_selection_score_coverage_min",
        arg_score_coverage,
        expectations.get("arg_selection_score_coverage_min"),
    )
    _check_min(
        checks,
        "operation_graph_path_coverage_min",
        operation_graph_path_coverage,
        expectations.get("operation_graph_path_coverage_min"),
    )
    _check_min(
        checks,
        "operation_retrieval_coverage_min",
        operation_retrieval_coverage,
        expectations.get("operation_retrieval_coverage_min"),
    )
    _check_min(
        checks,
        "operation_dense_retrieval_coverage_min",
        operation_dense_retrieval_coverage,
        expectations.get("operation_dense_retrieval_coverage_min"),
    )
    _check_min(
        checks,
        "operation_hybrid_retrieval_coverage_min",
        operation_hybrid_retrieval_coverage,
        expectations.get("operation_hybrid_retrieval_coverage_min"),
    )
    _check_min(checks, "dense_recall_at_k_min", metrics.get("dense_recall_at_k"), expectations.get("dense_recall_at_k_min"))
    _check_min(checks, "hybrid_recall_at_k_min", metrics.get("hybrid_recall_at_k"), expectations.get("hybrid_recall_at_k_min"))
    _check_min(checks, "dense_mrr_min", metrics.get("dense_mrr"), expectations.get("dense_mrr_min"))
    _check_min(checks, "hybrid_mrr_min", metrics.get("hybrid_mrr"), expectations.get("hybrid_mrr_min"))
    _check_min(
        checks,
        "reference_resolution_rate_min",
        metrics.get("reference_resolution_rate"),
        expectations.get("reference_resolution_rate_min"),
    )
    _check_min(
        checks,
        "evidence_chain_report_coverage_min",
        metrics.get("evidence_chain_report_coverage"),
        expectations.get("evidence_chain_report_coverage_min"),
    )
    _check_min(
        checks,
        "kept_context_primary_doc_coverage_min",
        metrics.get("kept_context_primary_doc_coverage"),
        expectations.get("kept_context_primary_doc_coverage_min"),
    )
    _check_min(
        checks,
        "kept_context_reference_doc_coverage_min",
        metrics.get("kept_context_reference_doc_coverage"),
        expectations.get("kept_context_reference_doc_coverage_min"),
    )
    _check_min(
        checks,
        "filter_decision_trace_coverage_min",
        metrics.get("filter_decision_trace_coverage"),
        expectations.get("filter_decision_trace_coverage_min"),
    )
    _check_max(
        checks,
        "average_dense_latency_seconds_max",
        metrics.get("average_dense_latency_seconds"),
        expectations.get("average_dense_latency_seconds_max"),
    )
    _check_max(
        checks,
        "audit_review_items_max",
        (arg_audit_payload.get("summary") or {}).get("review_items"),
        expectations.get("audit_review_items_max"),
    )
    _check_max(
        checks,
        "resolution_review_required_max",
        (resolution_payload.get("summary") or {}).get("review_required"),
        expectations.get("resolution_review_required_max"),
    )

    _check_critical_args(checks, scripts, critical_args)
    baseline_path = Path(str(paths.get("baseline_xml_plan") or ""))
    if use_llm_xml and baseline_path.exists():
        _check_critical_args_against_baseline(checks, scripts, _load_json(baseline_path), critical_args)
    golden_passed, golden_total = _check_node_golden_checks(checks, scripts, node_golden_checks)
    if golden_total:
        metrics["node_golden_checks"] = golden_total
        metrics["node_golden_passed"] = golden_passed
        metrics["node_golden_accuracy"] = round(_safe_ratio(golden_passed, golden_total), 6)

    return RegressionReport(
        valid=not any(check.status == "fail" for check in checks),
        workload=workload,
        metrics=metrics,
        checks=checks,
    )


def _check_critical_args(checks: list[RegressionCheck], scripts: list[dict[str, Any]], critical_args: list[dict[str, str]]) -> None:
    arg_values = {
        (script.get("node_name"), arg.get("name")): arg.get("value")
        for script in scripts
        for arg in script.get("args", [])
    }
    for item in critical_args:
        node_name = item.get("node_name")
        arg_name = item.get("arg_name")
        expected = item.get("value")
        actual = arg_values.get((node_name, arg_name))
        _check_equal(checks, f"critical_arg:{node_name}.{arg_name}", actual, expected)


def _check_critical_args_against_baseline(
    checks: list[RegressionCheck],
    scripts: list[dict[str, Any]],
    baseline_payload: dict[str, Any],
    critical_args: list[dict[str, str]],
) -> None:
    baseline_plan = baseline_payload.get("plan") or baseline_payload
    baseline_scripts = (baseline_plan.get("serial") or {}).get("scripts") or []
    actual_values = _script_arg_values(scripts)
    baseline_values = _script_arg_values(baseline_scripts)
    for item in critical_args:
        node_name = item.get("node_name")
        arg_name = item.get("arg_name")
        if (node_name, arg_name) not in baseline_values:
            continue
        actual = actual_values.get((node_name, arg_name))
        expected = baseline_values.get((node_name, arg_name))
        _check_equal(checks, f"critical_arg_matches_baseline:{node_name}.{arg_name}", actual, expected)


def _script_arg_values(scripts: list[dict[str, Any]]) -> dict[tuple[Any, Any], Any]:
    return {
        (script.get("node_name"), arg.get("name")): arg.get("value")
        for script in scripts
        for arg in script.get("args", [])
    }


def _check_node_golden_checks(
    checks: list[RegressionCheck],
    scripts: list[dict[str, Any]],
    node_golden_checks: list[dict[str, Any]],
) -> tuple[int, int]:
    if not node_golden_checks:
        return 0, 0
    scripts_by_node = {script.get("node_name"): script for script in scripts}
    passed = 0
    total = 0
    for item in node_golden_checks:
        node_name = item.get("node_name")
        script = scripts_by_node.get(node_name)
        if item.get("class_name") is not None:
            total += 1
            actual = script.get("class_name") if script else None
            expected = item.get("class_name")
            ok = actual == expected
            passed += int(ok)
            _check(checks, f"node_golden:{node_name}.ClassName", ok, actual=actual, expected=expected)
        arg_values = {
            arg.get("name"): arg.get("value")
            for arg in (script.get("args") if script else []) or []
        }
        for arg_name, expected in (item.get("args") or {}).items():
            total += 1
            actual = arg_values.get(arg_name)
            ok = actual == expected
            passed += int(ok)
            _check(checks, f"node_golden:{node_name}.{arg_name}", ok, actual=actual, expected=expected)
    return passed, total


def _llm_generation_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    generations = list(payload.get("generations") or [])
    summary = payload.get("summary") or {}
    valid_generations = sum(1 for item in generations if item.get("valid"))
    needs_review = sum(1 for item in generations if item.get("needs_review"))
    repair_attempts = sum(len(item.get("repair_attempts") or []) for item in generations)
    issue_counts: dict[str, int] = {}
    for item in generations:
        for issue in item.get("xml_validation_errors") or []:
            code = str(issue.get("code") or "UNKNOWN")
            issue_counts[code] = issue_counts.get(code, 0) + 1
    return {
        "llm_generations": int(summary.get("generations") or len(generations)),
        "llm_valid_generations": int(summary.get("valid_generations") or valid_generations),
        "llm_needs_review": int(summary.get("needs_review") or needs_review),
        "llm_repair_attempts": int(summary.get("repair_attempts") or repair_attempts),
        "llm_generation_issue_counts": summary.get("issue_counts") or issue_counts,
        "prompt_estimated_tokens_total": int(summary.get("prompt_estimated_tokens_total") or _sum_generation_field(generations, "prompt_estimated_tokens")),
        "output_estimated_tokens_total": int(summary.get("output_estimated_tokens_total") or _sum_generation_field(generations, "output_estimated_tokens")),
        "average_generation_latency_seconds": float(summary.get("average_generation_latency_seconds") or _average_generation_field(generations, "generation_latency_seconds")),
        "average_repair_latency_seconds": float(summary.get("average_repair_latency_seconds") or _average_generation_field(generations, "repair_latency_seconds")),
    }


def _llm_guardrail_metrics(payload: dict[str, Any], critical_args: list[dict[str, str]]) -> dict[str, Any]:
    generations = list(payload.get("generations") or [])
    summary = payload.get("summary") or {}
    corrections = [
        correction
        for generation in generations
        for correction in generation.get("guardrail_corrections") or []
    ]
    raw_passed, raw_total = _llm_critical_arg_accuracy(generations, "raw_llm_args", critical_args)
    post_passed, post_total = _llm_critical_arg_accuracy(generations, "post_guardrail_args", critical_args)
    return {
        "raw_llm_critical_arg_checks": raw_total,
        "raw_llm_critical_arg_passed": raw_passed,
        "raw_llm_critical_arg_accuracy": round(_safe_ratio(raw_passed, raw_total), 6) if raw_total else None,
        "post_guardrail_critical_arg_checks": post_total,
        "post_guardrail_critical_arg_passed": post_passed,
        "post_guardrail_critical_arg_accuracy": round(_safe_ratio(post_passed, post_total), 6) if post_total else None,
        "guardrail_correction_count": int(summary.get("guardrail_corrections") or len(corrections)),
        "guardrail_corrections_by_arg": summary.get("guardrail_corrections_by_arg")
        or _correction_counts(corrections, "arg_name"),
        "guardrail_corrections_by_source": summary.get("guardrail_corrections_by_source")
        or _correction_counts(corrections, "correction_source"),
        "semantic_default_usage_count": int(
            summary.get("semantic_default_usage_count")
            or sum(1 for correction in corrections if correction.get("correction_source") == "semantic_default")
        ),
    }


def _llm_critical_arg_accuracy(
    generations: list[dict[str, Any]],
    field_name: str,
    critical_args: list[dict[str, str]],
) -> tuple[int, int]:
    values: dict[tuple[Any, Any], Any] = {}
    for generation in generations:
        args = generation.get(field_name) or generation.get("args") or []
        for arg in args:
            values[(generation.get("node_name"), arg.get("name"))] = arg.get("value")
    passed = 0
    total = 0
    for item in critical_args:
        total += 1
        actual = values.get((item.get("node_name"), item.get("arg_name")))
        passed += int(actual == item.get("value"))
    return passed, total


def _correction_counts(corrections: list[dict[str, Any]], field_name: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for correction in corrections:
        key = str(correction.get(field_name) or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _sum_generation_field(generations: list[dict[str, Any]], field_name: str) -> int:
    return sum(int(generation.get(field_name) or 0) for generation in generations)


def _average_generation_field(generations: list[dict[str, Any]], field_name: str) -> float:
    values = [float(generation.get(field_name) or 0.0) for generation in generations if generation.get(field_name)]
    return round(sum(values) / len(values), 6) if values else 0.0


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_retrieval_eval(config: dict[str, Any]) -> dict[str, Any]:
    fixture_path = config.get("fixture_path")
    if not fixture_path:
        return config
    payload = _load_json(Path(str(fixture_path)))
    merged = {**payload, **{key: value for key, value in config.items() if key != "fixture_path"}}
    return merged


def _operation_has_retrieval_source(operation: dict[str, Any], source: str) -> bool:
    for match in operation.get("retrieval_matches") or []:
        if str(match.get("source") or "").lower() == source:
            return True
        metadata = match.get("metadata") or {}
        hybrid_sources = metadata.get("hybrid_sources") or []
        if source in [str(item).lower() for item in hybrid_sources]:
            return True
    trace = operation.get("retrieval_trace") or {}
    if trace.get(source, {}).get("results"):
        return True
    return False


def _retrieval_eval_metrics(trace_operations: list[dict[str, Any]], retrieval_eval: dict[str, Any]) -> dict[str, Any]:
    metrics = {
        "average_dense_latency_seconds": _average_retrieval_latency(trace_operations, "dense"),
        "average_hybrid_latency_seconds": _average_retrieval_latency(trace_operations, "hybrid"),
    }
    cases = list(retrieval_eval.get("cases") or [])
    if not cases:
        return metrics

    top_k = int(retrieval_eval.get("top_k") or 8)
    operation_by_node = {str(operation.get("node_name") or ""): operation for operation in trace_operations}
    for source in ["dense", "hybrid"]:
        hits = 0
        reciprocal_ranks = []
        evaluated = 0
        for case in cases:
            operation = operation_by_node.get(str(case.get("node_name") or ""))
            expected = {str(value) for value in case.get("expected_evidence_ids") or case.get("expected_chunk_ids") or []}
            if operation is None or not expected:
                continue
            evaluated += 1
            ranked_ids = _ranked_retrieval_ids(operation, source)
            first_rank = _first_expected_rank(ranked_ids, expected)
            if first_rank is not None and first_rank <= top_k:
                hits += 1
            reciprocal_ranks.append(0.0 if first_rank is None else 1.0 / first_rank)
        metrics[f"{source}_eval_cases"] = evaluated
        metrics[f"{source}_recall_at_k"] = round(_safe_ratio(hits, evaluated), 6)
        metrics[f"{source}_mrr"] = round(sum(reciprocal_ranks) / len(reciprocal_ranks), 6) if reciprocal_ranks else 0.0
    metrics["retrieval_eval_top_k"] = top_k
    return metrics


def _ranked_retrieval_ids(operation: dict[str, Any], source: str) -> list[str]:
    values: list[str] = []
    for match in operation.get("retrieval_matches") or []:
        metadata = match.get("metadata") or {}
        hybrid_sources = [str(item).lower() for item in metadata.get("hybrid_sources") or []]
        if str(match.get("source") or "").lower() == source or source in hybrid_sources:
            _append_unique(values, str(match.get("evidence_id") or ""))
            _append_unique(values, str(metadata.get("source_id") or ""))
            _append_unique(values, str(metadata.get("source_evidence_id") or ""))
    trace = operation.get("retrieval_trace") or {}
    for item in (trace.get(source) or {}).get("results") or []:
        metadata = item.get("metadata") or {}
        _append_unique(values, str(item.get("chunk_id") or ""))
        _append_unique(values, str(item.get("source_id") or ""))
        _append_unique(values, str(metadata.get("source_id") or ""))
        _append_unique(values, str(metadata.get("source_evidence_id") or ""))
    if source == "hybrid":
        for item in (trace.get("hybrid") or {}).get("results") or []:
            metadata = item.get("metadata") or {}
            _append_unique(values, str(metadata.get("source_id") or ""))
            _append_unique(values, str(metadata.get("source_evidence_id") or ""))
    return [value for value in values if value]


def _evidence_chain_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    nodes = list(payload.get("nodes") or [])
    summary = payload.get("summary") or {}
    node_count = len(nodes)
    with_kept = sum(1 for node in nodes if node.get("kept_evidence_ids"))
    with_filter_trace = sum(1 for node in nodes if node.get("filter_decisions"))
    with_primary_doc = sum(
        1
        for node in nodes
        if str(node.get("parameter_source_markdown") or "") not in {"", "not_found", "not_available", "not_applicable"}
    )
    reference_nodes = [node for node in nodes if node.get("refer_texts")]
    with_reference_doc = sum(1 for node in reference_nodes if node.get("flowchart_status") == "found")
    return {
        "evidence_chain_report_nodes": node_count,
        "evidence_chain_report_coverage": round(_safe_ratio(with_kept, node_count), 6),
        "reference_resolution_rate": float(summary.get("reference_resolution_rate") or _safe_ratio(with_reference_doc, len(reference_nodes))),
        "kept_context_primary_doc_coverage": round(_safe_ratio(with_primary_doc, node_count), 6),
        "kept_context_reference_doc_coverage": round(_safe_ratio(with_reference_doc, len(reference_nodes)), 6)
        if reference_nodes
        else 0.0,
        "filter_decision_trace_coverage": round(_safe_ratio(with_filter_trace, node_count), 6),
    }


def _first_expected_rank(ranked_ids: list[str], expected: set[str]) -> int | None:
    for index, value in enumerate(ranked_ids, start=1):
        if value in expected:
            return index
    return None


def _average_retrieval_latency(trace_operations: list[dict[str, Any]], source: str) -> float:
    values = []
    for operation in trace_operations:
        trace = operation.get("retrieval_trace") or {}
        latency = (trace.get(source) or {}).get("latency_seconds")
        if latency is not None:
            values.append(float(latency))
    return round(sum(values) / len(values), 6) if values else 0.0


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _check(checks: list[RegressionCheck], name: str, condition: bool, actual: Any, expected: Any) -> None:
    checks.append(
        RegressionCheck(
            name=name,
            status="pass" if condition else "fail",
            actual=actual,
            expected=expected,
            message="" if condition else f"{name} expected {expected!r}, got {actual!r}",
        )
    )


def _check_equal(checks: list[RegressionCheck], name: str, actual: Any, expected: Any) -> None:
    if expected is None:
        return
    _check(checks, name, actual == expected, actual=actual, expected=expected)


def _check_min(checks: list[RegressionCheck], name: str, actual: Any, expected_min: Any) -> None:
    if expected_min is None:
        return
    _check(checks, name, actual is not None and actual >= expected_min, actual=actual, expected=f">= {expected_min}")


def _check_max(checks: list[RegressionCheck], name: str, actual: Any, expected_max: Any) -> None:
    if expected_max is None:
        return
    _check(checks, name, actual is not None and actual <= expected_max, actual=actual, expected=f"<= {expected_max}")


def _safe_ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator
