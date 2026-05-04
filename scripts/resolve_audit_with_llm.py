#!/usr/bin/env python3
"""Resolve remaining workflow audit conflicts with a vLLM endpoint."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from diagnostic_platform.resolution.llm_resolver import resolve_audit_with_llm  # noqa: E402
from diagnostic_platform.schemas import VllmModelConfig, WorkflowLlmResolveAuditRequest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--resolution-report-path",
        default="data/processed/workflow/treg_20260402/audit_resolution_report.json",
    )
    parser.add_argument("--output-path", default="data/processed/workflow/treg_20260402/llm_resolution_report.json")
    parser.add_argument("--base-url", default="http://127.0.0.1:8008/v1")
    parser.add_argument("--model", default="qwen-audit-resolver")
    parser.add_argument("--api-key", default="EMPTY")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--max-items", type=int, default=20)
    parser.add_argument("--request-profile", default="short_audit")
    parser.add_argument("--enable-router", action="store_true")
    parser.add_argument(
        "--short-base-url",
        action="append",
        default=[],
        help="Short-request vLLM endpoint. Repeat for 2-instance routing.",
    )
    parser.add_argument("--long-base-url", default="")
    parser.add_argument("--extended-base-url", default="")
    parser.add_argument("--extreme-base-url", default="")
    parser.add_argument(
        "--evidence-trace-path",
        default="",
        help="Optional flow_evidence_trace.json for adding budgeted evidence context to LLM prompts.",
    )
    parser.add_argument("--prompt-budget-tokens", type=int, default=4096)
    parser.add_argument("--prompt-reserved-output-tokens", type=int, default=512)
    args = parser.parse_args()

    evidence_contexts = _load_evidence_contexts(Path(args.evidence_trace_path)) if args.evidence_trace_path else {}
    report = resolve_audit_with_llm(
        WorkflowLlmResolveAuditRequest(
            resolution_report_path=args.resolution_report_path,
            output_path=args.output_path,
            max_items=args.max_items,
            request_profile=args.request_profile,
            enable_router=args.enable_router,
            router_short_base_urls=args.short_base_url,
            router_long_base_url=args.long_base_url or None,
            router_extended_base_url=args.extended_base_url or None,
            router_extreme_base_url=args.extreme_base_url or None,
            evidence_contexts=evidence_contexts,
            prompt_budget_tokens=args.prompt_budget_tokens,
            prompt_reserved_output_tokens=args.prompt_reserved_output_tokens,
            vllm_config=VllmModelConfig(
                base_url=args.base_url,
                model=args.model,
                api_key=args.api_key,
                timeout_seconds=args.timeout_seconds,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
            ),
        )
    )
    print(
        json.dumps(
            {
                "valid": report.valid,
                "summary": report.summary,
                "issues": len(report.issues),
                "output_path": report.output_path,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report.valid else 2


def _load_evidence_contexts(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    contexts: dict[str, str] = {}
    for step in payload.get("steps") or []:
        for operation in step.get("operations") or []:
            node_name = str(operation.get("node_name") or "")
            if not node_name:
                continue
            operation_context = _operation_context(operation)
            if operation_context:
                contexts[node_name] = operation_context
            for match in operation.get("retrieval_matches") or []:
                evidence_id = str(match.get("evidence_id") or "")
                if evidence_id:
                    contexts[evidence_id] = _match_context(match)
    return contexts


def _operation_context(operation: dict) -> str:
    lines = [
        f"node_name={operation.get('node_name', '')}",
        f"template_name={operation.get('template_name', '')}",
    ]
    selected_args = operation.get("selected_args") or []
    if selected_args:
        lines.append(f"selected_args={json.dumps(selected_args, ensure_ascii=False)}")
    graph_paths = operation.get("graph_paths") or []
    if graph_paths:
        lines.append("graph_paths:")
        lines.extend(f"- {path}" for path in graph_paths[:8])
    matches = operation.get("retrieval_matches") or []
    if matches:
        lines.append("retrieval_matches:")
        lines.extend(f"- {_match_context(match)}" for match in matches[:8])
    return "\n".join(str(line) for line in lines if line)


def _match_context(match: dict) -> str:
    fields = [
        f"evidence_id={match.get('evidence_id', '')}",
        f"score={match.get('score', '')}",
        f"doc_id={match.get('doc_id', '')}",
        f"page={match.get('page_idx', '')}",
        f"matched_terms={match.get('matched_terms', [])}",
        f"metadata={json.dumps(match.get('metadata') or {}, ensure_ascii=False)}",
        f"content={match.get('content_excerpt', '')}",
    ]
    return " | ".join(str(field) for field in fields if field)


if __name__ == "__main__":
    raise SystemExit(main())
