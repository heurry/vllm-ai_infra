"""Tests for attaching graph paths to evidence bundles."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.graph.evidence_paths import attach_graph_paths_to_evidence_response
from diagnostic_platform.normalizer.evidence import evidence_from_mineru
from diagnostic_platform.schemas import (
    BuildStepEvidenceRequest,
    DocumentMetadata,
    FlowNode,
    FlowStep,
    FlowStepPlan,
    MinerUContentBlock,
    NormalizeMinerURequest,
)
from diagnostic_platform.tracing.evidence_bundle import build_step_evidence_bundles


class GraphEvidencePathsTest(unittest.TestCase):
    def test_attach_graph_paths_to_node_bundles(self) -> None:
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
                )
            ],
        )
        evidence_units = evidence_from_mineru(
            NormalizeMinerURequest(
                metadata=DocumentMetadata(doc_id="doc_001", ecu="ZCUD1D01"),
                blocks=[
                    MinerUContentBlock(
                        type="text",
                        page_idx=0,
                        text="Car Mode Change DID D134 uses service 2F in extended session.",
                    )
                ],
            )
        )
        evidence_response = build_step_evidence_bundles(
            BuildStepEvidenceRequest(flow_plan=flow_plan, evidence_units=evidence_units)
        )

        enriched, graph = attach_graph_paths_to_evidence_response(
            flow_plan=flow_plan,
            evidence_response=evidence_response,
            evidence_units=evidence_units,
        )

        paths = enriched.bundles[0].node_bundles[0].graph_paths
        self.assertTrue(paths)
        self.assertTrue(any("--uses_template-->" in path for path in paths))
        self.assertTrue(any("--supported_by-->" in path for path in paths))
        self.assertGreater(len(graph.entities), 0)


if __name__ == "__main__":
    unittest.main()
