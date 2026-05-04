"""Prompt builders for constrained diagnostic LLM tasks."""

from __future__ import annotations

import json

from diagnostic_platform.schemas import ChatMessage, WorkflowResolveDecision


ALLOWED_LLM_ACTIONS = {"keep_base", "accept_plan", "needs_review"}


def build_audit_resolution_messages(
    decision: WorkflowResolveDecision,
    evidence_context: str = "",
) -> list[ChatMessage]:
    """Build a constrained prompt for one workflow audit conflict."""

    payload = {
        "node_name": decision.node_name,
        "class_name": decision.class_name,
        "arg_name": decision.arg_name,
        "audit_status": decision.audit_status,
        "base_value": decision.base_value,
        "plan_value": decision.plan_value,
        "fused_value": decision.fused_value,
        "allowed_by_contract": decision.allowed_by_contract,
        "required_by_contract": decision.required_by_contract,
        "evidence_ids": decision.evidence_ids,
        "resolver_reason": decision.reason,
    }
    system = (
        "You are a vehicle diagnostic XML audit resolver. "
        "You must choose the safest action for one ScriptNode Arg conflict. "
        "Never output XML. Return only one JSON object."
    )
    evidence_block = ""
    if evidence_context.strip():
        evidence_block = (
            "\n\nBudgeted evidence context:\n"
            f"{evidence_context.strip()}\n"
        )
    user = (
        "Resolve this workflow audit item.\n\n"
        "Rules:\n"
        "- Prefer keep_base when the existing workflow value is a stable engineering value.\n"
        "- Choose accept_plan only when the plan value is clearly more specific and compatible with the ClassName contract.\n"
        "- Choose needs_review if evidence is insufficient or values refer to different ECU/DID concepts.\n"
        "- Do not invent values.\n\n"
        "Return JSON schema:\n"
        '{"action":"keep_base|accept_plan|needs_review","confidence":0.0,"reason":"short reason"}\n\n'
        f"Audit item:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
        f"{evidence_block}"
    )
    return [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=user),
    ]
