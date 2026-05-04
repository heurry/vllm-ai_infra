"""Real workload replay dataset builders and replay benchmark helpers."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import statistics
from typing import Any, Iterable

from diagnostic_platform.schemas import ReplayWorkloadDataset, ReplayWorkloadItem


def load_replay_dataset(path: Path) -> ReplayWorkloadDataset:
    """Load a replay dataset JSON file."""

    return ReplayWorkloadDataset.model_validate(json.loads(path.read_text(encoding="utf-8")))


def write_replay_dataset(dataset: ReplayWorkloadDataset, path: Path) -> None:
    """Write a replay dataset to disk."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dataset.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_replay_dataset_from_artifacts(
    *,
    workload: str,
    flow_trace_payload: dict[str, Any],
    resolution_payload: dict[str, Any],
    llm_resolution_payload: dict[str, Any] | None = None,
    llm_xml_payload: dict[str, Any] | None = None,
    source_manifest: str | None = None,
) -> ReplayWorkloadDataset:
    """Build a replay dataset from real flow trace and audit artifacts."""

    operations = _collect_operations(flow_trace_payload)
    operation_by_name = {
        str(operation.get("node_name")): operation for operation in operations if str(operation.get("node_name") or "").strip()
    }

    resolution_decisions = list(resolution_payload.get("decisions") or [])
    llm_decisions = list((llm_resolution_payload or {}).get("decisions") or [])
    repair_decisions = [decision for decision in llm_decisions if decision.get("requires_review")]
    if not repair_decisions:
        repair_decisions = [decision for decision in resolution_decisions if decision.get("requires_review")]

    items: list[ReplayWorkloadItem] = []
    items.extend(_build_short_audit_items(resolution_decisions, operation_by_name))
    items.extend(_build_rerank_items(operations))
    items.extend(_build_long_context_items(operations))
    items.extend(_build_repair_items(repair_decisions, operation_by_name))
    items.extend(_build_xml_generation_items(llm_xml_payload or {}))
    items.extend(_build_xml_repair_items(llm_xml_payload or {}))

    profile_counts = Counter(item.profile for item in items)
    prompt_lengths = [len(item.prompt) for item in items]
    return ReplayWorkloadDataset(
        workload=workload,
        source_manifest=source_manifest,
        generated_at=datetime.now(timezone.utc).isoformat(),
        items=items,
        summary={
            "items": len(items),
            "operations": len(operations),
            "review_items": sum(1 for decision in resolution_decisions if decision.get("requires_review")),
            "profiles": dict(sorted(profile_counts.items())),
            "avg_prompt_chars": round(statistics.fmean(prompt_lengths), 2) if prompt_lengths else 0.0,
            "max_prompt_chars": max(prompt_lengths) if prompt_lengths else 0,
        },
    )


def default_replay_output_path(workload: str) -> Path:
    """Default replay dataset path for a workload."""

    return Path("data/processed/replay") / f"{workload}_workload_replay.json"


def aggregate_replay_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-request replay benchmark results."""

    valid = [result for result in results if result.get("valid")]
    metrics: dict[str, Any] = {
        "requests_total": len(results),
        "valid_requests": len(valid),
        "invalid_requests": len(results) - len(valid),
        "success_rate": round(len(valid) / len(results), 6) if results else 0.0,
        "completion_tokens_total": int(sum(int(result.get("completion_tokens") or 0) for result in results)),
    }
    total_duration = sum(float(result.get("duration_seconds") or 0.0) for result in results)
    metrics["wall_time_seconds"] = round(total_duration, 6)
    metrics["requests_per_second"] = round(len(valid) / total_duration, 6) if total_duration > 0 else 0.0
    xml_quality_samples = [result for result in results if result.get("xml_quality_evaluated")]
    if xml_quality_samples:
        xml_valid = sum(1 for result in xml_quality_samples if result.get("xml_valid"))
        xml_needs_review = sum(1 for result in xml_quality_samples if result.get("xml_needs_review"))
        metrics.update(
            {
                "xml_quality_samples": len(xml_quality_samples),
                "xml_valid": xml_valid,
                "xml_valid_rate": round(xml_valid / len(xml_quality_samples), 6),
                "xml_needs_review": xml_needs_review,
                "xml_needs_review_rate": round(xml_needs_review / len(xml_quality_samples), 6),
            }
        )

    numeric_keys = (
        "duration_seconds",
        "ttft_seconds",
        "tpot_seconds",
        "tokens_per_second",
        "prompt_tokens",
        "completion_tokens",
    )
    for key in numeric_keys:
        values = [float(result[key]) for result in results if isinstance(result.get(key), int | float)]
        if values:
            metrics.update(_series_metrics(key, values))
    return metrics


def aggregate_replay_profiles(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate replay results by request profile."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        grouped.setdefault(str(result.get("profile") or "default"), []).append(result)
    return {profile: aggregate_replay_metrics(samples) for profile, samples in sorted(grouped.items())}


def _build_short_audit_items(
    decisions: list[dict[str, Any]],
    operation_by_name: dict[str, dict[str, Any]],
) -> list[ReplayWorkloadItem]:
    items: list[ReplayWorkloadItem] = []
    index = 0
    for decision in decisions:
        if not decision.get("requires_review"):
            continue
        node_name = str(decision.get("node_name") or "").strip()
        arg_name = str(decision.get("arg_name") or "").strip()
        operation = operation_by_name.get(node_name, {})
        prompt = "\n".join(
            [
                "你是诊断流程参数审计助手。",
                "请判断下面这个 workflow 参数冲突应如何处理，并只输出 JSON：",
                '{"action":"keep_base|accept_plan|needs_review","confidence":0.0,"reason":"..."}',
                "",
                f"Node: {node_name}",
                f"Template: {decision.get('class_name') or ''}",
                f"Arg: {arg_name}",
                f"PlanValue: {decision.get('plan_value') or ''}",
                f"BaseValue: {decision.get('base_value') or ''}",
                f"FusedValue: {decision.get('fused_value') or ''}",
                f"Reason: {decision.get('reason') or ''}",
                f"EvidenceIds: {', '.join(decision.get('evidence_ids') or []) or 'None'}",
                f"SelectedArgs: {_render_selected_args(operation.get('selected_args') or [])}",
                f"OperationNotes: {_render_notes(operation.get('notes') or [])}",
            ]
        ).strip()
        items.append(
            ReplayWorkloadItem(
                item_id=_item_id("short_audit", index, node_name, arg_name),
                profile="short_audit",
                prompt=prompt,
                max_tokens=192,
                min_tokens=48,
                cache_mode="warm",
                tags=["audit", "review", "real_trace"],
                metadata={
                    "node_name": node_name,
                    "arg_name": arg_name,
                    "template_name": str(decision.get("class_name") or ""),
                    "expected_action": str(decision.get("action") or ""),
                    "evidence_ids": [str(item) for item in decision.get("evidence_ids") or []],
                    "estimated_prompt_tokens": _estimate_tokens(prompt),
                },
            )
        )
        index += 1
    return items


def _build_rerank_items(operations: list[dict[str, Any]]) -> list[ReplayWorkloadItem]:
    items: list[ReplayWorkloadItem] = []
    index = 0
    for operation in operations:
        matches = list(operation.get("retrieval_matches") or [])
        if not matches:
            continue
        node_name = str(operation.get("node_name") or "").strip()
        prompt = "\n".join(
            [
                "你在做诊断证据重排。",
                "请从候选证据中选出最相关的 3 条 evidence_id，并只输出 JSON：",
                '{"top_evidence_ids":["..."],"reason":"..."}',
                "",
                f"Operation: {node_name}",
                f"Template: {operation.get('template_name') or ''}",
                f"SelectedEvidenceIds: {', '.join(operation.get('selected_evidence_ids') or []) or 'None'}",
                "Candidates:",
                _render_retrieval_matches(matches, limit=5),
            ]
        ).strip()
        items.append(
            ReplayWorkloadItem(
                item_id=_item_id("rerank", index, node_name),
                profile="rerank",
                prompt=prompt,
                max_tokens=160,
                min_tokens=48,
                cache_mode="warm",
                tags=["rerank", "retrieval", "real_trace"],
                metadata={
                    "node_name": node_name,
                    "template_name": str(operation.get("template_name") or ""),
                    "selected_evidence_ids": [str(item) for item in operation.get("selected_evidence_ids") or []],
                    "estimated_prompt_tokens": _estimate_tokens(prompt),
                },
            )
        )
        index += 1
    return items


def _build_long_context_items(operations: list[dict[str, Any]]) -> list[ReplayWorkloadItem]:
    items: list[ReplayWorkloadItem] = []
    index = 0
    for operation in operations:
        node_name = str(operation.get("node_name") or "").strip()
        prompt = "\n".join(
            [
                "你在做诊断流程长上下文校核。",
                "请结合 operation、graph path 和检索证据，输出 JSON：",
                '{"status":"ok|risk","summary":"...","key_args":["..."]}',
                "",
                f"Operation: {node_name}",
                f"Template: {operation.get('template_name') or ''}",
                f"SelectedArgs: {_render_selected_args(operation.get('selected_args') or [])}",
                f"MissingFields: {', '.join(operation.get('missing_fields') or []) or 'None'}",
                f"OperationNotes: {_render_notes(operation.get('notes') or [])}",
                "GraphPaths:",
                _render_list(operation.get("graph_paths") or [], limit=8),
                "RetrievalEvidence:",
                _render_retrieval_matches(operation.get("retrieval_matches") or [], limit=6),
            ]
        ).strip()
        items.append(
            ReplayWorkloadItem(
                item_id=_item_id("long_context", index, node_name),
                profile="long_context",
                prompt=prompt,
                max_tokens=256,
                min_tokens=96,
                cache_mode="cold",
                tags=["long_context", "graph", "real_trace"],
                metadata={
                    "node_name": node_name,
                    "template_name": str(operation.get("template_name") or ""),
                    "selected_evidence_ids": [str(item) for item in operation.get("selected_evidence_ids") or []],
                    "estimated_prompt_tokens": _estimate_tokens(prompt),
                },
            )
        )
        index += 1
    return items


def _build_repair_items(
    decisions: list[dict[str, Any]],
    operation_by_name: dict[str, dict[str, Any]],
) -> list[ReplayWorkloadItem]:
    items: list[ReplayWorkloadItem] = []
    index = 0
    for decision in decisions:
        node_name = str(decision.get("node_name") or "").strip()
        arg_name = str(decision.get("arg_name") or "").strip()
        operation = operation_by_name.get(node_name, {})
        prompt = "\n".join(
            [
                "你在做 XML workflow repair 建议生成。",
                "请根据参数冲突和真实证据，输出 JSON：",
                '{"action":"keep_base|accept_plan|ignore_plan_arg|needs_review","confidence":0.0,"reason":"..."}',
                "",
                f"Node: {node_name}",
                f"Template: {decision.get('class_name') or ''}",
                f"Arg: {arg_name}",
                f"PlanValue: {decision.get('plan_value') or ''}",
                f"BaseValue: {decision.get('base_value') or ''}",
                f"FusedValue: {decision.get('fused_value') or ''}",
                f"CurrentAction: {decision.get('action') or ''}",
                f"Reason: {decision.get('reason') or ''}",
                "GraphPaths:",
                _render_list(operation.get("graph_paths") or [], limit=6),
                "Evidence:",
                _render_retrieval_matches(operation.get("retrieval_matches") or [], limit=5),
            ]
        ).strip()
        items.append(
            ReplayWorkloadItem(
                item_id=_item_id("repair", index, node_name, arg_name),
                profile="repair",
                prompt=prompt,
                max_tokens=256,
                min_tokens=80,
                cache_mode="cold",
                tags=["repair", "review", "real_trace"],
                metadata={
                    "node_name": node_name,
                    "arg_name": arg_name,
                    "template_name": str(decision.get("class_name") or ""),
                    "expected_action": str(decision.get("action") or ""),
                    "evidence_ids": [str(item) for item in decision.get("evidence_ids") or []],
                    "estimated_prompt_tokens": _estimate_tokens(prompt),
                },
            )
        )
        index += 1
    return items


def _build_xml_generation_items(llm_xml_payload: dict[str, Any]) -> list[ReplayWorkloadItem]:
    contexts = list(llm_xml_payload.get("contexts") or [])
    items: list[ReplayWorkloadItem] = []
    for index, context in enumerate(contexts):
        node_name = str(context.get("node_name") or "").strip()
        prompt = "\n".join(
            [
                "你在做汽车诊断 operation-level ScriptNode XML 生成。",
                "请基于证据包、模板契约、历史 XML 示例和 base workflow 参考，只输出 JSON：",
                '{"node_name":"...","class_name":"...","args":[],"xml":"<ScriptNode .../>","evidence_map":{},"confidence":0.0,"needs_review":false,"missing_or_conflict_reason":""}',
                "",
                f"Operation: {_json_compact(_operation_context(context))}",
                f"RequiredArgs: {', '.join(context.get('required_arg_names') or []) or 'None'}",
                f"AllowedArgs: {', '.join(context.get('allowed_arg_names') or []) or 'None'}",
                "EvidenceContext:",
                _render_llm_context_items(context.get("context_items") or [], limit=6),
                "TemplateExamples:",
                _render_template_examples(context.get("template_examples") or [], limit=3),
                f"BaseWorkflowNode: {_json_compact(context.get('base_workflow_node') or {})}",
            ]
        ).strip()
        profile = "long_context_xml_generation" if _estimate_tokens(prompt) > 8192 else "xml_generation"
        items.append(
            ReplayWorkloadItem(
                item_id=_item_id(profile, index, node_name),
                profile=profile,
                prompt=prompt,
                max_tokens=768,
                min_tokens=160,
                cache_mode="warm",
                tags=["xml_generation", "scriptnode", "real_trace"],
                metadata={
                    "node_name": node_name,
                    "template_name": str(context.get("template_name") or ""),
                    "prompt_context_ids": [str(item) for item in context.get("prompt_context_ids") or []],
                    "estimated_prompt_tokens": _estimate_tokens(prompt),
                },
            )
        )
    return items


def _build_xml_repair_items(llm_xml_payload: dict[str, Any]) -> list[ReplayWorkloadItem]:
    generations = list(llm_xml_payload.get("generations") or [])
    context_by_key = {
        (str(context.get("step_key") or ""), str(context.get("node_name") or "")): context
        for context in llm_xml_payload.get("contexts") or []
    }
    items: list[ReplayWorkloadItem] = []
    for index, generation in enumerate(generations):
        issues = list(generation.get("xml_validation_errors") or [])
        repair_attempts = list(generation.get("repair_attempts") or [])
        if not issues and not repair_attempts and generation.get("valid", False):
            continue
        node_name = str(generation.get("node_name") or "").strip()
        context = context_by_key.get((str(generation.get("step_key") or ""), node_name), {})
        prompt = "\n".join(
            [
                "你在做 operation-level ScriptNode XML repair。",
                "请只修复 validation_errors 指出的 JSON/XML 问题，返回同 schema JSON：",
                '{"node_name":"...","class_name":"...","args":[],"xml":"<ScriptNode .../>","evidence_map":{},"confidence":0.0,"needs_review":false,"missing_or_conflict_reason":""}',
                "",
                f"Operation: {_json_compact(_operation_context(context) or _operation_generation(generation))}",
                f"ValidationErrors: {_json_compact(issues)}",
                f"OriginalXml: {str(generation.get('xml') or '')[:2000]}",
                "EvidenceContext:",
                _render_llm_context_items(context.get("context_items") or [], limit=4),
            ]
        ).strip()
        items.append(
            ReplayWorkloadItem(
                item_id=_item_id("xml_repair", index, node_name),
                profile="xml_repair",
                prompt=prompt,
                max_tokens=768,
                min_tokens=160,
                cache_mode="cold",
                tags=["xml_repair", "scriptnode", "real_trace"],
                metadata={
                    "node_name": node_name,
                    "template_name": str(generation.get("template_name") or context.get("template_name") or ""),
                    "validation_issue_codes": [str(issue.get("code") or "") for issue in issues if isinstance(issue, dict)],
                    "estimated_prompt_tokens": _estimate_tokens(prompt),
                },
            )
        )
    return items


def _collect_operations(flow_trace_payload: dict[str, Any]) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for step in flow_trace_payload.get("steps") or []:
        for operation in step.get("operations") or []:
            operations.append(operation)
    return operations


def _operation_context(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "step_key": context.get("step_key") or "",
        "order": context.get("order") or 0,
        "row": context.get("row") or 0,
        "column": context.get("column") or 0,
        "node_name": context.get("node_name") or "",
        "template_name": context.get("template_name") or "",
        "class_name": context.get("class_name") or "",
        "script_type": context.get("script_type") or "NORMAL",
    }


def _operation_generation(generation: dict[str, Any]) -> dict[str, Any]:
    return {
        "step_key": generation.get("step_key") or "",
        "order": generation.get("order") or 0,
        "row": generation.get("row") or 0,
        "column": generation.get("column") or 0,
        "node_name": generation.get("node_name") or "",
        "template_name": generation.get("template_name") or "",
        "class_name": generation.get("class_name") or "",
    }


def _render_llm_context_items(items: list[dict[str, Any]], limit: int) -> str:
    if not items:
        return "None"
    lines = []
    for item in items[:limit]:
        lines.append(
            (
                f"- context_id={item.get('context_id') or ''}; "
                f"evidence_id={item.get('evidence_id') or ''}; "
                f"kind={item.get('kind') or ''}; "
                f"score={item.get('score') or 0}; "
                f"fields={_json_compact(item.get('parameter_fields') or {})}; "
                f"content={str(item.get('content') or '').strip()[:600]}"
            )
        )
    return "\n".join(lines)


def _render_template_examples(items: list[dict[str, Any]], limit: int) -> str:
    if not items:
        return "None"
    lines = []
    for item in items[:limit]:
        lines.append(
            (
                f"- template_id={item.get('template_id') or ''}; "
                f"class_name={item.get('class_name') or ''}; "
                f"args={','.join(item.get('arg_names') or [])}; "
                f"xml={str(item.get('xml') or '').strip()[:600]}"
            )
        )
    return "\n".join(lines)


def _json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _render_selected_args(selected_args: list[dict[str, Any]]) -> str:
    if not selected_args:
        return "None"
    parts = []
    for item in selected_args[:8]:
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        score = item.get("selection_score")
        parts.append(f"{name}={value} (score={score})")
    return "; ".join(parts)


def _render_notes(notes: Iterable[Any]) -> str:
    return "; ".join(str(note) for note in list(notes)[:6]) or "None"


def _render_list(items: list[Any], limit: int) -> str:
    if not items:
        return "None"
    return "\n".join(f"- {str(item)}" for item in items[:limit])


def _render_retrieval_matches(matches: list[dict[str, Any]], limit: int) -> str:
    if not matches:
        return "None"
    lines: list[str] = []
    for match in matches[:limit]:
        lines.append(
            (
                f"- evidence_id={match.get('evidence_id') or ''}; "
                f"score={match.get('score') or 0}; "
                f"matched_terms={','.join(match.get('matched_terms') or []) or 'None'}; "
                f"excerpt={str(match.get('content_excerpt') or '').strip()[:240]}"
            )
        )
    return "\n".join(lines)


def _series_metrics(prefix: str, values: list[float]) -> dict[str, Any]:
    return {
        f"{prefix}_count": len(values),
        f"{prefix}_min": round(min(values), 6),
        f"{prefix}_max": round(max(values), 6),
        f"{prefix}_mean": round(statistics.fmean(values), 6),
        f"{prefix}_p50": _percentile(values, 50),
        f"{prefix}_p95": _percentile(values, 95),
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 6)
    rank = (len(ordered) - 1) * (percentile / 100.0)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 6)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _item_id(profile: str, index: int, *parts: str) -> str:
    suffix = "-".join(_slug(part) for part in parts if part)
    return f"{profile}:{index:03d}:{suffix or 'item'}"


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip())
    return normalized.strip("_").lower() or "item"
