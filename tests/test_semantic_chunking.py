"""Tests for semantic chunking over MinerU outputs."""

from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.indexing.local_builder import build_local_index
from diagnostic_platform.normalizer.semantic_chunk import (
    evidence_units_from_semantic_chunks,
    knowledge_units_from_semantic_chunks,
    semantic_chunks_from_mineru,
)
from diagnostic_platform.schemas import (
    DocumentMetadata,
    LocalIndexBuildRequest,
    MinerUContentBlock,
    NormalizeMinerURequest,
)


class SemanticChunkingTest(unittest.TestCase):
    def _request(self) -> NormalizeMinerURequest:
        return NormalizeMinerURequest(
            metadata=DocumentMetadata(
                doc_id="GEEA3.0_EOL_TREG_ZCUD1D01_Parameter-1757416047",
                doc_type="pdf_protocol",
                protocol="UDS",
                module="ecu_parameter",
                ecu="ZCUD1D01",
                source="mineru",
                source_path="/tmp/ZCUD_content_list.json",
            ),
            blocks=[
                MinerUContentBlock(
                    type="text",
                    page_idx=3,
                    text="2.3.1 Change VMM",
                    bbox=[10, 10, 100, 20],
                ),
                MinerUContentBlock(
                    type="text",
                    page_idx=3,
                    text="DID D134 request parameter 0x00",
                    bbox=[10, 24, 160, 34],
                ),
                MinerUContentBlock(
                    type="table",
                    page_idx=4,
                    bbox=[20, 30, 300, 120],
                    img_path="images/table.jpg",
                    table_body=(
                        "<table><tr><th>DID</th><th>REQUEST PARAMETER</th></tr>"
                        "<tr><td>0x3101</td><td>0x00</td></tr></table>"
                    ),
                ),
            ],
        )

    def test_text_blocks_merge_and_table_rows_keep_provenance(self) -> None:
        chunks = semantic_chunks_from_mineru(self._request())

        paragraph = next(chunk for chunk in chunks if chunk.chunk_type == "paragraph")
        table_row = next(chunk for chunk in chunks if chunk.chunk_type == "table_row")

        self.assertIn("Change VMM", paragraph.content)
        self.assertIn("DID D134", paragraph.content)
        self.assertEqual(paragraph.source_block_ids, ["GEEA3.0_EOL_TREG_ZCUD1D01_Parameter-1757416047_block_0001", "GEEA3.0_EOL_TREG_ZCUD1D01_Parameter-1757416047_block_0002"])
        self.assertEqual(paragraph.bbox, [10, 10, 160, 34])
        self.assertEqual(table_row.table_fields["DID"], "0x3101")
        self.assertEqual(table_row.table_fields["REQUEST PARAMETER"], "0x00")
        self.assertEqual(table_row.parent_chunk_id, "GEEA3.0_EOL_TREG_ZCUD1D01_Parameter-1757416047_sem_0002")
        self.assertEqual(table_row.bbox, [20, 30, 300, 120])

    def test_semantic_chunks_convert_to_knowledge_and_evidence_units(self) -> None:
        chunks = semantic_chunks_from_mineru(self._request())
        knowledge_units = knowledge_units_from_semantic_chunks(chunks)
        evidence_units = evidence_units_from_semantic_chunks(chunks)

        self.assertTrue(any(unit.unit_type == "text_chunk" and "D134" in unit.dids for unit in knowledge_units))
        table_evidence = next(unit for unit in evidence_units if unit.metadata.get("block_role") == "table_row")
        self.assertEqual(table_evidence.metadata["source_block_ids"], ["GEEA3.0_EOL_TREG_ZCUD1D01_Parameter-1757416047_block_0003"])
        self.assertEqual(table_evidence.metadata["bbox_union"], [20, 30, 300, 120])
        self.assertEqual(table_evidence.metadata["table_fields"]["DID"], "0x3101")

    def test_build_local_index_semantic_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mineru_root = root / "mineru"
            index_dir = root / "index"
            doc_dir = mineru_root / "GEEA3.0_EOL_TREG_ZCUD1D01_Parameter-1757416047"
            doc_dir.mkdir(parents=True)
            (doc_dir / "GEEA3.0_EOL_TREG_ZCUD1D01_Parameter-1757416047_content_list.json").write_text(
                json.dumps(
                    [
                        {"type": "text", "page_idx": 1, "text": "2.3.1 Change VMM", "bbox": [1, 1, 20, 10]},
                        {"type": "text", "page_idx": 1, "text": "DID D134 Request Parameter 0x00", "bbox": [1, 12, 90, 20]},
                    ]
                ),
                encoding="utf-8",
            )
            (doc_dir / "GEEA3.0_EOL_TREG_ZCUD1D01_Parameter-1757416047.md").write_text(
                "# Change VMM\n\n<table><tr><th>DID</th><th>REQUEST PARAMETER</th></tr><tr><td>0xD134</td><td>0x00</td></tr></table>\n",
                encoding="utf-8",
            )

            result = build_local_index(
                LocalIndexBuildRequest(
                    mineru_output_dir=str(mineru_root),
                    index_dir=str(index_dir),
                    collection_name="semantic_unit_test",
                    chunking_mode="semantic",
                )
            )

            self.assertEqual(result.summary["chunking_mode"], "semantic")
            self.assertGreater(int(result.summary["semantic_chunk_count"]), 0)
            knowledge_lines = (index_dir / "knowledge_units.jsonl").read_text(encoding="utf-8").splitlines()
            evidence_text = (index_dir / "evidence_units.jsonl").read_text(encoding="utf-8")
            self.assertTrue(any("D134" in line and "Change VMM" in line for line in knowledge_lines))
            self.assertIn("source_block_ids", evidence_text)
            self.assertIn("table_fields", evidence_text)


if __name__ == "__main__":
    unittest.main()
