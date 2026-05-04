#!/usr/bin/env python3
"""Generate operation-level ScriptNode XML with an LLM and deterministic guardrails."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from diagnostic_platform.generation.llm_xml import (  # noqa: E402
    assemble_llm_xml_plan,
    build_node_xml_generation_messages,
    build_xml_generation_context,
    generate_node_xml_with_llm,
    validate_and_repair_node_xml,
)
from diagnostic_platform.generation.vllm_client import VllmClient, normalize_chat_messages_for_vllm  # noqa: E402
from diagnostic_platform.graph.evidence_paths import attach_graph_paths_to_evidence_response  # noqa: E402
from diagnostic_platform.graph.full_kg import KG_MANIFEST_FILE, LocalGraphRepository  # noqa: E402
from diagnostic_platform.ingestion.flow_excel import parse_flow_xlsx  # noqa: E402
from diagnostic_platform.indexing.local_builder import EVIDENCE_UNITS_FILE  # noqa: E402
from diagnostic_platform.renderers.xml_workflow import render_flow_serial_node, render_script_node  # noqa: E402
from diagnostic_platform.retrieval.prompt_budget import PromptBudgetConfig  # noqa: E402
from diagnostic_platform.schemas import (  # noqa: E402
    BuildStepEvidenceRequest,
    EvidenceUnit,
    LlmNodeXmlGeneration,
    LlmXmlGenerationTrace,
    VllmModelConfig,
    XmlGenerationContext,
    XmlPlanValidationRequest,
    XmlValidationRequest,
)
from diagnostic_platform.serving.router import build_default_router, estimate_prompt_tokens_from_messages  # noqa: E402
from diagnostic_platform.tracing.evidence_bundle import build_step_evidence_bundles  # noqa: E402
from diagnostic_platform.validation.xml_plan_validator import validate_xml_plan  # noqa: E402
from diagnostic_platform.validation.xml_validator import validate_xml  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flow-path", required=True)
    parser.add_argument("--index-dir", required=True)
    parser.add_argument("--template-registry", default="")
    parser.add_argument("--template-contracts", default="")
    parser.add_argument("--base-workflow", default="")
    parser.add_argument("--output-path", default="data/processed/llm_xml/llm_xml_plan.json")
    parser.add_argument("--xml-output-path", default="data/processed/llm_xml/serial_node.xml")
    parser.add_argument("--operation-xml-dir", default="data/processed/llm_xml/operations")
    parser.add_argument("--trace-output-path", default="data/processed/llm_xml/llm_generation_trace.json")
    parser.add_argument("--raw-source-dir", default="data/processed/llm_xml/raw_sources")
    parser.add_argument("--prompt-output-dir", default="")
    parser.add_argument("--serial-name", default="MAC_ALL")
    parser.add_argument("--top-k-per-node", type=int, default=5)
    parser.add_argument("--graph-max-paths-per-node", type=int, default=12)
    parser.add_argument("--graph-max-depth", type=int, default=2)
    parser.add_argument("--no-graph-paths", action="store_true")
    parser.add_argument("--no-kg-query", action="store_true")
    parser.add_argument("--enable-dense", action="store_true")
    parser.add_argument("--vector-config", default="")
    parser.add_argument("--dense-top-k", type=int, default=None)
    parser.add_argument("--hybrid-top-k", type=int, default=None)
    parser.add_argument("--dense-weight", type=float, default=None)
    parser.add_argument("--sparse-weight", type=float, default=None)
    parser.add_argument("--milvus-uri", default="")
    parser.add_argument("--vector-collection", default="")
    parser.add_argument("--embedding-model", default="")
    parser.add_argument("--vector-manifest-path", default="")
    parser.add_argument("--prompt-budget-tokens", type=int, default=8192)
    parser.add_argument("--prompt-reserved-output-tokens", type=int, default=1536)
    parser.add_argument("--max-template-examples", type=int, default=3)
    parser.add_argument("--repair-attempts", type=int, default=2)
    parser.add_argument("--node-limit", type=int, default=0)
    parser.add_argument("--node-name", action="append", default=[])
    parser.add_argument("--no-incremental-trace", action="store_true")
    parser.add_argument("--request-profile", default="xml_generation")
    parser.add_argument("--repair-profile", default="xml_repair")
    parser.add_argument("--vllm-base-url", default="http://127.0.0.1:8008/v1")
    parser.add_argument("--vllm-model", default="qwen-xml-generator")
    parser.add_argument("--vllm-api-key", default="EMPTY")
    parser.add_argument("--vllm-timeout-seconds", type=int, default=180)
    parser.add_argument("--vllm-temperature", type=float, default=0.0)
    parser.add_argument("--vllm-max-tokens", type=int, default=2048)
    parser.add_argument("--disable-thinking", action="store_true")
    parser.add_argument("--enable-router", action="store_true")
    parser.add_argument("--router-short-base-url", action="append", default=[])
    parser.add_argument("--router-long-base-url", default="")
    parser.add_argument("--router-extended-base-url", default="")
    parser.add_argument("--router-extreme-base-url", default="")
    args = parser.parse_args()

    index_dir = Path(args.index_dir)
    evidence_units = _load_evidence_units(index_dir / EVIDENCE_UNITS_FILE)
    flow_plan = parse_flow_xlsx(Path(args.flow_path))
    graph_repository = _load_graph_repository(index_dir, enabled=not args.no_kg_query)
    vector_options = _vector_options(args)
    evidence_response = build_step_evidence_bundles(
        BuildStepEvidenceRequest(
            flow_plan=flow_plan,
            evidence_units=evidence_units,
            top_k_per_node=args.top_k_per_node,
            index_dir=args.index_dir,
            **vector_options,
        ),
        graph_repository=graph_repository,
    )
    if not args.no_graph_paths:
        evidence_response, _graph = attach_graph_paths_to_evidence_response(
            flow_plan=flow_plan,
            evidence_response=evidence_response,
            evidence_units=evidence_units,
            max_paths_per_node=args.graph_max_paths_per_node,
            max_depth=args.graph_max_depth,
        )

    vllm_config = VllmModelConfig(
        base_url=args.vllm_base_url,
        model=args.vllm_model,
        api_key=args.vllm_api_key,
        timeout_seconds=args.vllm_timeout_seconds,
        temperature=args.vllm_temperature,
        max_tokens=args.vllm_max_tokens,
    )
    router = None
    if args.enable_router:
        router = build_default_router(
            short_base_urls=args.router_short_base_url or [args.vllm_base_url],
            long_base_url=args.router_long_base_url or args.vllm_base_url,
            extended_base_url=args.router_extended_base_url or None,
            extreme_base_url=args.router_extreme_base_url or None,
            model=args.vllm_model,
        )
    client = VllmClient(vllm_config, router=router, profile=_request_profile(args.request_profile))
    extra_body = _llm_extra_body(args)
    prompt_budget = PromptBudgetConfig(
        max_prompt_tokens=args.prompt_budget_tokens,
        reserved_output_tokens=args.prompt_reserved_output_tokens,
    )

    contexts: list[XmlGenerationContext] = []
    generations: list[LlmNodeXmlGeneration] = []
    node_positions = _select_node_positions(
        _node_positions(evidence_response),
        node_names=set(args.node_name or []),
        node_limit=args.node_limit,
    )
    if not node_positions:
        raise ValueError("No flow nodes selected for LLM XML generation")
    total_nodes = len(node_positions)
    for index, (order, node_bundle) in enumerate(node_positions, start=1):
        print(
            json.dumps(
                {
                    "event": "llm_node_start",
                    "index": index,
                    "total": total_nodes,
                    "node_name": node_bundle.node_name,
                    "template_name": node_bundle.template_name,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )
        context = build_xml_generation_context(
            node_bundle,
            template_registry=args.template_registry or None,
            base_workflow=args.base_workflow or None,
            contract_registry=args.template_contracts or None,
            order=order,
            script_type=_boundary_script_type(index, total_nodes),
            prompt_budget=prompt_budget,
            raw_source_dir=args.raw_source_dir,
            max_template_examples=args.max_template_examples,
        )
        prompt_path = _write_prompt_artifact(
            Path(args.prompt_output_dir) if args.prompt_output_dir else Path(args.raw_source_dir) / "prompts",
            context=context,
            index=index,
            total_nodes=total_nodes,
            messages=build_node_xml_generation_messages(context),
            vllm_config=vllm_config,
            extra_body=extra_body,
            request_profile=_request_profile(args.request_profile),
        )
        print(
            json.dumps(
                {
                    "event": "llm_prompt_saved",
                    "index": index,
                    "total": total_nodes,
                    "node_name": context.node_name,
                    "path": str(prompt_path),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )
        generated = generate_node_xml_with_llm(
            context,
            vllm_config,
            client=client,
            request_profile=_request_profile(args.request_profile),
            extra_body=extra_body,
        )
        checked = validate_and_repair_node_xml(
            generated,
            context,
            vllm_config=vllm_config,
            client=client,
            max_repair_attempts=args.repair_attempts,
            request_profile=_request_profile(args.repair_profile),
            extra_body=extra_body,
        )
        contexts.append(context)
        generations.append(checked)
        print(
            json.dumps(
                {
                    "event": "llm_node_done",
                    "index": index,
                    "total": total_nodes,
                    "node_name": checked.node_name,
                    "valid": checked.valid,
                    "needs_review": checked.needs_review,
                    "repair_attempts": len(checked.repair_attempts),
                    "generation_latency_seconds": checked.generation_latency_seconds,
                    "repair_latency_seconds": checked.repair_latency_seconds,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )
        if not args.no_incremental_trace:
            _write_trace_artifact(
                Path(args.trace_output_path),
                source_flow_path=flow_plan.source_path,
                model=args.vllm_model,
                generations=generations,
                contexts=contexts,
                total_nodes=total_nodes,
                complete=False,
            )

    plan = assemble_llm_xml_plan(
        generations,
        serial_name=args.serial_name,
        source_flow_path=flow_plan.source_path,
    )
    plan_validation = validate_xml_plan(XmlPlanValidationRequest(plan=plan, min_script_nodes=1))
    serial_xml = render_flow_serial_node(plan)
    serial_validation = validate_xml(XmlValidationRequest(xml=serial_xml, min_script_nodes=1))
    operation_xml_files = _write_operation_xml_files(plan.nodes, Path(args.operation_xml_dir))

    trace_output_path = Path(args.trace_output_path)
    _write_trace_artifact(
        trace_output_path,
        source_flow_path=flow_plan.source_path,
        model=args.vllm_model,
        generations=generations,
        contexts=contexts,
        total_nodes=total_nodes,
        complete=True,
    )

    output_path = Path(args.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "relation_semantics": {
                    "serial": "flow.xlsx columns are executed from left to right",
                    "parallel": "non-empty flow cells in the same column are parallel operations ordered by row",
                    "operation_xml": "each operation is generated as one LLM ScriptNode and assembled by the system",
                    "llm_scope": "operation-level ScriptNode only; fused workflow remains system controlled",
                },
                "flow_plan": flow_plan.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json"),
                "plan_validation": plan_validation.model_dump(mode="json"),
                "serial_xml_validation": serial_validation.model_dump(mode="json"),
                "generation_summary": _trace_summary(generations),
                "llm_generation_trace_path": str(trace_output_path),
                "raw_source_dir": args.raw_source_dir,
                "prompt_output_dir": args.prompt_output_dir or str(Path(args.raw_source_dir) / "prompts"),
                "operation_xml_files": operation_xml_files,
                "hybrid_retrieval": {
                    "enabled": bool(vector_options["enable_dense"]),
                    "vector_collection": vector_options["vector_collection"],
                    "embedding_model": vector_options["embedding_model"],
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    xml_output_path = Path(args.xml_output_path)
    xml_output_path.parent.mkdir(parents=True, exist_ok=True)
    xml_output_path.write_text(serial_xml, encoding="utf-8")

    summary = {
        "flow_steps": len(flow_plan.steps),
        "script_nodes": len(plan.serial.scripts),
        "valid_generations": sum(1 for generation in generations if generation.valid),
        "needs_review": sum(1 for generation in generations if generation.needs_review),
        "plan_valid": plan_validation.valid,
        "serial_xml_valid": serial_validation.valid,
        "plan_path": str(output_path),
        "xml_path": str(xml_output_path),
        "trace_path": str(trace_output_path),
        "raw_source_dir": args.raw_source_dir,
        "operation_xml_count": len(operation_xml_files),
        "kg_query": graph_repository is not None,
        "dense_retrieval": bool(vector_options["enable_dense"]),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if plan_validation.valid and serial_validation.valid and all(item.valid for item in generations) else 2


def _node_positions(evidence_response):
    positions = []
    for step in evidence_response.bundles:
        for node_bundle in step.node_bundles:
            positions.append((step.order, node_bundle))
    return positions


def _select_node_positions(positions, *, node_names: set[str], node_limit: int):
    selected = []
    for order, node_bundle in positions:
        if node_names and node_bundle.node_name not in node_names and node_bundle.template_name not in node_names:
            continue
        selected.append((order, node_bundle))
    if node_limit and node_limit > 0:
        selected = selected[:node_limit]
    return selected


def _write_trace_artifact(
    path: Path,
    *,
    source_flow_path: str,
    model: str,
    generations: list[LlmNodeXmlGeneration],
    contexts: list[XmlGenerationContext],
    total_nodes: int,
    complete: bool,
) -> None:
    trace = LlmXmlGenerationTrace(
        source_flow_path=source_flow_path,
        model=model,
        generations=generations,
        contexts=contexts,
        summary={
            **_trace_summary(generations),
            "complete": complete,
            "completed_nodes": len(generations),
            "total_nodes": total_nodes,
            "hybrid_retrieval": _retrieval_summary(contexts),
        },
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(trace.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_prompt_artifact(
    output_dir: Path,
    *,
    context: XmlGenerationContext,
    index: int,
    total_nodes: int,
    messages,
    vllm_config: VllmModelConfig,
    extra_body: dict | None,
    request_profile: str,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    wire_messages = normalize_chat_messages_for_vllm(messages)
    payload = {
        "node": {
            "index": index,
            "total": total_nodes,
            "step_key": context.step_key,
            "order": context.order,
            "row": context.row,
            "column": context.column,
            "node_name": context.node_name,
            "template_name": context.template_name,
            "class_name": context.class_name,
        },
        "request_profile": request_profile,
        "model_config": {
            "base_url": vllm_config.base_url,
            "model": vllm_config.model,
            "temperature": vllm_config.temperature,
            "max_tokens": vllm_config.max_tokens,
            "timeout_seconds": vllm_config.timeout_seconds,
        },
        "extra_body": extra_body or {},
        "estimated_tokens": {
            "canonical_prompt": estimate_prompt_tokens_from_messages(messages),
            "wire_prompt": estimate_prompt_tokens_from_messages(wire_messages),
        },
        "canonical_messages": [message.model_dump(mode="json") for message in messages],
        "vllm_wire_messages": [message.model_dump(mode="json") for message in wire_messages],
    }
    filename = f"{index:03d}_{_slug(context.node_name)}_{_slug(context.template_name)}_prompt.json"
    path = output_dir / filename
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _load_evidence_units(path: Path) -> list[EvidenceUnit]:
    if not path.exists():
        raise FileNotFoundError(f"Evidence index does not exist: {path}")
    return [
        EvidenceUnit.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _load_graph_repository(index_dir: Path, enabled: bool) -> LocalGraphRepository | None:
    if not enabled:
        return None
    manifest_path = index_dir / KG_MANIFEST_FILE
    if not manifest_path.exists():
        return None
    return LocalGraphRepository.from_index_dir(index_dir)


def _vector_options(args) -> dict:
    config = _load_optional_json(Path(args.vector_config)) if args.vector_config else {}
    return {
        "enable_dense": bool(args.enable_dense or config.get("enable_dense") or config.get("enabled")),
        "dense_top_k": int(args.dense_top_k if args.dense_top_k is not None else config.get("dense_top_k", 8)),
        "hybrid_top_k": int(args.hybrid_top_k if args.hybrid_top_k is not None else config.get("hybrid_top_k", 8)),
        "dense_weight": float(args.dense_weight if args.dense_weight is not None else config.get("dense_weight", 0.6)),
        "sparse_weight": float(args.sparse_weight if args.sparse_weight is not None else config.get("sparse_weight", 0.4)),
        "milvus_uri": args.milvus_uri or str(config.get("milvus_uri") or "http://127.0.0.1:19530"),
        "vector_collection": args.vector_collection
        or str(config.get("vector_collection") or config.get("collection_name") or ""),
        "embedding_model": args.embedding_model or str(config.get("embedding_model") or "BAAI/bge-m3"),
        "vector_manifest_path": args.vector_manifest_path or config.get("vector_manifest_path"),
    }


def _load_optional_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Vector config does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Vector config must be a JSON object: {path}")
    return payload


def _write_operation_xml_files(nodes, output_dir: Path) -> list[dict[str, str | int | bool]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale_file in output_dir.glob("*.xml"):
        stale_file.unlink()
    files = []
    for node in sorted(nodes, key=lambda item: (item.order, item.row, item.column, item.node_name)):
        filename = f"{node.step_key}_row_{node.row:03d}_col_{node.column:03d}_{_slug(node.node_name)}.xml"
        path = output_dir / filename
        path.write_text(render_script_node(node.script), encoding="utf-8")
        files.append(
            {
                "step_key": node.step_key,
                "order": node.order,
                "row": node.row,
                "column": node.column,
                "node_name": node.node_name,
                "template_name": node.template_name,
                "needs_review": "needs_review" in node.missing_fields,
                "path": str(path),
            }
        )
    return files


def _trace_summary(generations: list[LlmNodeXmlGeneration]) -> dict:
    corrections = [correction for generation in generations for correction in generation.guardrail_corrections]
    generation_latencies = [generation.generation_latency_seconds for generation in generations if generation.generation_latency_seconds]
    repair_latencies = [generation.repair_latency_seconds for generation in generations if generation.repair_latency_seconds]
    return {
        "generations": len(generations),
        "valid": sum(1 for item in generations if item.valid),
        "valid_generations": sum(1 for item in generations if item.valid),
        "invalid": sum(1 for item in generations if not item.valid),
        "needs_review": sum(1 for item in generations if item.needs_review),
        "repair_attempts": sum(len(item.repair_attempts) for item in generations),
        "validation_issue_counts": _issue_counts(generations),
        "issue_counts": _issue_counts(generations),
        "guardrail_corrections": len(corrections),
        "guardrail_corrections_by_arg": _correction_counts(corrections, "arg_name"),
        "guardrail_corrections_by_source": _correction_counts(corrections, "correction_source"),
        "semantic_default_usage_count": sum(
            1 for correction in corrections if correction.correction_source == "semantic_default"
        ),
        "prompt_estimated_tokens_total": sum(item.prompt_estimated_tokens for item in generations),
        "output_estimated_tokens_total": sum(item.output_estimated_tokens for item in generations),
        "average_generation_latency_seconds": _average(generation_latencies),
        "average_repair_latency_seconds": _average(repair_latencies),
    }


def _retrieval_summary(contexts: list[XmlGenerationContext]) -> dict:
    nodes_with_dense = 0
    nodes_with_hybrid = 0
    dense_results = 0
    hybrid_results = 0
    for context in contexts:
        trace = context.node_bundle.retrieval_trace or {}
        dense = trace.get("dense") or {}
        hybrid = trace.get("hybrid") or {}
        dense_count = len(dense.get("results") or [])
        hybrid_count = len(hybrid.get("results") or [])
        if dense_count:
            nodes_with_dense += 1
            dense_results += dense_count
        if hybrid_count:
            nodes_with_hybrid += 1
            hybrid_results += hybrid_count
    return {
        "nodes_with_dense_results": nodes_with_dense,
        "nodes_with_hybrid_results": nodes_with_hybrid,
        "dense_results": dense_results,
        "hybrid_results": hybrid_results,
    }


def _issue_counts(generations: list[LlmNodeXmlGeneration]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for generation in generations:
        for issue in generation.xml_validation_errors:
            counts[issue.code] = counts.get(issue.code, 0) + 1
    return dict(sorted(counts.items()))


def _correction_counts(corrections, field_name: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for correction in corrections:
        key = str(getattr(correction, field_name) or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _average(values: list[float]) -> float:
    return round(sum(values) / len(values), 6) if values else 0.0


def _boundary_script_type(index: int, total: int):
    if total <= 1:
        return "NORMAL"
    if index == 1:
        return "START"
    if index == total:
        return "END"
    return "NORMAL"


def _request_profile(value: str):
    allowed = {
        "short_audit",
        "rerank",
        "long_context",
        "repair",
        "xml_generation",
        "xml_repair",
        "long_context_xml_generation",
        "default",
    }
    return value if value in allowed else "default"


def _llm_extra_body(args) -> dict | None:
    if not args.disable_thinking:
        return None
    return {"chat_template_kwargs": {"enable_thinking": False}}


def _slug(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z_.-]+", "_", value.strip())
    return value.strip("._-") or "operation"


if __name__ == "__main__":
    raise SystemExit(main())
