"""Tests for Phase B evidence construction."""

from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.indexing.local_builder import EVIDENCE_UNITS_FILE, KNOWLEDGE_UNITS_FILE, SUMMARY_FILE
from diagnostic_platform.indexing.vector_index import build_vector_index
from diagnostic_platform.normalizer.evidence import evidence_from_mineru
from diagnostic_platform.retrieval.embedding import HashingEmbeddingClient
from diagnostic_platform.retrieval.vector_store import InMemoryVectorStore
from diagnostic_platform.schemas import (
    BuildStepEvidenceRequest,
    DocumentMetadata,
    EvidenceUnit,
    FlowNode,
    FlowStep,
    FlowStepPlan,
    KnowledgeUnit,
    MinerUContentBlock,
    NormalizeMinerURequest,
    VectorIndexBuildRequest,
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

    def test_build_step_evidence_bundle_with_hybrid_retrieval_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            index_dir = Path(tmpdir) / "index"
            index_dir.mkdir()
            evidence = [
                EvidenceUnit(
                    evidence_id="doc1_tbl_1",
                    evidence_type="table",
                    content="Car Mode Change table row ECU ZCUD1D01 DID D134 Request Parameter 0x00.",
                    doc_id="doc1",
                    source_path="doc1.md",
                    page_idx=2,
                    metadata={
                        "block_role": "table_row",
                        "ecu": "ZCUD1D01",
                        "dids": ["D134"],
                        "service_ids": ["2F"],
                    },
                )
            ]
            knowledge = [
                KnowledgeUnit(
                    chunk_id="doc1_text_0001",
                    unit_type="text_chunk",
                    content="ZCUD1D01 Car Mode Change DID D134 uses request parameter 0x00.",
                    protocol="UDS",
                    module="body",
                    ecu="ZCUD1D01",
                    service_ids=["2F"],
                    dids=["D134"],
                    page_idx=2,
                    source_path="doc1.md",
                )
            ]
            _write_jsonl(index_dir / EVIDENCE_UNITS_FILE, evidence)
            _write_jsonl(index_dir / KNOWLEDGE_UNITS_FILE, knowledge)
            (index_dir / SUMMARY_FILE).write_text(json.dumps({"collection_name": "fixture"}), encoding="utf-8")

            embedder = HashingEmbeddingClient(dimension=16)
            store = InMemoryVectorStore(collection_name="diagnostic_knowledge_fixture")
            build_vector_index(
                VectorIndexBuildRequest(
                    index_dir=str(index_dir),
                    collection_name="diagnostic_knowledge_fixture",
                    embedding_model=embedder.model_name,
                    drop_existing=True,
                ),
                embedding_client=embedder,
                vector_store=store,
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
                BuildStepEvidenceRequest(
                    flow_plan=flow_plan,
                    evidence_units=evidence,
                    top_k_per_node=1,
                    index_dir=str(index_dir),
                    enable_dense=True,
                    dense_top_k=2,
                    hybrid_top_k=3,
                    vector_collection="diagnostic_knowledge_fixture",
                    embedding_model=embedder.model_name,
                ),
                embedding_client=embedder,
                vector_store=store,
            )

            node_bundle = response.bundles[0].node_bundles[0]
            self.assertTrue(node_bundle.retrieval_trace["dense"]["results"])
            self.assertTrue(node_bundle.retrieval_trace["hybrid"]["results"])
            self.assertTrue(any(candidate.retrieval_source in {"dense", "hybrid"} for candidate in node_bundle.candidates))
            self.assertTrue(any(match.source in {"dense", "hybrid"} for match in node_bundle.matches))
            for candidate in node_bundle.candidates:
                self.assertTrue(all(isinstance(score, (int, float)) for score in candidate.score_breakdown.values()))

def _write_jsonl(path: Path, models: list) -> None:
    with path.open("w", encoding="utf-8") as file:
        for model in models:
            file.write(json.dumps(model.model_dump(mode="json"), ensure_ascii=False) + "\n")


if __name__ == "__main__":
    unittest.main()
