"""Tests for graph-aware XML argument value selection."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.generation.xml_plan import generate_xml_plan
from diagnostic_platform.schemas import (
    BuildStepEvidenceResponse,
    EvidenceUnit,
    NodeEvidenceBundle,
    StepEvidenceBundle,
    TaskEvidenceCandidate,
    XmlPlanGenerateRequest,
)


class GraphAwareXmlSelectionTest(unittest.TestCase):
    def test_graph_candidate_parameter_fields_drive_xml_args(self) -> None:
        evidence_high = EvidenceUnit(
            evidence_id="ev_high",
            evidence_type="text",
            content="DID 1111 candidate",
            doc_id="doc",
            page_idx=0,
            metadata={"block_role": "section_text"},
        )
        evidence_graph = EvidenceUnit(
            evidence_id="ev_graph",
            evidence_type="table",
            content="Secret Key for Immobilizer Target Write DID 408F SOURCE TCA008",
            doc_id="doc",
            page_idx=1,
            metadata={"block_role": "table_row"},
        )
        response = BuildStepEvidenceResponse(
            source_flow_path="flow.xlsx",
            bundles=[
                StepEvidenceBundle(
                    step_key="step_001",
                    display_name="第一步",
                    order=1,
                    row=1,
                    column=1,
                    node_bundles=[
                        NodeEvidenceBundle(
                            step_key="step_001",
                            node_name="Demo_Node",
                            template_name="Demo_Template",
                            row=1,
                            column=1,
                            candidates=[
                                TaskEvidenceCandidate(
                                    evidence=evidence_high,
                                    score=10.0,
                                    matched_terms=["demo"],
                                    graph_paths=["FlowTask:Demo_Node --supported_by--> Section:ev_high"],
                                    block_role="section_text",
                                    parameter_fields={"DID": "1111"},
                                ),
                                TaskEvidenceCandidate(
                                    evidence=evidence_graph,
                                    score=16.0,
                                    matched_terms=["secret key immobilizer"],
                                    graph_paths=[
                                        "FlowTask:Demo_Node --supported_by--> TableRow:ev_graph --row_has_field--> ParameterField:DID=408F"
                                    ],
                                    block_role="table_row",
                                    parameter_fields={
                                        "DESCRIPTION": "Secret Key for Immobilizer Target Write",
                                        "DID": "408F",
                                        "SOURCE": "TCA008",
                                    },
                                ),
                            ],
                        )
                    ],
                )
            ],
        )

        plan = generate_xml_plan(XmlPlanGenerateRequest(evidence_response=response))
        args = {arg.name: arg for arg in plan.serial.scripts[0].args}

        self.assertEqual(args["DID"].value, "0x408F")
        self.assertEqual(args["Source"].value, "TCA008")
        self.assertTrue(args["DID"].graph_paths)

    def test_dtc_read_does_not_absorb_unrelated_ecu_parameter_fields(self) -> None:
        mismatched_parameter = EvidenceUnit(
            evidence_id="ev_asdm",
            evidence_type="table",
            content="ASDM1401 SecOC key status DID D0CB",
            doc_id="asdm",
            page_idx=1,
            metadata={"block_role": "table_row", "ecu": "ASDM1401", "module": "ecu_parameter"},
        )
        dtc_flowchart = EvidenceUnit(
            evidence_id="ev_dtc_flow",
            evidence_type="flowchart",
            content="# DTC Read Type1(Read Snapshot)",
            doc_id="process",
            page_idx=2,
            metadata={"block_role": "flowchart_title", "section_title": "DTC Read Type1(Read Snapshot)"},
        )
        response = BuildStepEvidenceResponse(
            source_flow_path="flow.xlsx",
            bundles=[
                StepEvidenceBundle(
                    step_key="step_001",
                    display_name="DTC read",
                    order=1,
                    row=1,
                    column=1,
                    node_bundles=[
                        NodeEvidenceBundle(
                            step_key="step_001",
                            node_name="CMD1A1C_DTC_Read",
                            template_name="DTC_Read_Type1",
                            row=1,
                            column=1,
                            candidates=[
                                TaskEvidenceCandidate(
                                    evidence=mismatched_parameter,
                                    score=90.0,
                                    matched_terms=["dtc read type1"],
                                    block_role="table_row",
                                    parameter_fields={"DID": "D0CB", "DESCRIPTION": "SecOC key status"},
                                ),
                                TaskEvidenceCandidate(
                                    evidence=dtc_flowchart,
                                    score=40.0,
                                    matched_terms=["dtc read type1"],
                                    block_role="flowchart_title",
                                    parameter_fields={"FLOWCHART_TITLE": "DTC Read Type1(Read Snapshot)"},
                                ),
                            ],
                        )
                    ],
                )
            ],
        )

        plan = generate_xml_plan(XmlPlanGenerateRequest(evidence_response=response))
        args = {arg.name: arg for arg in plan.serial.scripts[0].args}

        self.assertEqual(args["EcuBOMName"].value, "CMD1A1C")
        self.assertEqual(args["DID"].value, "0x3101")
        self.assertEqual(args["FlowchartTitle"].value, "DTC Read Type1(Read Snapshot)")
        self.assertNotIn("Description", args)

    def test_xml_plan_ignores_nonnumeric_score_breakdown_entries(self) -> None:
        evidence = EvidenceUnit(
            evidence_id="ev_hybrid",
            evidence_type="table",
            content="ZCUD1D01 Car Mode Change DID D134 Request Parameter 0x00",
            doc_id="doc",
            page_idx=1,
            metadata={"block_role": "table_row", "ecu": "ZCUD1D01"},
        )
        candidate = TaskEvidenceCandidate.model_construct(
            evidence=evidence,
            score=20.0,
            matched_terms=["car mode change"],
            graph_paths=["FlowTask:Car_Mode --supported_by--> TableRow:ev_hybrid"],
            kg_candidate_ids=[],
            kg_paths=[],
            score_breakdown={"graph_score": 20.0, "retrieval_source": "hybrid"},
            provenance_refs=[],
            raw_source_path="doc.md",
            needs_review=[],
            block_role="table_row",
            parameter_fields={"DID": "D134", "REQUEST_PARAMETER": "0x00"},
            flowchart_title="",
            template_match="",
            retrieval_source="hybrid",
            retrieval_rank=1,
            retrieval_score=0.9,
            retrieval_metadata={},
            notes=[],
        )
        response = BuildStepEvidenceResponse(
            source_flow_path="flow.xlsx",
            bundles=[
                StepEvidenceBundle(
                    step_key="step_001",
                    display_name="第一步",
                    order=1,
                    row=1,
                    column=1,
                    node_bundles=[
                        NodeEvidenceBundle(
                            step_key="step_001",
                            node_name="ZCUD1D01_Car_Mode_Change",
                            template_name="GEEA30_VMM_Change",
                            row=1,
                            column=1,
                            candidates=[candidate],
                        )
                    ],
                )
            ],
        )

        plan = generate_xml_plan(XmlPlanGenerateRequest(evidence_response=response))
        did = {arg.name: arg for arg in plan.serial.scripts[0].args}["DID"]

        self.assertEqual(did.value, "0xD134")
        self.assertEqual(did.score_breakdown["graph_score"], 20.0)
        self.assertNotIn("retrieval_source", did.score_breakdown)


if __name__ == "__main__":
    unittest.main()
