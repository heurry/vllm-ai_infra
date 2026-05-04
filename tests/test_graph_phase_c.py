"""Tests for Phase C lightweight GraphRAG scaffold."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.graph.diagnostic_graph import build_diagnostic_graph, search_graph_paths
from diagnostic_platform.normalizer.evidence import evidence_from_mineru
from diagnostic_platform.schemas import (
    BuildDiagnosticGraphRequest,
    BuildStepEvidenceRequest,
    DocumentMetadata,
    FlowNode,
    FlowStep,
    FlowStepPlan,
    GraphPathSearchRequest,
    MinerUContentBlock,
    NormalizeMinerURequest,
)
from diagnostic_platform.tracing.evidence_bundle import build_step_evidence_bundles


class GraphPhaseCTest(unittest.TestCase):
    def test_build_graph_and_search_paths(self) -> None:
        flow_plan = FlowStepPlan(
            source_path="flow.xlsx",
            steps=[
                FlowStep(
                    step_key="step_001",
                    display_name="第一步",
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
                    display_name="第二步",
                    order=2,
                    column=2,
                    parallel_nodes=[
                        FlowNode(
                            raw="DTC_Read_1(DTC_Read_Type1)",
                            name="DTC_Read_1",
                            template_name="DTC_Read_Type1",
                            sheet="Sheet1",
                            row=1,
                            column=2,
                        )
                    ],
                ),
            ],
        )
        evidence_units = evidence_from_mineru(
            NormalizeMinerURequest(
                metadata=DocumentMetadata(doc_id="doc_001", ecu="ZCUD1D01"),
                blocks=[
                    MinerUContentBlock(
                        type="text",
                        page_idx=0,
                        text="Car Mode Change DID D134 extended session Security Access level 3 request 2F.",
                    )
                ],
            )
        )
        evidence_response = build_step_evidence_bundles(
            BuildStepEvidenceRequest(flow_plan=flow_plan, evidence_units=evidence_units)
        )

        graph = build_diagnostic_graph(
            BuildDiagnosticGraphRequest(
                flow_plan=flow_plan,
                evidence_response=evidence_response,
                evidence_units=evidence_units,
            )
        )

        entity_ids = {entity.entity_id for entity in graph.entities}
        relations = {(rel.source_id, rel.relation_type, rel.target_id) for rel in graph.relations}
        self.assertIn("FlowTask:Car_Mode_Change_1", entity_ids)
        self.assertIn("FlowNode:Car_Mode_Change_1", entity_ids)
        self.assertIn("XmlTemplate:GEEA30_VMM_Change", entity_ids)
        self.assertIn("TemplateClass:GEEA30_VMM_Change", entity_ids)
        self.assertIn("ParameterField:DID=D134", entity_ids)
        self.assertIn(
            ("FlowTask:Car_Mode_Change_1", "uses_template", "TemplateClass:GEEA30_VMM_Change"),
            relations,
        )
        self.assertIn(
            ("FlowTask:Car_Mode_Change_1", "supported_by", "Section:doc_001_ev_0001"),
            relations,
        )
        self.assertIn(
            ("Section:doc_001_ev_0001", "section_has_field", "ParameterField:DID=D134"),
            relations,
        )
        self.assertIn(("FlowTask:Car_Mode_Change_1", "next_step", "FlowTask:DTC_Read_1"), relations)

        paths = search_graph_paths(
            GraphPathSearchRequest(
                graph=graph,
                source_id="FlowTask:Car_Mode_Change_1",
                target_id="ParameterField:DID=D134",
                max_depth=2,
            )
        )
        self.assertTrue(paths.paths)
        self.assertEqual(paths.paths[0].entity_ids[-1], "ParameterField:DID=D134")


if __name__ == "__main__":
    unittest.main()
