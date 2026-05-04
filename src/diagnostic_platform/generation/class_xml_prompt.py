"""Prompt construction for ClassName TaskNode XML generation."""

from __future__ import annotations

import json

from diagnostic_platform.retrieval.prompt_budget import estimate_tokens
from diagnostic_platform.schemas import (
    ChatMessage,
    NodeEvidenceBundle,
    TemplateFamilyResolution,
    XmlPromptContextItem,
)


def build_class_xml_generation_messages(
    *,
    node_bundle: NodeEvidenceBundle,
    family_resolution: TemplateFamilyResolution,
    context_items: list[XmlPromptContextItem] | None = None,
    max_example_chars: int = 6000,
) -> list[ChatMessage]:
    """Build a prompt for generating the full TaskNode implementation XML for one FlowNode."""

    return [
        ChatMessage(role="system", content=_system_prompt()),
        ChatMessage(role="developer", content=_developer_prompt()),
        ChatMessage(
            role="user",
            content=_user_prompt(
                node_bundle=node_bundle,
                family_resolution=family_resolution,
                context_items=context_items or [],
                max_example_chars=max_example_chars,
            ),
        ),
    ]


def _user_prompt(
    *,
    node_bundle: NodeEvidenceBundle,
    family_resolution: TemplateFamilyResolution,
    context_items: list[XmlPromptContextItem],
    max_example_chars: int,
) -> str:
    family = family_resolution.family
    payload = {
        "task": "generate_one_classname_tasknode_xml",
        "operation": {
            "step_key": node_bundle.step_key,
            "node_name": node_bundle.node_name,
            "template_name": node_bundle.template_name,
            "row": node_bundle.row,
            "column": node_bundle.column,
        },
        "resolved_template_family": {
            "status": family_resolution.status,
            "family_id": family_resolution.family_id,
            "score": family_resolution.score,
            "match_reasons": family_resolution.match_reasons,
            "aliases": family.aliases if family else [],
            "required_evidence": family.required_evidence if family else [],
            "validation_hints": family.validation_hints if family else [],
            "merged_signature": family.merged_signature.model_dump(mode="json") if family and family.merged_signature else None,
        },
        "flowchart_descriptions": [
            {
                "flowchart_id": flowchart.flowchart_id,
                "title": flowchart.title,
                "source_path": flowchart.source_path,
                "start_line": flowchart.start_line,
                "end_line": flowchart.end_line,
                "content": _clip(flowchart.content, 3000),
            }
            for flowchart in (family.flowcharts if family else [])
        ],
        "xml_family_examples": [
            {
                "source_path": path,
                "xml": _clip(xml, max_example_chars),
            }
            for path, xml in zip((family.xml_example_paths if family else []), (family.xml_examples if family else []))
        ],
        "evidence_context": [_context_item_payload(item) for item in context_items],
        "candidate_evidence": [
            {
                "evidence_id": candidate.evidence.evidence_id,
                "type": candidate.evidence.evidence_type,
                "source_path": candidate.evidence.source_path,
                "page_idx": candidate.evidence.page_idx,
                "block_role": candidate.block_role,
                "score": candidate.score,
                "content": _clip(candidate.evidence.content, 1800),
            }
            for candidate in _ordered_candidates_for_class_xml(node_bundle.candidates)[:12]
        ],
        "output_schema": {
            "node_name": "must equal operation.node_name",
            "class_name": "ClassName/TaskNode implementation name",
            "template_family": "resolved family_id",
            "class_xml": "one well-formed <TaskNode>...</TaskNode> implementation XML",
            "used_evidence": ["evidence_id or flowchart_id"],
            "confidence": "0..1",
            "needs_review": "true if evidence is missing/conflicting",
            "missing_or_conflict_reason": "required when needs_review=true",
        },
        "prompt_estimated_tokens_without_examples": estimate_tokens(
            json.dumps(
                {
                    "operation": node_bundle.node_name,
                    "family": family_resolution.family_id,
                    "evidence_count": len(context_items) or len(node_bundle.candidates),
                },
                ensure_ascii=False,
            )
        ),
    }
    return "Return only one compact JSON object.\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _context_item_payload(item: XmlPromptContextItem) -> dict:
    return {
        "context_id": item.context_id,
        "evidence_id": item.evidence_id,
        "kind": item.kind,
        "source_path": item.source_path,
        "page_idx": item.page_idx,
        "block_role": item.block_role,
        "parameter_fields": item.parameter_fields,
        "raw_source_path": item.raw_source_path,
        "content": _clip(item.content, 1800),
    }


def _ordered_candidates_for_class_xml(candidates):
    return sorted(
        candidates,
        key=lambda candidate: (
            -_candidate_prompt_priority(candidate),
            -candidate.score,
            candidate.evidence.evidence_id,
        ),
    )


def _candidate_prompt_priority(candidate) -> float:
    metadata = candidate.evidence.metadata or {}
    role = str(candidate.block_role or metadata.get("block_role") or candidate.evidence.evidence_type)
    module = str(metadata.get("module") or "")
    priority = 0.0
    if module == "ecu_parameter":
        priority += 20.0
    if role in {"section_text", "table"}:
        priority += 14.0
    if role == "table_row":
        priority += 8.0
    if candidate.retrieval_source in {"parent_context", "sibling_table_context"}:
        priority += 4.0
    if candidate.flowchart_title or metadata.get("flowchart_title"):
        priority += 3.0
    if candidate.evidence.evidence_type == "flowchart" or role in {"flowchart_image", "flowchart_title"}:
        priority += 10.0
    return priority


def _system_prompt() -> str:
    return (
        "You generate auditable automotive diagnostic ClassName implementation XML. "
        "Use only supplied evidence, flowchart descriptions, and same-family XML examples. "
        "Do not generate the main workflow wrapper and do not generate ScriptNode invocation XML."
    )


def _developer_prompt() -> str:
    return "\n".join(
        [
            "Generation rules:",
            "- Output exactly one TaskNode XML implementation in the JSON field class_xml.",
            "- The TaskNode implements the operation's ClassName; ScriptNode is only the caller in the main workflow.",
            "- Preserve the resolved template family's structural pattern unless evidence requires a value change.",
            "- Business values such as DID, RID, DTC HEX, NRC, request parameter, and ECU-specific identifiers must come from evidence_context.",
            "- Flow control such as retry, positive response, negative response handling, and security/session steps should follow the flowchart and XML family examples.",
            "- If a required table/flowchart/template is missing, set needs_review=true and explain it.",
            "- Return JSON only. No Markdown fences.",
        ]
    )


def _clip(value: str, max_chars: int) -> str:
    text = str(value or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + "...[clipped]"
