"""Tests for XML plan trace raw-source and Markdown artifacts."""

from pathlib import Path
import importlib.util
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.generation.xml_plan import generate_xml_plan  # noqa: E402
from diagnostic_platform.graph.full_kg import LocalGraphRepository, build_full_knowledge_graph  # noqa: E402
from diagnostic_platform.retrieval.graph_rag import load_markdown_graph_evidence_units  # noqa: E402
from diagnostic_platform.schemas import (  # noqa: E402
    BuildStepEvidenceRequest,
    EvidenceUnit,
    FlowNode,
    FlowStep,
    FlowStepPlan,
    XmlPlanGenerateRequest,
)
from diagnostic_platform.tracing.evidence_bundle import build_step_evidence_bundles  # noqa: E402


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "generate_xml_plan.py"
SPEC = importlib.util.spec_from_file_location("generate_xml_plan_script", SCRIPT_PATH)
generate_xml_plan_script = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(generate_xml_plan_script)


class XmlPlanTraceArtifactsTest(unittest.TestCase):
    def test_trace_payload_writes_raw_sources_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            markdown_root = root / "markdown"
            markdown_root.mkdir()
            (markdown_root / "GEEA3.0_EOL_TREG_ZCUD1D01_Parameter-1757416047.md").write_text(
                """
# 5 Change VMM
Refer to the EOL Process-IO Control Type21(Level 3 No Return Control)
<table><tr><td>DESCRIPTION</td><td>DID</td><td>REQUEST PARAMETER</td></tr>
<tr><td>Usage Mode Change</td><td>DD0A</td><td>0x0B:Active</td></tr></table>
""",
                encoding="utf-8",
            )
            evidence_units = [
                *load_markdown_graph_evidence_units(markdown_root),
                EvidenceUnit(
                    evidence_id="flowchart_img_1",
                    evidence_type="flowchart",
                    content="IO Control Type21 Level 3 No Return Control flowchart image",
                    doc_id="GEEA3.0_EOL_TREG_ZCUD1D01_Parameter-1757416047",
                    page_idx=0,
                    metadata={"block_role": "flowchart_image"},
                ),
            ]
            flow_plan = FlowStepPlan(
                source_path="flow.xlsx",
                steps=[
                    FlowStep(
                        step_key="step_001",
                        display_name="Usage mode",
                        order=1,
                        column=1,
                        parallel_nodes=[
                            FlowNode(
                                raw="Usage_Mode_Change_1(GEEA30_VMM_Change)",
                                name="Usage_Mode_Change_1",
                                template_name="GEEA30_VMM_Change",
                                sheet="Sheet1",
                                row=2,
                                column=1,
                            )
                        ],
                    )
                ],
            )
            snapshot = build_full_knowledge_graph(evidence_units, collection_name="trace_test", source=str(markdown_root))
            repo = LocalGraphRepository(snapshot=snapshot, evidence_units=evidence_units)
            evidence_response = build_step_evidence_bundles(
                BuildStepEvidenceRequest(flow_plan=flow_plan, evidence_units=evidence_units),
                graph_repository=repo,
            )
            xml_plan = generate_xml_plan(XmlPlanGenerateRequest(evidence_response=evidence_response))
            trace_payload = generate_xml_plan_script._build_trace_payload(
                flow_plan=flow_plan,
                evidence_response=evidence_response,
                xml_plan=xml_plan,
                operation_xml_files=[
                    {
                        "step_key": "step_001",
                        "order": 1,
                        "row": 2,
                        "column": 1,
                        "node_name": "Usage_Mode_Change_1",
                        "template_name": "GEEA30_VMM_Change",
                        "path": str(root / "operation.xml"),
                    }
                ],
                content_chars=120,
                raw_source_dir=root / "raw_sources",
                kg_enabled=True,
                kg_manifest_path=str(root / "kg_manifest.json"),
                graph_repository=repo,
            )
            markdown = generate_xml_plan_script._render_trace_markdown(trace_payload)

            operation = trace_payload["steps"][0]["operations"][0]
            self.assertTrue(operation["raw_sources"])
            self.assertTrue(Path(operation["raw_sources"][0]["raw_source_path"]).exists())
            self.assertTrue(operation["reference_resolutions"])
            self.assertIn("Flow Evidence Trace", markdown)
            self.assertIn("Raw sources", markdown)
            self.assertIn("Reference resolutions", markdown)


if __name__ == "__main__":
    unittest.main()
