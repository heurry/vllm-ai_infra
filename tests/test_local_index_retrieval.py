"""Tests for local MinerU indexing and sparse retrieval."""

from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.indexing.local_builder import build_local_index
from diagnostic_platform.retrieval.local_sparse import query_local_index
from diagnostic_platform.schemas import LocalIndexBuildRequest, RetrievalFilters, RetrievalQueryRequest


class LocalIndexRetrievalTest(unittest.TestCase):
    def test_build_index_and_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mineru_root = root / "mineru"
            index_dir = root / "index"
            doc_dir = mineru_root / "GEEA3.0_EOL_TREG_ZCUD1D01_Parameter-1757416047"
            doc_dir.mkdir(parents=True)
            (doc_dir / "GEEA3.0_EOL_TREG_ZCUD1D01_Parameter-1757416047_content_list.json").write_text(
                json.dumps(
                    [
                        {
                            "type": "text",
                            "page_idx": 3,
                            "text": "ZCUD1D01 Change VMM uses DID D134 with 2F service in extended session level 3.",
                            "bbox": [1, 2, 3, 4],
                        },
                        {
                            "type": "table",
                            "page_idx": 4,
                            "table_body": "<table><tr><td>Battery voltage check</td></tr></table>",
                        },
                    ]
                ),
                encoding="utf-8",
            )

            build_result = build_local_index(
                LocalIndexBuildRequest(
                    mineru_output_dir=str(mineru_root),
                    index_dir=str(index_dir),
                    collection_name="unit_test",
                )
            )

            self.assertEqual(build_result.document_count, 1)
            self.assertEqual(build_result.knowledge_unit_count, 2)
            self.assertGreater(build_result.graph_entity_count, 0)
            self.assertGreater(build_result.kg_node_count, 0)
            self.assertGreater(build_result.kg_edge_count, 0)
            self.assertGreater(build_result.kg_provenance_count, 0)
            self.assertTrue((index_dir / "knowledge_units.jsonl").exists())
            self.assertTrue((index_dir / "diagnostic_graph.json").exists())
            self.assertTrue((index_dir / "kg_manifest.json").exists())
            self.assertTrue((index_dir / "kg_nodes.jsonl").exists())
            self.assertTrue((index_dir / "kg_edges.jsonl").exists())
            self.assertTrue((index_dir / "kg_provenance.jsonl").exists())

            response = query_local_index(
                RetrievalQueryRequest(
                    query="Change VMM D134",
                    index_dir=str(index_dir),
                    filters=RetrievalFilters(ecu="ZCUD1D01", dids=["D134"]),
                    top_k=1,
                )
            )

            self.assertEqual(len(response.chunks), 1)
            self.assertIn("D134", response.chunks[0].content)
            self.assertIn("chunk_id=", response.context)


if __name__ == "__main__":
    unittest.main()
