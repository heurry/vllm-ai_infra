"""Workload manifest helpers for repeatable pipeline runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class WorkloadManifest(BaseModel):
    """Single workload definition for the XML generation pipeline."""

    workload: str
    description: str = ""
    paths: dict[str, str] = Field(default_factory=dict)
    local_index: dict[str, Any] = Field(default_factory=dict)
    generation: dict[str, Any] = Field(default_factory=dict)
    vector_index: dict[str, Any] = Field(default_factory=dict)
    hybrid_retrieval: dict[str, Any] = Field(default_factory=dict)
    fusion: dict[str, Any] = Field(default_factory=dict)
    retrieval_eval: dict[str, Any] = Field(default_factory=dict)
    regression_expectations: dict[str, Any] = Field(default_factory=dict)
    critical_args: list[dict[str, str]] = Field(default_factory=list)
    node_golden_checks: list[dict[str, Any]] = Field(default_factory=list)


class WorkloadRegressionRunRequest(BaseModel):
    """Request to run regression evaluation from a workload manifest."""

    workload_path: str = "configs/workloads/treg_20260402.json"
    output_path: str | None = None
    use_llm_xml: bool = False


class WorkloadPipelineRunRequest(BaseModel):
    """Request to run the end-to-end pipeline from a workload manifest."""

    workload_path: str = "configs/workloads/treg_20260402.json"
    output_path: str | None = None
    enable_llm: bool = False
    skip_steps: list[str] = Field(default_factory=list)


def load_workload_manifest(path: Path) -> WorkloadManifest:
    """Load a workload manifest JSON file."""

    return WorkloadManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))


def workload_to_regression_config(manifest: WorkloadManifest, use_llm_xml: bool = False) -> dict[str, Any]:
    """Convert a workload manifest to regression evaluation config."""

    paths = manifest.paths
    generation = manifest.generation
    use_llm_plan = _should_use_llm_xml_for_fusion(generation, use_llm_xml)
    evidence_chain_report = _sibling_path(paths, "evidence_chain_report", "flow_evidence_trace", "evidence_chain_report.json")
    evidence_chain_report_md = _sibling_path(paths, "evidence_chain_report_md", "flow_evidence_trace", "evidence_chain_report.md")
    return {
        "workload": manifest.workload,
        "generation": {
            "mode": "llm_node" if use_llm_plan else "deterministic",
            "configured_mode": str(generation.get("mode") or "deterministic"),
            "active_plan": "llm_xml_plan" if use_llm_plan else "xml_plan",
            "baseline_plan": "xml_plan",
        },
        "paths": {
            "xml_plan": paths["llm_xml_plan"] if use_llm_plan else paths["xml_plan"],
            "baseline_xml_plan": paths.get("xml_plan", ""),
            "llm_xml_plan": paths.get("llm_xml_plan", ""),
            "llm_generation_trace": paths.get("llm_generation_trace", ""),
            "flow_evidence_trace": paths["flow_evidence_trace"],
            "evidence_chain_report": evidence_chain_report,
            "evidence_chain_report_md": evidence_chain_report_md,
            "diagnostic_graph": paths["diagnostic_graph"],
            "serial_xml": paths["llm_serial_xml"] if use_llm_plan else paths["serial_xml"],
            "fused_workflow": paths["fused_workflow"],
            "arg_audit_report": paths["arg_audit_report"],
            "audit_resolution_report": paths["audit_resolution_report"],
            "operation_xml_dir": paths["llm_operation_xml_dir"] if use_llm_plan else paths["operation_xml_dir"],
            "baseline_operation_xml_dir": paths.get("operation_xml_dir", ""),
        },
        "expectations": manifest.regression_expectations,
        "retrieval_eval": manifest.retrieval_eval,
        "critical_args": manifest.critical_args,
        "node_golden_checks": manifest.node_golden_checks,
    }


def workload_to_pipeline_config(manifest: WorkloadManifest, enable_llm: bool = False) -> dict[str, Any]:
    """Convert a workload manifest to command-based pipeline config."""

    paths = manifest.paths
    local_index = manifest.local_index
    generation = manifest.generation
    vector_index = manifest.vector_index
    hybrid_retrieval = manifest.hybrid_retrieval
    fusion = manifest.fusion
    retrieval_args = _hybrid_retrieval_args(hybrid_retrieval)
    evidence_chain_report = _sibling_path(paths, "evidence_chain_report", "flow_evidence_trace", "evidence_chain_report.json")
    evidence_chain_report_md = _sibling_path(paths, "evidence_chain_report_md", "flow_evidence_trace", "evidence_chain_report.md")
    llm_mode = str(generation.get("mode") or "").strip().lower()
    should_generate_llm_xml = enable_llm and llm_mode == "llm_node" and bool(paths.get("llm_xml_plan")) and bool(paths.get("llm_serial_xml"))
    use_llm_xml_for_fusion = _should_use_llm_xml_for_fusion(generation, should_generate_llm_xml)
    steps: list[dict[str, Any]] = []
    if local_index.get("enabled"):
        steps.append(
            {
                "name": "build_local_index",
                "command": _local_index_command(manifest),
                "required": True,
                "enabled": True,
                "timeout_seconds": int(local_index.get("timeout_seconds", 1800)),
            }
        )

    if vector_index.get("enabled"):
        steps.append(
            {
                "name": "build_vector_index",
                "command": [
                    "{python}",
                    "scripts/build_vector_index.py",
                    "--index-dir",
                    paths["index_dir"],
                    "--collection-name",
                    str(vector_index.get("collection_name") or hybrid_retrieval.get("vector_collection") or ""),
                    "--milvus-uri",
                    str(vector_index.get("milvus_uri") or hybrid_retrieval.get("milvus_uri") or "http://127.0.0.1:19530"),
                    "--embedding-model",
                    str(vector_index.get("embedding_model") or hybrid_retrieval.get("embedding_model") or "BAAI/bge-m3"),
                    "--metric-type",
                    str(vector_index.get("metric_type") or "COSINE"),
                    "--batch-size",
                    str(vector_index.get("batch_size", 64)),
                ]
                + (["--drop-existing"] if vector_index.get("drop_existing") else []),
                "required": True,
                "enabled": True,
                "timeout_seconds": int(vector_index.get("timeout_seconds", 1800)),
            }
        )

    steps.append(
        {
            "name": "generate_xml_plan",
            "command": [
                "{python}",
                "scripts/generate_xml_plan.py",
                "--flow-path",
                paths["flow_path"],
                "--index-dir",
                paths["index_dir"],
                "--output-path",
                paths["xml_plan"],
                "--xml-output-path",
                paths["serial_xml"],
                "--operation-xml-dir",
                paths["operation_xml_dir"],
                "--trace-output-path",
                paths["flow_evidence_trace"],
                "--evidence-chain-output-path",
                evidence_chain_report,
                "--evidence-chain-markdown-output-path",
                evidence_chain_report_md,
                "--graph-output-path",
                paths["diagnostic_graph"],
                "--top-k-per-node",
                str(generation.get("top_k_per_node", 5)),
                "--graph-max-paths-per-node",
                str(generation.get("graph_max_paths_per_node", 12)),
                "--graph-max-depth",
                str(generation.get("graph_max_depth", 2)),
                "--serial-name",
                str(generation.get("serial_name", "MAC_ALL")),
            ]
            + retrieval_args,
            "required": True,
            "enabled": True,
            "timeout_seconds": int(generation.get("timeout_seconds", 120)),
        }
    )
    if should_generate_llm_xml:
        steps.append(
            {
                "name": "generate_llm_xml",
                "command": [
                    "{python}",
                    "scripts/generate_xml_with_llm.py",
                    "--flow-path",
                    paths["flow_path"],
                    "--index-dir",
                    paths["index_dir"],
                    "--template-registry",
                    str(paths.get("template_registry") or ""),
                    "--template-contracts",
                    paths["template_contracts"],
                    "--base-workflow",
                    paths["base_workflow"],
                    "--output-path",
                    paths["llm_xml_plan"],
                    "--xml-output-path",
                    paths["llm_serial_xml"],
                    "--operation-xml-dir",
                    paths["llm_operation_xml_dir"],
                    "--trace-output-path",
                    paths["llm_generation_trace"],
                    "--raw-source-dir",
                    paths["llm_raw_source_dir"],
                    "--top-k-per-node",
                    str(generation.get("top_k_per_node", 5)),
                    "--graph-max-paths-per-node",
                    str(generation.get("graph_max_paths_per_node", 12)),
                    "--graph-max-depth",
                    str(generation.get("graph_max_depth", 2)),
                    "--serial-name",
                    str(generation.get("serial_name", "MAC_ALL")),
                ]
                + _llm_xml_args(generation)
                + retrieval_args,
                "required": bool(use_llm_xml_for_fusion),
                "enabled": True,
                "timeout_seconds": int(generation.get("llm_timeout_seconds", 1800)),
            }
        )

    fusion_plan_path = paths["llm_xml_plan"] if use_llm_xml_for_fusion else paths["xml_plan"]
    steps.extend(
        [
        {
            "name": "fuse_workflow",
            "command": [
                "{python}",
                "scripts/fuse_workflow.py",
                "--plan-path",
                fusion_plan_path,
                "--base-xml-path",
                paths["base_workflow"],
                "--output-path",
                paths["fused_workflow"],
                "--target-serial-name",
                str(fusion.get("target_serial_name", "FHC_ALL")),
                "--arg-merge-strategy",
                str(fusion.get("arg_merge_strategy", "fill_missing")),
            ],
            "required": True,
            "enabled": True,
            "timeout_seconds": int(fusion.get("timeout_seconds", 120)),
        },
        {
            "name": "audit_workflow_args",
            "command": [
                "{python}",
                "scripts/audit_workflow_args.py",
                "--plan-path",
                fusion_plan_path,
                "--base-xml-path",
                paths["base_workflow"],
                "--fused-xml-path",
                paths["fused_workflow"],
                "--output-path",
                paths["arg_audit_report"],
            ],
            "required": True,
            "enabled": True,
            "timeout_seconds": 120,
        },
        {
            "name": "resolve_audit_report",
            "command": [
                "{python}",
                "scripts/resolve_audit_report.py",
                "--audit-report-path",
                paths["arg_audit_report"],
                "--contract-registry-path",
                paths["template_contracts"],
                "--output-path",
                paths["audit_resolution_report"],
            ],
            "required": True,
            "enabled": True,
            "timeout_seconds": 120,
        },
        {
            "name": "resolve_audit_with_llm",
            "command": [
                "{python}",
                "scripts/resolve_audit_with_llm.py",
                "--resolution-report-path",
                paths["audit_resolution_report"],
                "--output-path",
                paths["llm_resolution_report"],
                "--max-items",
                str(manifest.generation.get("llm_max_items", 20)),
            ],
            "required": False,
            "enabled": enable_llm,
            "timeout_seconds": 300,
        },
        {
            "name": "regression_eval",
            "command": [
                "{python}",
                "scripts/run_workload_regression.py",
                "--workload",
                "{workload_manifest}",
                "--output-path",
                paths["regression_report"],
            ],
            "command_suffix": ["--use-llm-xml"] if use_llm_xml_for_fusion else [],
            "required": True,
            "enabled": True,
            "timeout_seconds": 120,
        },
        ]
    )

    for step in steps:
        suffix = step.pop("command_suffix", [])
        if suffix:
            step["command"].extend(suffix)

    return {"workload": manifest.workload, "steps": steps}


def _should_use_llm_xml_for_fusion(generation: dict[str, Any], llm_available: bool) -> bool:
    return bool(llm_available and generation.get("use_llm_xml_for_fusion"))


def _local_index_command(manifest: WorkloadManifest) -> list[str]:
    paths = manifest.paths
    config = manifest.local_index
    mineru_output_dir = config.get("mineru_output_dir") or paths.get("mineru_output_dir")
    if not mineru_output_dir:
        raise ValueError("local_index.enabled requires local_index.mineru_output_dir or paths.mineru_output_dir")
    command = [
        "{python}",
        "scripts/build_mineru_index.py",
        "--mineru-output-dir",
        str(mineru_output_dir),
        "--index-dir",
        paths["index_dir"],
        "--collection-name",
        str(config.get("collection_name") or manifest.workload),
        "--protocol",
        str(config.get("protocol") or "UDS"),
        "--doc-type",
        str(config.get("doc_type") or "pdf_protocol"),
        "--source",
        str(config.get("source") or "mineru"),
        "--chunking-mode",
        str(config.get("chunking_mode") or "legacy"),
    ]
    if config.get("include_graph") is False:
        command.append("--no-graph")
    return command


def _llm_xml_args(generation: dict[str, Any]) -> list[str]:
    config = {
        **{key: value for key, value in generation.items() if key.startswith("llm_") or key.startswith("vllm_")},
        **dict(generation.get("llm_xml") or {}),
    }
    args: list[str] = []
    option_map = {
        "prompt_budget_tokens": "--prompt-budget-tokens",
        "prompt_reserved_output_tokens": "--prompt-reserved-output-tokens",
        "prompt_output_dir": "--prompt-output-dir",
        "max_template_examples": "--max-template-examples",
        "repair_attempts": "--repair-attempts",
        "node_limit": "--node-limit",
        "request_profile": "--request-profile",
        "repair_profile": "--repair-profile",
        "vllm_base_url": "--vllm-base-url",
        "vllm_model": "--vllm-model",
        "vllm_api_key": "--vllm-api-key",
        "vllm_timeout_seconds": "--vllm-timeout-seconds",
        "vllm_temperature": "--vllm-temperature",
        "vllm_max_tokens": "--vllm-max-tokens",
    }
    for key, option in option_map.items():
        if config.get(key) is not None:
            args.extend([option, str(config[key])])
    node_names = config.get("node_names") or config.get("node_name") or []
    if isinstance(node_names, str):
        node_names = [node_names]
    for node_name in node_names:
        args.extend(["--node-name", str(node_name)])
    if config.get("no_incremental_trace"):
        args.append("--no-incremental-trace")
    if config.get("disable_thinking") or config.get("vllm_disable_thinking"):
        args.append("--disable-thinking")
    if config.get("enable_router"):
        args.append("--enable-router")
    short_urls = config.get("router_short_base_urls") or config.get("router_short_base_url") or []
    if isinstance(short_urls, str):
        short_urls = [short_urls]
    for url in short_urls:
        args.extend(["--router-short-base-url", str(url)])
    for key, option in [
        ("router_long_base_url", "--router-long-base-url"),
        ("router_extended_base_url", "--router-extended-base-url"),
        ("router_extreme_base_url", "--router-extreme-base-url"),
    ]:
        if config.get(key):
            args.extend([option, str(config[key])])
    return args


def _hybrid_retrieval_args(config: dict[str, Any]) -> list[str]:
    if not config.get("enabled"):
        return []
    args = ["--enable-dense"]
    if config.get("vector_config"):
        args.extend(["--vector-config", str(config["vector_config"])])
    if config.get("dense_top_k") is not None:
        args.extend(["--dense-top-k", str(config["dense_top_k"])])
    if config.get("hybrid_top_k") is not None:
        args.extend(["--hybrid-top-k", str(config["hybrid_top_k"])])
    if config.get("dense_weight") is not None:
        args.extend(["--dense-weight", str(config["dense_weight"])])
    if config.get("sparse_weight") is not None:
        args.extend(["--sparse-weight", str(config["sparse_weight"])])
    if config.get("milvus_uri"):
        args.extend(["--milvus-uri", str(config["milvus_uri"])])
    if config.get("vector_collection") or config.get("collection_name"):
        args.extend(["--vector-collection", str(config.get("vector_collection") or config.get("collection_name"))])
    if config.get("embedding_model"):
        args.extend(["--embedding-model", str(config["embedding_model"])])
    if config.get("vector_manifest_path"):
        args.extend(["--vector-manifest-path", str(config["vector_manifest_path"])])
    return args


def _sibling_path(paths: dict[str, str], key: str, sibling_key: str, filename: str) -> str:
    if paths.get(key):
        return str(paths[key])
    sibling = paths.get(sibling_key)
    if not sibling:
        return filename
    return str(Path(str(sibling)).with_name(filename))


def inject_workload_manifest_path(pipeline_config: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    """Replace workload manifest placeholder in generated command configs."""

    payload = json.loads(json.dumps(pipeline_config))
    for step in payload.get("steps", []):
        step["command"] = [
            str(manifest_path) if item == "{workload_manifest}" else item
            for item in step.get("command", [])
        ]
    return payload
