"""Tests for generic evidence-chain planning, reference expansion, and reports."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.schemas import (  # noqa: E402
    BuildStepEvidenceRequest,
    EvidenceUnit,
    FlowNode,
    FlowStep,
    FlowStepPlan,
    TaskEvidenceCandidate,
)
from diagnostic_platform.tracing.evidence_bundle import build_step_evidence_bundles  # noqa: E402
from diagnostic_platform.tracing.evidence_chain import (  # noqa: E402
    build_evidence_chain_report,
    build_query_plan,
    filter_evidence_context,
    include_parent_context,
    render_evidence_chain_markdown,
)


class EvidenceChainTest(unittest.TestCase):
    def test_query_plan_is_generic_for_non_dtc_nodes(self) -> None:
        plan = build_query_plan(
            FlowNode(
                raw="TCAM_IMMO_Secret_Key_Write(Information_Write_Type3)",
                name="TCAM_IMMO_Secret_Key_Write",
                template_name="Information_Write_Type3",
                sheet="Sheet1",
                row=1,
                column=1,
            )
        )

        query_types = {item.query_type for item in plan.queries}
        self.assertIn("anchor", query_types)
        self.assertIn("ecu_doc", query_types)
        self.assertIn("public_process", query_types)
        self.assertIn("template", query_types)
        self.assertEqual(plan.node_intent.target_ecu, "TCAM")
        self.assertIn("Secret", plan.node_intent.operation_phrase)

    def test_reference_expansion_filtering_and_report_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parameter_path = root / "GEEA3.0_EOL_TREG_ADCU1301_Parameter-1756876939.md"
            process_path = root / "EOL_TREG_EOL_Process-Test_Sequence-1751025478.md"
            evidence_units = [
                EvidenceUnit(
                    evidence_id="param_adcu1301_dtc_clear",
                    evidence_type="text",
                    content=(
                        "4.2 DTC Clear\n"
                        "Refer to the EOL process-DTC Clear Type7(Single DTC NRC Allowed)\n"
                        "<table><tr><td>PCODE</td><td>HEX</td><td>NRC ALLOWED</td></tr>"
                        "<tr><td>U2F7292</td><td>EF7292</td><td>0x31</td></tr>"
                        "<tr><td>U2F7392</td><td>EF7392</td><td>0x31</td></tr></table>"
                    ),
                    doc_id="adcu1301_parameter",
                    source_path=str(parameter_path),
                    page_idx=12,
                    metadata={
                        "module": "ecu_parameter",
                        "ecu": "ADCU1301",
                        "block_role": "section_text",
                        "section_title": "4.2 DTC Clear",
                        "line_start": "1905",
                        "line_end": "1925",
                        "page_image": "parameter_pages/step_adcu1301_page_013.png",
                        "reference_text": "EOL process-DTC Clear Type7(Single DTC NRC Allowed)",
                    },
                ),
                EvidenceUnit(
                    evidence_id="conflict_adcu9_dtc_clear",
                    evidence_type="text",
                    content="ADCU1301 DTC Clear Type7 (ADCU9-6V1L) Refer to the EOL process-DTC Clear Type7.",
                    doc_id="adcu1301_parameter",
                    source_path=str(parameter_path),
                    page_idx=13,
                    metadata={
                        "module": "ecu_parameter",
                        "ecu": "ADCU1301",
                        "block_role": "section_text",
                        "section_title": "DTC Clear Type7 (ADCU9-6V1L)",
                    },
                ),
                EvidenceUnit(
                    evidence_id="process_dtc_clear_type7",
                    evidence_type="flowchart",
                    content=(
                        "# 2.3.1.3.5.7 DTC Clear Type7(Single DTC NRC Allowed)\n"
                        "Req:14 xx xx xx\nRes:54\nretry <= 3\n![](images/dtc_clear_type7.jpg)"
                    ),
                    doc_id="eol_process",
                    source_path=str(process_path),
                    page_idx=94,
                    metadata={
                        "module": "eol_process",
                        "block_role": "flowchart_title",
                        "section_title": "DTC Clear Type7(Single DTC NRC Allowed)",
                        "img_path": "images/dtc_clear_type7.jpg",
                    },
                ),
            ]

            response = build_step_evidence_bundles(
                BuildStepEvidenceRequest(
                    flow_plan=_one_node_flow_plan(),
                    evidence_units=evidence_units,
                    top_k_per_node=5,
                )
            )

        bundle = response.bundles[0].node_bundles[0]
        trace = bundle.retrieval_trace
        query_types = {item["query_type"] for item in trace["query_plan"]["queries"]}
        self.assertTrue({"anchor", "ecu_doc", "public_process", "template"}.issubset(query_types))

        resolved_ids = {
            evidence_id
            for hop in trace["reference_hops"]
            for evidence_id in hop.get("resolved_evidence_ids", [])
        }
        self.assertIn("process_dtc_clear_type7", resolved_ids)
        self.assertIn("process_dtc_clear_type7", [candidate.evidence.evidence_id for candidate in bundle.candidates])

        decisions = {item["evidence_id"]: item for item in trace["filter_decisions"]}
        self.assertEqual(decisions["conflict_adcu9_dtc_clear"]["decision"], "demote")
        self.assertEqual(decisions["conflict_adcu9_dtc_clear"]["reason"], "local_ecu_context_conflict")
        self.assertEqual(decisions["process_dtc_clear_type7"]["reason"], "public_process_keep")

        report = build_evidence_chain_report(
            flow_plan=_one_node_flow_plan(),
            evidence_response=response,
            content_chars=4000,
        )
        markdown = render_evidence_chain_markdown(report)
        self.assertIn("## 第一步", markdown)
        self.assertIn("### ADCU1301_DTC_Clear (ADCU1301_DTC_Clear_Type7)", markdown)
        self.assertIn("参数来源 Markdown", markdown)
        self.assertIn("Refer 信息", markdown)
        self.assertIn("最终流程标题", markdown)
        self.assertIn("DTC Clear Type7", markdown)
        self.assertIn("EF7292", markdown)
        self.assertIn("EF7392", markdown)

    def test_filter_and_parent_context_are_generic(self) -> None:
        parent = EvidenceUnit(
            evidence_id="param_section",
            evidence_type="text",
            content="Complete section with full table text. EF7292 EF7392",
            doc_id="doc",
            page_idx=0,
            metadata={"block_role": "section_text", "module": "ecu_parameter", "ecu": "ADCU1301"},
        )
        row = EvidenceUnit(
            evidence_id="param_row",
            evidence_type="table",
            content="DTC Clear row EF7292",
            doc_id="doc",
            page_idx=0,
            metadata={
                "block_role": "table_row",
                "parent_evidence_id": "param_section",
                "module": "ecu_parameter",
                "ecu": "ADCU1301",
            },
        )
        sibling = EvidenceUnit(
            evidence_id="param_row_2",
            evidence_type="table",
            content="DTC Clear row EF7392",
            doc_id="doc",
            page_idx=0,
            metadata={
                "block_role": "table_row",
                "parent_evidence_id": "param_section",
                "module": "ecu_parameter",
                "ecu": "ADCU1301",
                "row_index": 2,
            },
        )
        candidate = TaskEvidenceCandidate(evidence=row, score=10.0, block_role="table_row")
        with_parent = include_parent_context(
            [candidate],
            {"param_section": parent, "param_row": row, "param_row_2": sibling},
        )
        context_ids = {item.evidence.evidence_id for item in with_parent}
        self.assertIn("param_section", context_ids)
        self.assertIn("param_row_2", context_ids)

        conflicting = TaskEvidenceCandidate(
            evidence=EvidenceUnit(
                evidence_id="other_ecu",
                evidence_type="text",
                content="DTC Clear Type7",
                doc_id="doc",
                page_idx=0,
                metadata={"block_role": "section_text", "module": "ecu_parameter", "ecu": "ADCU9-6V1L"},
            ),
            score=8.0,
            block_role="section_text",
        )
        kept, decisions = filter_evidence_context(
            node=FlowNode(
                raw="ADCU1301_DTC_Clear(ADCU1301_DTC_Clear_Type7)",
                name="ADCU1301_DTC_Clear",
                template_name="ADCU1301_DTC_Clear_Type7",
                sheet="Sheet1",
                row=1,
                column=1,
            ),
            candidates=[candidate, conflicting],
        )
        self.assertNotIn("other_ecu", {item.evidence.evidence_id for item in kept})
        self.assertEqual(
            {item.evidence_id: item.reason for item in decisions}["other_ecu"],
            "explicit_ecu_conflict",
        )


def _one_node_flow_plan() -> FlowStepPlan:
    return FlowStepPlan(
        source_path="flow.xlsx",
        steps=[
            FlowStep(
                step_key="step_001",
                display_name="DTC Clear",
                order=1,
                column=1,
                parallel_nodes=[
                    FlowNode(
                        raw="ADCU1301_DTC_Clear(ADCU1301_DTC_Clear_Type7)",
                        name="ADCU1301_DTC_Clear",
                        template_name="ADCU1301_DTC_Clear_Type7",
                        sheet="Sheet1",
                        row=1,
                        column=1,
                    )
                ],
            )
        ],
    )


if __name__ == "__main__":
    unittest.main()
