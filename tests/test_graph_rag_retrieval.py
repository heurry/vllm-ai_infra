"""Tests for deterministic Graph RAG retrieval over Markdown sections and tables."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.generation.xml_plan import generate_xml_plan
from diagnostic_platform.retrieval.graph_rag import extract_parameter_fields, load_markdown_graph_evidence_units
from diagnostic_platform.schemas import (
    BuildStepEvidenceRequest,
    EvidenceUnit,
    FlowNode,
    FlowStep,
    FlowStepPlan,
    XmlPlanGenerateRequest,
)
from diagnostic_platform.tracing.evidence_bundle import build_step_evidence_bundles


class GraphRagRetrievalTest(unittest.TestCase):
    def test_markdown_table_rows_support_alias_and_parameter_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "GEEA3.0_EOL_TREG_ZCUD1D01_Parameter-1757416047.md").write_text(
                """
# 5 Change VMM
# 5.1.1Usage Mode Change
Refer to the EOL Process-IO Control Type21(Level 3 No Return Control)
<table><tr><td>DESCRIPTION</td><td>DID</td><td>REQUEST PARAMETER</td><td>DID FOR IO CONTROL RESULT CHECK</td><td>EXPECTED VALUE</td></tr><tr><td>Usage Mode</td><td>DD0A</td><td>0x00:Abandoned
0x01:Inactive
0x0B:Active
0x0D:Driving</td><td>DD0A</td><td>22DD0A's Respond ==Request parameter</td></tr></table>
# 5.1.2 Car Mode Change
Refer to the EOL Process-IO Control Type21(Level 3 No Return Control)
<table><tr><td>DESCRIPTION</td><td>DID</td><td>REQUEST PARAMETER</td><td>DID FOR IO CONTROL RESULT CHECK</td><td>EXPECTED VALUE</td></tr><tr><td>Car Mode</td><td>D134</td><td>0x00:Normal
0x05:Dynamometer</td><td>D134</td><td>22D134's Respond ==Request parameter</td></tr></table>
""",
                encoding="utf-8",
            )
            (root / "GEEA3.0_EOL_TREG_TCAM1101_Parameter-1748656980.md").write_text(
                """
# 4.2 Secret Key for Immobilizer Target Write
Write DID List:
<table><tr><td>DESCRIPTION</td><td>DID</td><td>DATA TYPE</td><td>DATA LENGTH</td><td>SOURCE</td><td>ALLOWED NRC</td></tr><tr><td>Remote Vehicle Immobilization Secret Key</td><td>408F</td><td>Hex</td><td>16Bytes</td><td>TCA008</td><td>NA</td></tr></table>
""",
                encoding="utf-8",
            )

            evidence_units = [
                *load_markdown_graph_evidence_units(root),
                EvidenceUnit(
                    evidence_id="flowchart_body",
                    evidence_type="flowchart",
                    content="Req:10 03 Res:50 03 Positive Response Negative Response Failed Timeout DID FFFF",
                    doc_id="flowchart",
                    page_idx=0,
                    metadata={"block_role": "flowchart_body"},
                ),
            ]
            response = build_step_evidence_bundles(
                BuildStepEvidenceRequest(flow_plan=_flow_plan(), evidence_units=evidence_units, top_k_per_node=3)
            )
            plan = generate_xml_plan(XmlPlanGenerateRequest(evidence_response=response))

        by_node = {node.node_name: {arg.name: arg for arg in node.script.args} for node in plan.nodes}
        self.assertEqual(by_node["Car_Mode_Change_1"]["DID"].value, "0xD134")
        self.assertEqual(by_node["Car_Mode_Change_1"]["RequestParameter"].value, "0x00")
        self.assertEqual(by_node["Usage_Mode_Change_1"]["DID"].value, "0xDD0A")
        self.assertEqual(by_node["Usage_Mode_Change_1"]["RequestParameter"].value, "0x0B")
        self.assertEqual(by_node["TCAM_IMMO_Secret_Key_Write"]["DID"].value, "0x408F")
        self.assertEqual(by_node["TCAM_IMMO_Secret_Key_Write"]["Source"].value, "TCA008")

        all_candidate_ids = [
            candidate.evidence.evidence_id
            for step in response.bundles
            for bundle in step.node_bundles
            for candidate in bundle.candidates
        ]
        self.assertNotIn("flowchart_body", all_candidate_ids)

    def test_flowchart_ocr_tables_are_not_expanded_as_parameter_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "EOL_TREG_EOL_Process-Test_Sequence-1751025478.md").write_text(
                """
# 2.3.1.3.9.11 IO Control Type21(Level3 No Return Control)
(1):
<table><tr><td>IO Control
No / Negative Response
Req:10 03
Res:50 03
Req:2F D1 34 03 00
Res:6F D1 34 03 00
Positive Response
Fail
Pass</td></tr></table>
![](images/io_type21.jpg)
""",
                encoding="utf-8",
            )

            evidence_units = load_markdown_graph_evidence_units(root)

        flowchart_units = [unit for unit in evidence_units if unit.metadata.get("block_role") == "flowchart_title"]
        self.assertEqual(len(flowchart_units), 1)
        self.assertIn("IO Control Type21", flowchart_units[0].content)
        self.assertIn("![](images/io_type21.jpg)", flowchart_units[0].content)
        self.assertNotIn("Req:2F", flowchart_units[0].content)
        self.assertFalse([unit for unit in evidence_units if unit.metadata.get("block_role") == "table_row"])

    def test_reference_text_is_preserved_when_strict_flowchart_title_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "GEEA3.0_EOL_TREG_ECM1610_Parameter-1.md").write_text(
                """
# 4 Request Open HV Battery
Detail can refer to ecu Routine ID List.
<table><tr><td>DESCRIPTION</td><td>RID</td><td>REQUEST PARAMETER</td></tr>
<tr><td>Request BECM to Open HV Battery</td><td>0208</td><td>NA</td></tr></table>
""",
                encoding="utf-8",
            )

            evidence_units = load_markdown_graph_evidence_units(root)

        table_rows = [unit for unit in evidence_units if unit.metadata.get("block_role") == "table_row"]
        self.assertEqual(len(table_rows), 1)
        fields = extract_parameter_fields(table_rows[0])
        self.assertEqual(fields["RID"], "0208")
        self.assertEqual(fields["REFERENCE_TEXT"], "ecu Routine ID List")


def _flow_plan() -> FlowStepPlan:
    return FlowStepPlan(
        source_path="flow.xlsx",
        steps=[
            FlowStep(
                step_key="step_001",
                display_name="Car mode",
                order=1,
                column=1,
                parallel_nodes=[
                    FlowNode(
                        raw="Car_Mode_Change_1(GEEA30_VMM_Change)",
                        name="Car_Mode_Change_1",
                        template_name="GEEA30_VMM_Change",
                        sheet="Sheet1",
                        row=1,
                        column=1,
                    )
                ],
            ),
            FlowStep(
                step_key="step_002",
                display_name="Usage mode",
                order=2,
                column=2,
                parallel_nodes=[
                    FlowNode(
                        raw="Usage_Mode_Change_1(GEEA30_VMM_Change)",
                        name="Usage_Mode_Change_1",
                        template_name="GEEA30_VMM_Change",
                        sheet="Sheet1",
                        row=1,
                        column=2,
                    )
                ],
            ),
            FlowStep(
                step_key="step_003",
                display_name="TCAM IMMO",
                order=3,
                column=3,
                parallel_nodes=[
                    FlowNode(
                        raw="TCAM_IMMO_Secret_Key_Write(Information_Write_Type3)",
                        name="TCAM_IMMO_Secret_Key_Write",
                        template_name="Information_Write_Type3",
                        sheet="Sheet1",
                        row=1,
                        column=3,
                    )
                ],
            ),
        ],
    )


if __name__ == "__main__":
    unittest.main()
