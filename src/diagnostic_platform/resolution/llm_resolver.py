"""vLLM-assisted resolver for workflow audit decisions."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Protocol

from diagnostic_platform.generation.prompt_builder import ALLOWED_LLM_ACTIONS, build_audit_resolution_messages
from diagnostic_platform.generation.vllm_client import VllmClient, VllmClientError
from diagnostic_platform.retrieval.prompt_budget import PromptBudgetConfig, PromptSection, budget_text_sections
from diagnostic_platform.schemas import (
    ValidationIssue,
    WorkflowLlmResolveAuditReport,
    WorkflowLlmResolveAuditRequest,
    WorkflowResolveAuditReport,
    WorkflowResolveDecision,
)
from diagnostic_platform.serving.profiles import RequestProfile
from diagnostic_platform.serving.router import build_default_router


class JsonChatClient(Protocol):
    """Protocol for testing vLLM JSON chat calls."""

    def chat_json(self, messages, extra_body=None, profile=None): ...


def resolve_audit_with_llm(
    request: WorkflowLlmResolveAuditRequest,
    client: JsonChatClient | None = None,
) -> WorkflowLlmResolveAuditReport:
    """Resolve review-required decisions by calling a vLLM OpenAI-compatible endpoint."""

    report = _load_resolution_report(request)
    router = None
    if request.enable_router:
        router = build_default_router(
            short_base_urls=request.router_short_base_urls or [request.vllm_config.base_url],
            long_base_url=request.router_long_base_url or request.vllm_config.base_url,
            extended_base_url=request.router_extended_base_url or None,
            extreme_base_url=request.router_extreme_base_url or None,
            model=request.vllm_config.model,
        )
    llm_client = client or VllmClient(request.vllm_config, router=router, profile=_request_profile(request.request_profile))
    prompt_budget = PromptBudgetConfig(
        max_prompt_tokens=request.prompt_budget_tokens,
        reserved_output_tokens=request.prompt_reserved_output_tokens,
    )

    decisions: list[WorkflowResolveDecision] = []
    raw_responses: dict[str, str] = {}
    issues: list[ValidationIssue] = []
    llm_calls = 0
    prompt_budget_reports: dict[str, dict] = {}
    for decision in report.decisions:
        if not decision.requires_review or llm_calls >= request.max_items:
            decisions.append(decision)
            continue

        key = _decision_key(decision)
        try:
            evidence_context, budget_report = _budgeted_evidence_context(
                decision,
                request.evidence_contexts,
                prompt_budget,
            )
            prompt_budget_reports[key] = budget_report
            payload = llm_client.chat_json(
                build_audit_resolution_messages(decision, evidence_context=evidence_context),
                profile=_request_profile(request.request_profile),
            )
            raw_responses[key] = json.dumps(payload, ensure_ascii=False)
            resolved = _apply_llm_payload(decision, payload)
            decisions.append(resolved)
            llm_calls += 1
        except (VllmClientError, ValueError, TypeError) as exc:
            decisions.append(decision)
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="LLM_RESOLUTION_FAILED",
                    message=f"{key}: {exc}",
                )
            )

    action_counts = Counter(decision.action for decision in decisions)
    review_count = sum(1 for decision in decisions if decision.requires_review)
    summary = {
        "decisions": len(decisions),
        "llm_calls": llm_calls,
        "review_required": review_count,
        "action_counts": dict(sorted(action_counts.items())),
        "routed": bool(request.enable_router),
        "prompt_budget_tokens": request.prompt_budget_tokens,
        "prompt_budgeted_items": len(prompt_budget_reports),
    }
    output = WorkflowLlmResolveAuditReport(
        valid=not any(issue.severity == "error" for issue in issues),
        summary=summary,
        decisions=decisions,
        issues=issues,
        raw_responses=raw_responses,
        prompt_budget_reports=prompt_budget_reports,
    )
    if request.output_path:
        _write_report(output, request.output_path)
        output.output_path = request.output_path
    return output


def _apply_llm_payload(decision: WorkflowResolveDecision, payload: dict) -> WorkflowResolveDecision:
    action = str(payload.get("action") or "").strip()
    if action not in ALLOWED_LLM_ACTIONS:
        raise ValueError(f"Invalid LLM action: {action}")

    confidence = float(payload.get("confidence", 0.0))
    confidence = max(0.0, min(confidence, 1.0))
    reason = str(payload.get("reason") or "llm_resolution").strip()
    requires_review = action == "needs_review" or confidence < 0.65

    return decision.model_copy(
        update={
            "action": action,
            "requires_review": requires_review,
            "reason": f"llm:{reason}",
            "confidence": confidence,
        }
    )


def _load_resolution_report(request: WorkflowLlmResolveAuditRequest) -> WorkflowResolveAuditReport:
    if request.resolution_report is not None:
        return request.resolution_report
    if request.resolution_report_path:
        payload = json.loads(Path(request.resolution_report_path).read_text(encoding="utf-8"))
        return WorkflowResolveAuditReport.model_validate(payload)
    raise ValueError("Either resolution_report or resolution_report_path is required.")


def _write_report(report: WorkflowLlmResolveAuditReport, output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _decision_key(decision: WorkflowResolveDecision) -> str:
    return f"{decision.node_name}.{decision.arg_name}"


def _request_profile(value: str) -> RequestProfile:
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
    return value if value in allowed else "default"  # type: ignore[return-value]


def _budgeted_evidence_context(
    decision: WorkflowResolveDecision,
    evidence_contexts: dict[str, str],
    prompt_budget: PromptBudgetConfig,
) -> tuple[str, dict]:
    sections = _context_sections(decision, evidence_contexts)
    kept, report = budget_text_sections(sections, prompt_budget)
    context = "\n\n".join(f"[{section.label}]\n{section.content}" for section in kept)
    return (
        context,
        {
            "original_sections": report.original_matches,
            "kept_sections": report.kept_matches,
            "dropped_sections": report.dropped_matches,
            "estimated_tokens": report.estimated_tokens,
            "budget_tokens": report.budget_tokens,
            "dropped_reasons": report.dropped_reasons,
        },
    )


def _context_sections(decision: WorkflowResolveDecision, evidence_contexts: dict[str, str]) -> list[PromptSection]:
    keys: list[tuple[str, float]] = [
        (_decision_key(decision), 3.0),
        (decision.node_name, 2.0),
    ]
    keys.extend((evidence_id, 1.0) for evidence_id in decision.evidence_ids)

    sections: list[PromptSection] = []
    seen: set[str] = set()
    for key, score in keys:
        if key in seen:
            continue
        seen.add(key)
        content = evidence_contexts.get(key, "")
        if content:
            sections.append(PromptSection(label=key, content=content, score=score))
    return sections
