"""Tests for prompt budget helpers."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.retrieval.prompt_budget import PromptBudgetConfig, budget_node_bundle
from diagnostic_platform.retrieval.prompt_budget import PromptSection, budget_text_sections
from diagnostic_platform.schemas import EvidenceMatch, EvidenceUnit, NodeEvidenceBundle


class PromptBudgetTest(unittest.TestCase):
    def test_budget_node_bundle_dedupes_and_trims(self) -> None:
        evidence_a = _evidence("ev_a", "A" * 400)
        evidence_b = _evidence("ev_b", "B" * 400)
        bundle = NodeEvidenceBundle(
            step_key="step_1",
            node_name="ADCU_DTC_Read",
            template_name="DTC_Read_Type1",
            matches=[
                EvidenceMatch(evidence=evidence_a, score=0.9),
                EvidenceMatch(evidence=evidence_a, score=0.8),
                EvidenceMatch(evidence=evidence_b, score=0.7),
            ],
            graph_paths=["a", "a", "b"],
        )

        budgeted, report = budget_node_bundle(
            bundle,
            PromptBudgetConfig(max_prompt_tokens=220, reserved_output_tokens=20, max_evidence_items=2, max_graph_paths=1),
        )

        self.assertEqual([match.evidence.evidence_id for match in budgeted.matches], ["ev_a", "ev_b"])
        self.assertEqual(budgeted.graph_paths, ["a"])
        self.assertEqual(report.dropped_reasons["duplicate_evidence"], 1)
        self.assertIn("prompt_budget:", budgeted.notes[-1])

    def test_budget_text_sections_truncates_oversized_first_section(self) -> None:
        sections = [
            PromptSection(label="node", content="A" * 1000, score=2.0),
            PromptSection(label="ev_001", content="B" * 100, score=1.0),
        ]

        kept, report = budget_text_sections(
            sections,
            PromptBudgetConfig(max_prompt_tokens=80, reserved_output_tokens=20, max_evidence_items=4),
        )

        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].label, "node")
        self.assertIn("truncated by prompt budget", kept[0].content)
        self.assertEqual(report.dropped_reasons["section_truncated"], 1)
        self.assertEqual(report.dropped_reasons["token_budget"], 1)


def _evidence(evidence_id: str, content: str) -> EvidenceUnit:
    return EvidenceUnit(
        evidence_id=evidence_id,
        evidence_type="text",
        content=content,
        doc_id="doc",
        page_idx=1,
    )


if __name__ == "__main__":
    unittest.main()
