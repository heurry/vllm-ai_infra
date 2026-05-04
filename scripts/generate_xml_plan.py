#!/usr/bin/env python3
"""Generate XML intermediate DSL from a flow.xlsx and local evidence index."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from diagnostic_platform.generation.xml_plan import generate_xml_plan  # noqa: E402
from diagnostic_platform.graph.evidence_paths import attach_graph_paths_to_evidence_response  # noqa: E402
from diagnostic_platform.graph.full_kg import KG_MANIFEST_FILE, LocalGraphRepository  # noqa: E402
from diagnostic_platform.ingestion.flow_excel import parse_flow_xlsx  # noqa: E402
from diagnostic_platform.indexing.local_builder import EVIDENCE_UNITS_FILE  # noqa: E402
from diagnostic_platform.renderers.xml_workflow import render_flow_serial_node, render_script_node  # noqa: E402
from diagnostic_platform.schemas import (  # noqa: E402
    BuildStepEvidenceRequest,
    BuildStepEvidenceResponse,
    EvidenceUnit,
    FlowStepPlan,
    XmlArg,
    XmlGenerationPlan,
    XmlPlanGenerateRequest,
    XmlPlanValidationRequest,
)
from diagnostic_platform.tracing.evidence_bundle import build_step_evidence_bundles  # noqa: E402
from diagnostic_platform.tracing.evidence_chain import (  # noqa: E402
    build_evidence_chain_report,
    render_evidence_chain_markdown,
)
from diagnostic_platform.validation.xml_plan_validator import validate_xml_plan  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flow-path", default="pdf-loop/20260402_new_v2/0402/0402/flow.xlsx")
    parser.add_argument("--index-dir", default="data/index/treg_20260402")
    parser.add_argument("--output-path", default="data/processed/xml_plan/treg_20260402/xml_plan.json")
    parser.add_argument("--xml-output-path", default="data/processed/xml_plan/treg_20260402/serial_node.xml")
    parser.add_argument("--operation-xml-dir", default="data/processed/xml_plan/treg_20260402/operations")
    parser.add_argument(
        "--trace-output-path",
        default="data/processed/xml_plan/treg_20260402/flow_evidence_trace.json",
    )
    parser.add_argument(
        "--graph-output-path",
        default="data/processed/xml_plan/treg_20260402/diagnostic_graph.json",
    )
    parser.add_argument(
        "--trace-markdown-output-path",
        default="data/processed/xml_plan/treg_20260402/flow_evidence_trace.md",
    )
    parser.add_argument(
        "--evidence-chain-output-path",
        default="data/processed/xml_plan/treg_20260402/evidence_chain_report.json",
    )
    parser.add_argument(
        "--evidence-chain-markdown-output-path",
        default="data/processed/xml_plan/treg_20260402/evidence_chain_report.md",
    )
    parser.add_argument(
        "--raw-source-dir",
        default="data/processed/xml_plan/treg_20260402/raw_sources",
    )
    parser.add_argument("--trace-content-chars", type=int, default=300)
    parser.add_argument("--evidence-chain-content-chars", type=int, default=6000)
    parser.add_argument("--top-k-per-node", type=int, default=5)
    parser.add_argument("--graph-max-paths-per-node", type=int, default=12)
    parser.add_argument("--graph-max-depth", type=int, default=2)
    parser.add_argument("--no-graph-paths", action="store_true")
    parser.add_argument("--no-kg-query", action="store_true")
    parser.add_argument("--no-trace-markdown", action="store_true")
    parser.add_argument("--no-evidence-chain-report", action="store_true")
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
    parser.add_argument("--serial-name", default="MAC_ALL")
    parser.add_argument("--default-arg", action="append", default=[], help="Default Arg in Name=Value format.")
    parser.add_argument("--required-arg", action="append", default=[], help="Required Arg name for plan validation.")
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
    graph_output_path = Path(args.graph_output_path)
    graph_path_count = 0
    if not args.no_graph_paths:
        evidence_response, graph = attach_graph_paths_to_evidence_response(
            flow_plan=flow_plan,
            evidence_response=evidence_response,
            evidence_units=evidence_units,
            max_paths_per_node=args.graph_max_paths_per_node,
            max_depth=args.graph_max_depth,
        )
        graph_output_path.parent.mkdir(parents=True, exist_ok=True)
        graph_output_path.write_text(
            json.dumps(graph.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        graph_path_count = sum(
            len(node_bundle.graph_paths)
            for bundle in evidence_response.bundles
            for node_bundle in bundle.node_bundles
        )
    xml_plan = generate_xml_plan(
        XmlPlanGenerateRequest(
            evidence_response=evidence_response,
            serial_name=args.serial_name,
            default_args=_parse_default_args(args.default_arg),
        )
    )
    validation = validate_xml_plan(
        XmlPlanValidationRequest(
            plan=xml_plan,
            required_arg_names=args.required_arg,
            min_script_nodes=1,
        )
    )

    operation_xml_files = _write_operation_xml_files(xml_plan, Path(args.operation_xml_dir))
    trace_output_path = Path(args.trace_output_path)
    trace_output_path.parent.mkdir(parents=True, exist_ok=True)
    trace_payload = _build_trace_payload(
        flow_plan=flow_plan,
        evidence_response=evidence_response,
        xml_plan=xml_plan,
        operation_xml_files=operation_xml_files,
        content_chars=args.trace_content_chars,
        raw_source_dir=Path(args.raw_source_dir),
        kg_enabled=graph_repository is not None,
        kg_manifest_path=str(index_dir / KG_MANIFEST_FILE) if graph_repository is not None else "",
        graph_repository=graph_repository,
    )
    trace_output_path.write_text(
        json.dumps(trace_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    trace_markdown_path = Path(args.trace_markdown_output_path)
    if not args.no_trace_markdown:
        trace_markdown_path.parent.mkdir(parents=True, exist_ok=True)
        trace_markdown_path.write_text(_render_trace_markdown(trace_payload), encoding="utf-8")
    evidence_chain_path = Path(args.evidence_chain_output_path)
    evidence_chain_markdown_path = Path(args.evidence_chain_markdown_output_path)
    if not args.no_evidence_chain_report:
        evidence_chain_report = build_evidence_chain_report(
            flow_plan=flow_plan,
            evidence_response=evidence_response,
            content_chars=args.evidence_chain_content_chars,
        )
        evidence_chain_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_chain_path.write_text(
            json.dumps(evidence_chain_report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        evidence_chain_markdown_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_chain_markdown_path.write_text(
            render_evidence_chain_markdown(evidence_chain_report),
            encoding="utf-8",
        )

    output_path = Path(args.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "relation_semantics": {
                    "serial": "flow.xlsx columns are executed from left to right",
                    "parallel": "non-empty flow cells in the same column are parallel operations ordered by row",
                    "operation_xml": "each operation is also rendered as a standalone ScriptNode XML file",
                },
                "flow_plan": flow_plan.model_dump(mode="json"),
                "plan": xml_plan.model_dump(mode="json"),
                "validation": validation.model_dump(mode="json"),
                "evidence_trace_path": str(trace_output_path),
                "evidence_trace_markdown_path": "" if args.no_trace_markdown else str(trace_markdown_path),
                "evidence_chain_report_path": "" if args.no_evidence_chain_report else str(evidence_chain_path),
                "evidence_chain_report_markdown_path": ""
                if args.no_evidence_chain_report
                else str(evidence_chain_markdown_path),
                "raw_source_dir": str(Path(args.raw_source_dir)),
                "kg_manifest_path": str(index_dir / KG_MANIFEST_FILE) if graph_repository is not None else "",
                "diagnostic_graph_path": str(graph_output_path) if not args.no_graph_paths else "",
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
    xml_output_path.write_text(render_flow_serial_node(xml_plan), encoding="utf-8")

    print(
        json.dumps(
            {
                "flow_steps": len(flow_plan.steps),
                "script_nodes": len(xml_plan.serial.scripts),
                "valid": validation.valid,
                "issues": len(validation.issues),
                "plan_path": str(output_path),
                "xml_path": str(xml_output_path),
                "trace_path": str(trace_output_path),
                "trace_markdown_path": "" if args.no_trace_markdown else str(trace_markdown_path),
                "evidence_chain_report_path": "" if args.no_evidence_chain_report else str(evidence_chain_path),
                "evidence_chain_report_markdown_path": ""
                if args.no_evidence_chain_report
                else str(evidence_chain_markdown_path),
                "raw_source_dir": str(Path(args.raw_source_dir)),
                "graph_path": str(graph_output_path) if not args.no_graph_paths else "",
                "kg_query": graph_repository is not None,
                "graph_paths": graph_path_count,
                "operation_xml_count": len(operation_xml_files),
                "dense_retrieval": bool(vector_options["enable_dense"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if validation.valid else 2


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


def _parse_default_args(values: list[str]) -> list[XmlArg]:
    args: list[XmlArg] = []
    for raw in values:
        if "=" not in raw:
            raise ValueError(f"--default-arg must use Name=Value format: {raw}")
        name, value = raw.split("=", 1)
        args.append(XmlArg(name=name.strip(), value=value.strip()))
    return args


def _write_operation_xml_files(xml_plan: XmlGenerationPlan, output_dir: Path) -> list[dict[str, str | int]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale_file in output_dir.glob("*.xml"):
        stale_file.unlink()
    files: list[dict[str, str | int]] = []
    for node in sorted(xml_plan.nodes, key=lambda item: (item.order, item.row, item.node_name)):
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
                "path": str(path),
            }
        )
    return files


def _build_trace_payload(
    flow_plan: FlowStepPlan,
    evidence_response: BuildStepEvidenceResponse,
    xml_plan: XmlGenerationPlan,
    operation_xml_files: list[dict[str, str | int]],
    content_chars: int,
    raw_source_dir: Path | None = None,
    kg_enabled: bool = False,
    kg_manifest_path: str = "",
    graph_repository: LocalGraphRepository | None = None,
) -> dict:
    bundles = {
        (bundle.step_key, node_bundle.node_name): node_bundle
        for bundle in evidence_response.bundles
        for node_bundle in bundle.node_bundles
    }
    planned_nodes = {
        (node.step_key, node.node_name): node
        for node in xml_plan.nodes
    }
    xml_paths = {
        (str(item["step_key"]), str(item["node_name"])): str(item["path"])
        for item in operation_xml_files
    }

    steps = []
    for step in flow_plan.steps:
        operations = []
        for node in sorted(step.parallel_nodes, key=lambda item: item.column):
            key = (step.step_key, node.name)
            bundle = bundles.get(key)
            planned = planned_nodes.get(key)
            graph_candidates = _trace_candidates(bundle, content_chars, raw_source_dir) if bundle else []
            retrieval_matches = _trace_matches(bundle, content_chars, raw_source_dir) if bundle else []
            selected_arg_evidence_ids = _selected_arg_evidence_ids(planned)
            reference_resolutions = _reference_resolutions(bundle, graph_repository)
            raw_sources = _operation_raw_sources(
                graph_candidates=graph_candidates,
                retrieval_matches=retrieval_matches,
                selected_evidence_ids=selected_arg_evidence_ids,
            )
            needs_review = _operation_needs_review(planned, graph_candidates, reference_resolutions)
            operations.append(
                {
                    "raw": node.raw,
                    "node_name": node.name,
                    "template_name": node.template_name,
                    "sheet": node.sheet,
                    "row": node.row,
                    "column": node.column,
                    "operation_xml_path": xml_paths.get(key, ""),
                    "generated_args": [
                        arg.model_dump(mode="json")
                        for arg in (planned.script.args if planned else [])
                    ],
                    "selected_args": [
                        arg.model_dump(mode="json")
                        for arg in (planned.script.args if planned else [])
                    ],
                    "selected_evidence_ids": planned.evidence_ids if planned else [],
                    "selected_arg_evidence_ids": selected_arg_evidence_ids,
                    "selected_section": _selected_role(bundle, "section_text") if bundle else {},
                    "selected_table_rows": _selected_roles(bundle, "table_row") if bundle else [],
                    "flowchart_title": _selected_flowchart_title(bundle) if bundle else "",
                    "reference_resolutions": reference_resolutions,
                    "template_match": node.template_name,
                    "missing_fields": planned.missing_fields if planned else [],
                    "notes": planned.notes if planned else [],
                    "needs_review": needs_review,
                    "kg_candidate_ids": _operation_kg_candidate_ids(graph_candidates),
                    "kg_paths": _operation_kg_paths(graph_candidates),
                    "provenance_refs": _operation_provenance_refs(graph_candidates),
                    "raw_sources": raw_sources,
                    "graph_candidates": graph_candidates,
                    "retrieval_matches": retrieval_matches,
                    "retrieval_trace": bundle.retrieval_trace if bundle else {},
                    "reference_paths": (bundle.retrieval_trace or {}).get("reference_hops", []) if bundle else [],
                    "kept_context": (bundle.retrieval_trace or {}).get("kept_context", []) if bundle else [],
                    "demoted_context": (bundle.retrieval_trace or {}).get("demoted_context", []) if bundle else [],
                    "dropped_context": (bundle.retrieval_trace or {}).get("dropped_context", []) if bundle else [],
                    "evidence_chain_report_node_id": f"{step.step_key}:{node.name}",
                    "graph_paths": bundle.graph_paths if bundle else [],
                }
            )
        steps.append(
            {
                "step_key": step.step_key,
                "display_name": step.display_name,
                "order": step.order,
                "row": step.row,
                "column": step.column,
                "serial_relation": "after_previous_column" if step.order > 1 else "first_column",
                "parallel_relation": "same_column_cells",
                "operations": operations,
            }
        )

    return {
        "source_flow_path": flow_plan.source_path,
        "relation_semantics": {
            "serial": "columns_left_to_right",
            "parallel": "rows_within_same_column",
            "operation_unit": "one flow cell / one ScriptNode XML",
            "batch_table_expansion": "disabled; multiple matching table rows are trace candidates only",
        },
        "kg_enabled": kg_enabled,
        "kg_manifest_path": kg_manifest_path,
        "top_level_xml": xml_plan.serial.name,
        "operation_xml_files": operation_xml_files,
        "steps": steps,
    }


def _trace_matches(bundle, content_chars: int, raw_source_dir: Path | None = None) -> list[dict]:
    matches = []
    for rank, match in enumerate(bundle.matches, start=1):
        evidence = match.evidence
        raw_source = _save_raw_source(evidence, raw_source_dir)
        matches.append(
            {
                "rank": rank,
                "score": match.score,
                "source": match.source,
                "matched_terms": match.matched_terms,
                "evidence_id": evidence.evidence_id,
                "evidence_type": evidence.evidence_type,
                "doc_id": evidence.doc_id,
                "source_path": evidence.source_path,
                "page_idx": evidence.page_idx,
                "bbox": evidence.bbox,
                "metadata": {**evidence.metadata, **match.metadata},
                "content_excerpt": _clip(evidence.content, content_chars),
                "raw_source": raw_source,
                "raw_source_path": raw_source.get("raw_source_path", ""),
            }
        )
    return matches


def _trace_candidates(bundle, content_chars: int, raw_source_dir: Path | None = None) -> list[dict]:
    candidates = []
    for rank, candidate in enumerate(bundle.candidates, start=1):
        evidence = candidate.evidence
        raw_source = _save_raw_source(evidence, raw_source_dir)
        candidates.append(
            {
                "rank": rank,
                "score": candidate.score,
                "score_breakdown": candidate.score_breakdown,
                "matched_terms": candidate.matched_terms,
                "graph_paths": candidate.graph_paths,
                "kg_candidate_ids": candidate.kg_candidate_ids,
                "kg_paths": candidate.kg_paths,
                "provenance_refs": candidate.provenance_refs,
                "needs_review": candidate.needs_review,
                "block_role": candidate.block_role,
                "parameter_fields": candidate.parameter_fields,
                "flowchart_title": candidate.flowchart_title,
                "template_match": candidate.template_match,
                "retrieval_source": candidate.retrieval_source,
                "retrieval_rank": candidate.retrieval_rank,
                "retrieval_score": candidate.retrieval_score,
                "retrieval_metadata": candidate.retrieval_metadata,
                "evidence_id": evidence.evidence_id,
                "evidence_type": evidence.evidence_type,
                "doc_id": evidence.doc_id,
                "source_path": evidence.source_path,
                "page_idx": evidence.page_idx,
                "bbox": evidence.bbox,
                "metadata": evidence.metadata,
                "content_excerpt": _clip(evidence.content, content_chars),
                "raw_source": raw_source,
                "raw_source_path": raw_source.get("raw_source_path", candidate.raw_source_path),
            }
        )
    return candidates


def _selected_role(bundle, block_role: str) -> dict:
    for candidate in bundle.candidates:
        if candidate.block_role == block_role:
            return _candidate_summary(candidate)
    return {}


def _selected_roles(bundle, block_role: str) -> list[dict]:
    return [_candidate_summary(candidate) for candidate in bundle.candidates if candidate.block_role == block_role]


def _selected_flowchart_title(bundle) -> str:
    for candidate in bundle.candidates:
        if candidate.flowchart_title:
            return candidate.flowchart_title
        value = candidate.parameter_fields.get("FLOWCHART_TITLE")
        if value:
            return str(value)
    return ""


def _candidate_summary(candidate) -> dict:
    evidence = candidate.evidence
    return {
        "evidence_id": evidence.evidence_id,
        "doc_id": evidence.doc_id,
        "source_path": evidence.source_path,
        "page_idx": evidence.page_idx,
        "score": candidate.score,
        "score_breakdown": candidate.score_breakdown,
        "matched_terms": candidate.matched_terms,
        "parameter_fields": candidate.parameter_fields,
        "kg_candidate_ids": candidate.kg_candidate_ids,
        "kg_paths": candidate.kg_paths,
        "provenance_refs": candidate.provenance_refs,
        "needs_review": candidate.needs_review,
    }


def _selected_arg_evidence_ids(planned) -> list[str]:
    if planned is None:
        return []
    ids: list[str] = []
    for arg in planned.script.args:
        for evidence_id in arg.evidence_ids:
            if evidence_id not in ids:
                ids.append(evidence_id)
    return ids


def _operation_raw_sources(
    graph_candidates: list[dict],
    retrieval_matches: list[dict],
    selected_evidence_ids: list[str],
) -> list[dict]:
    by_path: dict[str, dict] = {}
    selected = set(selected_evidence_ids)
    for item in [*graph_candidates, *retrieval_matches]:
        raw_source = dict(item.get("raw_source") or {})
        path = str(raw_source.get("raw_source_path") or "")
        if not path:
            continue
        raw_source["selected"] = str(item.get("evidence_id") or "") in selected
        by_path.setdefault(path, raw_source)
        if raw_source["selected"]:
            by_path[path]["selected"] = True
    return sorted(by_path.values(), key=lambda item: (not bool(item.get("selected")), str(item.get("raw_source_path"))))


def _operation_kg_candidate_ids(graph_candidates: list[dict]) -> list[str]:
    values: list[str] = []
    for candidate in graph_candidates:
        for value in candidate.get("kg_candidate_ids") or []:
            if value not in values:
                values.append(value)
    return values


def _operation_kg_paths(graph_candidates: list[dict]) -> list[str]:
    values: list[str] = []
    for candidate in graph_candidates:
        for value in candidate.get("kg_paths") or []:
            if value not in values:
                values.append(value)
    return values


def _operation_provenance_refs(graph_candidates: list[dict]) -> list[str]:
    values: list[str] = []
    for candidate in graph_candidates:
        for value in candidate.get("provenance_refs") or []:
            if value not in values:
                values.append(value)
    return values


def _operation_needs_review(planned, graph_candidates: list[dict], reference_resolutions: list[dict]) -> list[str]:
    values: list[str] = []
    for value in (planned.notes if planned else []):
        if value.startswith("ambiguous") or value.endswith("review"):
            values.append(value)
    for candidate in graph_candidates:
        for value in candidate.get("needs_review") or []:
            if value not in values:
                values.append(value)
    for resolution in reference_resolutions:
        for value in resolution.get("needs_review") or []:
            if value not in values:
                values.append(value)
    return values


def _reference_resolutions(bundle, graph_repository: LocalGraphRepository | None) -> list[dict]:
    if bundle is None or graph_repository is None:
        return []
    titles: list[str] = []
    for candidate in bundle.candidates:
        for title in [
            candidate.flowchart_title,
            candidate.parameter_fields.get("FLOWCHART_TITLE", ""),
            candidate.parameter_fields.get("REFERENCE_TEXT", ""),
        ]:
            if title and title not in titles:
                titles.append(str(title))
    resolutions: list[dict] = []
    for title in titles:
        for resolution in graph_repository.resolve_references(title, limit=3):
            resolutions.append(resolution.model_dump(mode="json"))
    return resolutions


def _save_raw_source(evidence: EvidenceUnit, output_dir: Path | None) -> dict:
    content_hash = hashlib.sha256(evidence.content.encode("utf-8")).hexdigest()
    record = {
        "evidence_id": evidence.evidence_id,
        "doc_id": evidence.doc_id,
        "source_path": evidence.source_path or "",
        "page_idx": evidence.page_idx,
        "bbox": evidence.bbox,
        "content_hash": content_hash,
        "raw_source_path": "",
    }
    if output_dir is None:
        return record
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{_slug(evidence.evidence_id)}_{content_hash[:12]}.md"
    if not path.exists():
        header = {
            "evidence_id": evidence.evidence_id,
            "doc_id": evidence.doc_id,
            "source_path": evidence.source_path or "",
            "page_idx": evidence.page_idx,
            "bbox": evidence.bbox,
            "metadata": evidence.metadata,
            "content_hash": content_hash,
        }
        path.write_text(
            "---\n"
            + json.dumps(header, ensure_ascii=False, indent=2)
            + "\n---\n\n"
            + evidence.content
            + "\n",
            encoding="utf-8",
        )
    record["raw_source_path"] = str(path)
    return record


def _render_trace_markdown(payload: dict) -> str:
    lines = [
        "# Flow Evidence Trace",
        "",
        f"- Source flow: `{payload.get('source_flow_path', '')}`",
        f"- KG enabled: `{payload.get('kg_enabled', False)}`",
        f"- KG manifest: `{payload.get('kg_manifest_path', '')}`",
        f"- Operation unit: `{payload.get('relation_semantics', {}).get('operation_unit', '')}`",
        f"- Batch table expansion: `{payload.get('relation_semantics', {}).get('batch_table_expansion', '')}`",
        "",
    ]
    for step in payload.get("steps") or []:
        lines.extend(
            [
                f"## {step.get('step_key')} - {step.get('display_name')}",
                "",
            ]
        )
        for operation in step.get("operations") or []:
            lines.extend(_render_operation_markdown(operation))
    return "\n".join(lines).rstrip() + "\n"


def _render_operation_markdown(operation: dict) -> list[str]:
    lines = [
        f"### {operation.get('node_name')}",
        "",
        f"- Template: `{operation.get('template_name', '')}`",
        f"- Excel: row `{operation.get('row')}`, column `{operation.get('column')}`",
        f"- Operation XML: `{operation.get('operation_xml_path', '')}`",
        f"- Missing fields: `{', '.join(operation.get('missing_fields') or []) or 'None'}`",
        f"- Needs review: `{', '.join(operation.get('needs_review') or []) or 'None'}`",
        "",
        "| Arg | Value | Evidence | Score | Raw Source |",
        "| --- | --- | --- | --- | --- |",
    ]
    for arg in operation.get("selected_args") or []:
        lines.append(
            "| "
            + " | ".join(
                [
                    _md_cell(arg.get("name")),
                    _md_cell(arg.get("value")),
                    _md_cell(", ".join(arg.get("evidence_ids") or [])),
                    _md_cell(arg.get("selection_score")),
                    _md_cell(arg.get("raw_source_path")),
                ]
            )
            + " |"
        )
    lines.extend(["", "| Rank | Evidence | Role | Score | Fields | Raw Source |", "| --- | --- | --- | --- | --- | --- |"])
    for candidate in (operation.get("graph_candidates") or [])[:6]:
        lines.append(
            "| "
            + " | ".join(
                [
                    _md_cell(candidate.get("rank")),
                    _md_cell(candidate.get("evidence_id")),
                    _md_cell(candidate.get("block_role")),
                    _md_cell(candidate.get("score")),
                    _md_cell(_short_fields(candidate.get("parameter_fields") or {})),
                    _md_cell(candidate.get("raw_source_path")),
                ]
            )
            + " |"
        )
    if operation.get("kg_paths"):
        lines.extend(["", "KG paths:"])
        for path in (operation.get("kg_paths") or [])[:8]:
            lines.append(f"- `{path}`")
    if operation.get("reference_resolutions"):
        lines.extend(["", "Reference resolutions:"])
        for resolution in operation.get("reference_resolutions") or []:
            nodes = ", ".join(resolution.get("resolved_node_ids") or []) or "unresolved"
            review = ", ".join(resolution.get("needs_review") or []) or "None"
            lines.append(f"- `{resolution.get('reference_text')}` -> `{nodes}` review=`{review}`")
    if operation.get("raw_sources"):
        lines.extend(["", "Raw sources:"])
        for source in operation.get("raw_sources") or []:
            selected = "selected" if source.get("selected") else "candidate"
            lines.append(f"- `{selected}` `{source.get('evidence_id')}` -> `{source.get('raw_source_path')}`")
    lines.append("")
    return lines


def _short_fields(fields: dict) -> str:
    return ", ".join(f"{key}={value}" for key, value in list(fields.items())[:5])


def _md_cell(value) -> str:
    text = "" if value is None else str(value)
    text = text.replace("|", "\\|").replace("\n", " ")
    return text


def _clip(value: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if limit <= 0 or len(value) <= limit:
        return value
    return value[:limit].rstrip() + "..."


def _slug(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z_.-]+", "_", value.strip())
    return value.strip("._-") or "operation"


if __name__ == "__main__":
    raise SystemExit(main())
