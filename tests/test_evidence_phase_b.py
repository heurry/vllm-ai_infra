"""Tests for Phase B evidence construction."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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


class EvidencePhaseBTest(unittest.TestCase):
    def test_evidence_from_mineru_blocks(self) -> None:
        request = NormalizeMinerURequest(
            metadata=DocumentMetadata(
                doc_id="treg_zcud_v1",
                protocol="UDS",
                module="io_control",
                ecu="ZCUD1D01",
                source_path="data/raw/treg_zcud.pdf",
            ),
            blocks=[
                MinerUContentBlock(
                    type="text",
                    page_idx=12,
                    bbox=[1, 2, 3, 4],
                    text="Car Mode Change DID D134 Request Parameter 0x00 refer to EOL Process IO Control Type21.",
                ),
                MinerUContentBlock(
                    type="image",
                    page_idx=95,
                    bbox=[5, 6, 7, 8],
                    image_caption=["IO Control Type21 flow sequence"],
                    img_path="images/page_095_img_002.jpg",
                ),
            ],
        )

        evidence = evidence_from_mineru(request)

        self.assertEqual(len(evidence), 2)
        self.assertEqual(evidence[0].evidence_type, "text")
        self.assertEqual(evidence[1].evidence_type, "flowchart")
        self.assertIn("D134", evidence[0].metadata["dids"])

    def test_build_step_evidence_bundle(self) -> None:
        evidence = evidence_from_mineru(
            NormalizeMinerURequest(
                metadata=DocumentMetadata(doc_id="doc_001", ecu="ZCUD1D01"),
                blocks=[
                    MinerUContentBlock(
                        type="text",
                        page_idx=0,
                        text="Car Mode Change uses DID D134 and Request Parameter 0x00.",
                    ),
                    MinerUContentBlock(
                        type="text",
                        page_idx=1,
                        text="DTC Read Type1 reads snapshot records.",
                    ),
                ],
            )
        )
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

        response = build_step_evidence_bundles(
            BuildStepEvidenceRequest(flow_plan=flow_plan, evidence_units=evidence, top_k_per_node=1)
        )

        node_bundle = response.bundles[0].node_bundles[0]
        self.assertEqual(node_bundle.node_name, "Car_Mode_Change_1")
        self.assertEqual(len(node_bundle.matches), 1)
        self.assertIn("car mode change", node_bundle.matches[0].matched_terms)


if __name__ == "__main__":
    unittest.main()
