#!/usr/bin/env python3
"""Generate full ClassName TaskNode XML for flow nodes using template-family prompts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from diagnostic_platform.generation.class_xml_prompt import build_class_xml_generation_messages  # noqa: E402
from diagnostic_platform.generation.vllm_client import VllmClient, extract_json_object, normalize_chat_messages_for_vllm  # noqa: E402
from diagnostic_platform.graph.full_kg import KG_MANIFEST_FILE, LocalGraphRepository  # noqa: E402
from diagnostic_platform.ingestion.flow_excel import parse_flow_xlsx  # noqa: E402
from diagnostic_platform.indexing.local_builder import EVIDENCE_UNITS_FILE  # noqa: E402
from diagnostic_platform.schemas import BuildStepEvidenceRequest, EvidenceUnit, VllmModelConfig  # noqa: E402
from diagnostic_platform.template_family.resolver import load_template_family_registry, resolve_template_family  # noqa: E402
from diagnostic_platform.tracing.evidence_chain import include_parent_context  # noqa: E402
from diagnostic_platform.tracing.evidence_bundle import build_step_evidence_bundles  # noqa: E402
from diagnostic_platform.validation.tasknode_validator import validate_tasknode_xml  # noqa: E402
from diagnostic_platform.validation.template_family_validator import validate_tasknode_against_family  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flow-path", required=True)
    parser.add_argument("--index-dir", required=True)
    parser.add_argument("--template-family-registry", default="data/processed/template_family_registry/template_families.json")
    parser.add_argument("--output-dir", default="data/processed/class_xml/treg_20260402")
    parser.add_argument("--node-name", action="append", default=[])
    parser.add_argument("--node-limit", type=int, default=0)
    parser.add_argument("--top-k-per-node", type=int, default=5)
    parser.add_argument("--no-kg-query", action="store_true")
    parser.add_argument("--enable-dense", action="store_true")
    parser.add_argument("--dense-top-k", type=int, default=8)
    parser.add_argument("--hybrid-top-k", type=int, default=8)
    parser.add_argument("--dense-weight", type=float, default=0.6)
    parser.add_argument("--sparse-weight", type=float, default=0.4)
    parser.add_argument("--milvus-uri", default="http://127.0.0.1:19530")
    parser.add_argument("--vector-collection", default="")
    parser.add_argument("--embedding-model", default="BAAI/bge-m3")
    parser.add_argument("--call-llm", action="store_true")
    parser.add_argument("--vllm-base-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--vllm-model", default="")
    parser.add_argument("--vllm-api-key", default="EMPTY")
    parser.add_argument("--vllm-timeout-seconds", type=int, default=300)
    parser.add_argument("--vllm-temperature", type=float, default=0.0)
    parser.add_argument("--vllm-max-tokens", type=int, default=8192)
    parser.add_argument("--disable-thinking", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    prompt_dir = output_dir / "prompts"
    xml_dir = output_dir / "templates"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    xml_dir.mkdir(parents=True, exist_ok=True)

    evidence_units = _load_evidence_units(Path(args.index_dir) / EVIDENCE_UNITS_FILE)
    evidence_by_id = {evidence.evidence_id: evidence for evidence in evidence_units}
    flow_plan = parse_flow_xlsx(Path(args.flow_path))
    graph_repository = _load_graph_repository(Path(args.index_dir), enabled=not args.no_kg_query)
    evidence_response = build_step_evidence_bundles(
        BuildStepEvidenceRequest(
            flow_plan=flow_plan,
            evidence_units=evidence_units,
            top_k_per_node=args.top_k_per_node,
            index_dir=args.index_dir,
            enable_dense=args.enable_dense,
            dense_top_k=args.dense_top_k,
            hybrid_top_k=args.hybrid_top_k,
            dense_weight=args.dense_weight,
            sparse_weight=args.sparse_weight,
            milvus_uri=args.milvus_uri,
            vector_collection=args.vector_collection,
            embedding_model=args.embedding_model,
        ),
        graph_repository=graph_repository,
    )
    registry = load_template_family_registry(args.template_family_registry)
    selected = _select_node_bundles(evidence_response, set(args.node_name or []), args.node_limit)
    if not selected:
        raise ValueError("No flow nodes selected for ClassName XML generation")

    client = None
    llm_config = VllmModelConfig(
        base_url=args.vllm_base_url,
        model=args.vllm_model or "class-xml-generator",
        api_key=args.vllm_api_key,
        timeout_seconds=args.vllm_timeout_seconds,
        temperature=args.vllm_temperature,
        max_tokens=args.vllm_max_tokens,
    )
    if args.call_llm:
        client = VllmClient(llm_config, profile="xml_generation")

    results = []
    for index, node_bundle in enumerate(selected, start=1):
        node_bundle = node_bundle.model_copy(
            update={"candidates": include_parent_context(node_bundle.candidates, evidence_by_id)}
        )
        resolution = resolve_template_family(node_bundle=node_bundle, registry=registry)
        messages = build_class_xml_generation_messages(node_bundle=node_bundle, family_resolution=resolution)
        prompt_path = _write_prompt(prompt_dir, index, node_bundle, messages, llm_config, args)
        raw_response = ""
        payload = None
        class_xml = ""
        validation = {"valid": False, "issues": [{"code": "LLM_NOT_CALLED", "message": "Prompt was saved only."}]}
        family_validation = validation
        if client is not None:
            raw_response = client.chat(messages, extra_body=_extra_body(args), profile="xml_generation")
            payload = extract_json_object(raw_response)
            class_xml = str(payload.get("class_xml") or "")
            if class_xml.strip():
                xml_path = xml_dir / f"{_slug(payload.get('class_name') or node_bundle.template_name or node_bundle.node_name)}.xml"
                xml_path.write_text(class_xml, encoding="utf-8")
                validation = validate_tasknode_xml(class_xml).model_dump(mode="json")
                if resolution.family is not None:
                    family_validation = validate_tasknode_against_family(class_xml, resolution.family).model_dump(mode="json")
                else:
                    family_validation = validation
        results.append(
            {
                "node_name": node_bundle.node_name,
                "template_name": node_bundle.template_name,
                "template_family_resolution": resolution.model_dump(mode="json"),
                "prompt_path": str(prompt_path),
                "llm_called": bool(client),
                "raw_response": raw_response,
                "payload": payload,
                "tasknode_validation": validation,
                "template_family_validation": family_validation,
            }
        )

    report = {
        "source_flow_path": flow_plan.source_path,
        "template_family_registry": args.template_family_registry,
        "nodes": results,
        "summary": {
            "nodes": len(results),
            "llm_called": bool(client),
            "resolved": sum(1 for item in results if item["template_family_resolution"]["status"] == "found"),
            "tasknode_valid": sum(1 for item in results if item["tasknode_validation"].get("valid")),
            "family_valid": sum(1 for item in results if item["template_family_validation"].get("valid")),
        },
    }
    report_path = output_dir / "class_xml_generation_trace.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "nodes": len(results),
                "resolved": report["summary"]["resolved"],
                "llm_called": bool(client),
                "tasknode_valid": report["summary"]["tasknode_valid"],
                "family_valid": report["summary"]["family_valid"],
                "report_path": str(report_path),
                "prompt_dir": str(prompt_dir),
                "xml_dir": str(xml_dir),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not client or report["summary"]["tasknode_valid"] == len(results) else 2


def _select_node_bundles(evidence_response, node_names: set[str], node_limit: int):
    selected = []
    for step in evidence_response.bundles:
        for node_bundle in step.node_bundles:
            if node_names and node_bundle.node_name not in node_names and node_bundle.template_name not in node_names:
                continue
            selected.append(node_bundle)
    if node_limit > 0:
        selected = selected[:node_limit]
    return selected


def _write_prompt(output_dir: Path, index: int, node_bundle, messages, config: VllmModelConfig, args) -> Path:
    wire_messages = normalize_chat_messages_for_vllm(messages)
    payload = {
        "node": {
            "index": index,
            "node_name": node_bundle.node_name,
            "template_name": node_bundle.template_name,
        },
        "model_config": config.model_dump(mode="json"),
        "extra_body": _extra_body(args),
        "canonical_messages": [message.model_dump(mode="json") for message in messages],
        "vllm_wire_messages": [message.model_dump(mode="json") for message in wire_messages],
    }
    path = output_dir / f"{index:03d}_{_slug(node_bundle.node_name)}_{_slug(node_bundle.template_name)}_class_xml_prompt.json"
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
    if not (index_dir / KG_MANIFEST_FILE).exists():
        return None
    return LocalGraphRepository.from_index_dir(index_dir)


def _extra_body(args) -> dict | None:
    if not args.disable_thinking:
        return None
    return {"chat_template_kwargs": {"enable_thinking": False}}


def _slug(value: object) -> str:
    text = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value or "").strip())
    return text.strip("._-") or "node"


if __name__ == "__main__":
    raise SystemExit(main())
