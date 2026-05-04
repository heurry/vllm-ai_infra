"""Tests for vLLM client helpers and LLM audit resolver."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.generation.vllm_client import extract_json_object
from diagnostic_platform.resolution.llm_resolver import resolve_audit_with_llm
from diagnostic_platform.schemas import (
    WorkflowLlmResolveAuditRequest,
    WorkflowResolveAuditReport,
    WorkflowResolveDecision,
)


class VllmLlmResolverTest(unittest.TestCase):
    def test_extract_json_object_from_markdown(self) -> None:
        payload = extract_json_object(
            """```json
{"action":"keep_base","confidence":0.91,"reason":"stable workflow value"}
```"""
        )

        self.assertEqual(payload["action"], "keep_base")
        self.assertEqual(payload["confidence"], 0.91)

    def test_resolve_audit_with_fake_llm_client(self) -> None:
        report = WorkflowResolveAuditReport(
            valid=True,
            decisions=[
                WorkflowResolveDecision(
                    node_name="Usage_Mode_Change_1",
                    class_name="GEEA30_VMM_Change",
                    arg_name="DID",
                    audit_status="plan_conflict_kept_base",
                    action="keep_base",
                    requires_review=True,
                    reason="plan_conflicts_with_existing_workflow_value",
                    confidence=0.65,
                    allowed_by_contract=True,
                    required_by_contract=True,
                    base_value="0xDD0A",
                    plan_value="0x7000",
                    fused_value="0xDD0A",
                    evidence_ids=["ev_001"],
                ),
                WorkflowResolveDecision(
                    node_name="Car_Mode_Change_1",
                    class_name="GEEA30_VMM_Change",
                    arg_name="DID",
                    audit_status="confirmed_by_plan",
                    action="no_action",
                    requires_review=False,
                    reason="audit_status_confirmed_by_plan",
                    confidence=1.0,
                    base_value="0xD134",
                    plan_value="0xD134",
                    fused_value="0xD134",
                ),
            ],
        )

        resolved = resolve_audit_with_llm(
            WorkflowLlmResolveAuditRequest(
                resolution_report=report,
                evidence_contexts={
                    "Usage_Mode_Change_1": "Evidence says 0xDD0A is the usage mode DID.",
                    "ev_001": "Protocol table confirms usage mode change parameter.",
                },
            ),
            client=_FakeClient(),
        )

        self.assertEqual(resolved.summary["llm_calls"], 1)
        self.assertEqual(resolved.summary["review_required"], 0)
        self.assertEqual(resolved.decisions[0].action, "keep_base")
        self.assertEqual(resolved.decisions[0].confidence, 0.92)
        self.assertTrue(resolved.decisions[0].reason.startswith("llm:"))
        self.assertEqual(resolved.decisions[1].action, "no_action")
        self.assertEqual(resolved.summary["prompt_budgeted_items"], 1)
        self.assertEqual(resolved.prompt_budget_reports["Usage_Mode_Change_1.DID"]["kept_sections"], 2)
        self.assertIn("Budgeted evidence context", _FakeClient.last_user_message)


class _FakeClient:
    last_user_message = ""

    def chat_json(self, messages, extra_body=None, profile=None):
        self.__class__.last_user_message = messages[-1].content
        return {
            "action": "keep_base",
            "confidence": 0.92,
            "reason": "Existing workflow DID matches the intended usage mode change parameter.",
        }


if __name__ == "__main__":
    unittest.main()
