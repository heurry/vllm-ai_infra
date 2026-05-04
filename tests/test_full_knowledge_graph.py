"""Tests for the local full knowledge graph snapshot and repository."""

from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.graph.full_kg import (  # noqa: E402
    KG_EDGES_FILE,
    KG_MANIFEST_FILE,
    KG_NODES_FILE,
    KG_PROVENANCE_FILE,
    LocalGraphRepository,
    build_full_knowledge_graph,
    load_knowledge_graph_snapshot,
    write_knowledge_graph_snapshot,
)
from diagnostic_platform.retrieval.graph_rag import load_markdown_graph_evidence_units  # noqa: E402
from diagnostic_platform.schemas import EvidenceUnit, FlowNode  # noqa: E402


class FullKnowledgeGraphTest(unittest.TestCase):
    def test_snapshot_query_reference_and_no_batch_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            markdown_root = root / "markdown"
            markdown_root.mkdir()
            (markdown_root / "GEEA3.0_EOL_TREG_ZCUD1D01_Parameter-1757416047.md").write_text(
                """
# 5 Change VMM
## 5.1 VMM Change Parameters
Refer to the EOL Process-IO Control Type21(Level 3 No Return Control)
<table><tr><td>DESCRIPTION</td><td>DID</td><td>REQUEST PARAMETER</td></tr>
<tr><td>VMM Change Usage Mode</td><td>DD0A</td><td>0x0B:Active</td></tr>
<tr><td>VMM Change Car Mode</td><td>D134</td><td>0x00:Normal</td></tr></table>
""",
                encoding="utf-8",
            )
            (markdown_root / "GEEA3.0_EOL_TREG_General_EOL_Test.md").write_text(
                """
# EOL Process-IO Control Type21(Level 3 No Return Control)
Change VMM operations refer to IO Control Type21(Level 3 No Return Control).
""",
                encoding="utf-8",
            )

            evidence_units = [
                *load_markdown_graph_evidence_units(markdown_root),
                EvidenceUnit(
                    evidence_id="flowchart_img_1",
                    evidence_type="flowchart",
                    content="IO Control Type21 Level 3 No Return Control flowchart image",
                    doc_id="GEEA3.0_EOL_TREG_General_EOL_Test",
                    source_path=str(markdown_root / "GEEA3.0_EOL_TREG_General_EOL_Test.md"),
                    page_idx=0,
                    metadata={
                        "block_role": "flowchart_image",
                        "img_path": "images/io_control_type21.png",
                        "module": "general_eol_test",
                    },
                ),
            ]

            snapshot = build_full_knowledge_graph(evidence_units, collection_name="unit_test", source=str(markdown_root))
            files = write_knowledge_graph_snapshot(snapshot, root / "kg")
            loaded = load_knowledge_graph_snapshot(root / "kg")
            repo = LocalGraphRepository(snapshot=loaded, evidence_units=evidence_units)

            self.assertTrue((root / "kg" / KG_MANIFEST_FILE).exists())
            self.assertTrue((root / "kg" / KG_NODES_FILE).exists())
            self.assertTrue((root / "kg" / KG_EDGES_FILE).exists())
            self.assertTrue((root / "kg" / KG_PROVENANCE_FILE).exists())
            self.assertEqual(files["kg_manifest"], str(root / "kg" / KG_MANIFEST_FILE))
            self.assertGreater(len(loaded.nodes), 0)
            self.assertGreater(len(loaded.edges), 0)
            self.assertEqual(loaded.manifest.schema_version, "kg.v1")

            node = FlowNode(
                raw="VMM_Change_1(GEEA30_VMM_Change)",
                name="VMM_Change_1",
                template_name="GEEA30_VMM_Change",
                sheet="Sheet1",
                row=1,
                column=1,
            )
            candidates = repo.query_task_candidates(node=node, top_k=5)

            self.assertGreaterEqual(len(candidates), 2)
            self.assertEqual(candidates[0].evidence.metadata.get("module"), "ecu_parameter")
            self.assertTrue(candidates[0].kg_candidate_ids)
            self.assertTrue(candidates[0].kg_paths)
            self.assertTrue(candidates[0].provenance_refs)
            self.assertTrue(any("ambiguous_table_rows_not_expanded" in candidate.needs_review for candidate in candidates))

            resolutions = repo.resolve_references("IO Control Type 21 Level 3 No Return Control")
            self.assertTrue(resolutions)
            self.assertTrue(resolutions[0].resolved_node_ids)
            self.assertNotIn("reference_not_resolved", resolutions[0].needs_review)
            self.assertTrue(any("backed_by_image" in path for path in resolutions[0].kg_paths))

            dumped_nodes = [
                json.loads(line)
                for line in (root / "kg" / KG_NODES_FILE).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            flowchart_nodes = [node for node in dumped_nodes if node["node_type"] == "FlowchartTitle"]
            self.assertTrue(flowchart_nodes)
            self.assertIn("control", flowchart_nodes[0]["normalized_tokens"])


if __name__ == "__main__":
    unittest.main()
