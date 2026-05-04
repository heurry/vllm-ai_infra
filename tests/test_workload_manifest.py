"""Tests for workload manifest conversion."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.orchestration.workload import (
    WorkloadManifest,
    inject_workload_manifest_path,
    workload_to_pipeline_config,
    workload_to_regression_config,
)


class WorkloadManifestTest(unittest.TestCase):
    def test_convert_workload_to_configs(self) -> None:
        manifest = WorkloadManifest(
            workload="demo",
            paths={
                "flow_path": "flow.xlsx",
                "index_dir": "index",
                "base_workflow": "base.xml",
                "template_contracts": "contracts.json",
                "xml_plan": "plan.json",
                "serial_xml": "serial.xml",
                "operation_xml_dir": "operations",
                "flow_evidence_trace": "trace.json",
                "diagnostic_graph": "graph.json",
                "fused_workflow": "fused.xml",
                "arg_audit_report": "audit.json",
                "audit_resolution_report": "resolution.json",
                "llm_resolution_report": "llm.json",
                "regression_report": "regression.json",
                "pipeline_report": "pipeline.json",
            },
            generation={"top_k_per_node": 7},
            fusion={"target_serial_name": "ROOT_ALL"},
            regression_expectations={"planned_nodes": 2},
            critical_args=[{"node_name": "N", "arg_name": "A", "value": "V"}],
        )

        regression = workload_to_regression_config(manifest)
        pipeline = inject_workload_manifest_path(
            workload_to_pipeline_config(manifest, enable_llm=True),
            Path("configs/workloads/demo.json"),
        )

        self.assertEqual(regression["expectations"]["planned_nodes"], 2)
        self.assertEqual(regression["critical_args"][0]["value"], "V")
        self.assertIn("--top-k-per-node", pipeline["steps"][0]["command"])
        self.assertIn("7", pipeline["steps"][0]["command"])
        self.assertIn("ROOT_ALL", pipeline["steps"][1]["command"])
        self.assertTrue(pipeline["steps"][4]["enabled"])
        self.assertIn("configs/workloads/demo.json", pipeline["steps"][-1]["command"])

    def test_llm_node_mode_uses_llm_plan_for_fusion_audit_and_regression(self) -> None:
        manifest = WorkloadManifest(
            workload="demo",
            paths={
                "flow_path": "flow.xlsx",
                "index_dir": "index",
                "base_workflow": "base.xml",
                "template_registry": "templates.json",
                "template_contracts": "contracts.json",
                "xml_plan": "plan.json",
                "serial_xml": "serial.xml",
                "operation_xml_dir": "operations",
                "flow_evidence_trace": "trace.json",
                "diagnostic_graph": "graph.json",
                "llm_xml_plan": "llm_plan.json",
                "llm_serial_xml": "llm_serial.xml",
                "llm_operation_xml_dir": "llm_operations",
                "llm_generation_trace": "llm_trace.json",
                "llm_raw_source_dir": "raw_sources",
                "fused_workflow": "fused.xml",
                "arg_audit_report": "audit.json",
                "audit_resolution_report": "resolution.json",
                "llm_resolution_report": "llm.json",
                "regression_report": "regression.json",
                "pipeline_report": "pipeline.json",
            },
            generation={
                "mode": "llm_node",
                "use_llm_xml_for_fusion": True,
                "llm_xml": {
                    "vllm_base_url": "http://local/v1",
                    "vllm_model": "qwen-xml",
                    "repair_attempts": 1,
                },
            },
        )

        regression = workload_to_regression_config(manifest, use_llm_xml=True)
        pipeline = inject_workload_manifest_path(
            workload_to_pipeline_config(manifest, enable_llm=True),
            Path("configs/workloads/demo.json"),
        )

        by_name = {step["name"]: step for step in pipeline["steps"]}
        self.assertIn("generate_llm_xml", by_name)
        self.assertTrue(by_name["generate_llm_xml"]["required"])
        self.assertIn("llm_plan.json", by_name["fuse_workflow"]["command"])
        self.assertIn("llm_plan.json", by_name["audit_workflow_args"]["command"])
        self.assertIn("--use-llm-xml", by_name["regression_eval"]["command"])
        self.assertEqual(regression["paths"]["xml_plan"], "llm_plan.json")
        self.assertEqual(regression["paths"]["operation_xml_dir"], "llm_operations")

    def test_local_index_step_supports_semantic_chunking(self) -> None:
        manifest = WorkloadManifest(
            workload="semantic_demo",
            paths={
                "flow_path": "flow.xlsx",
                "index_dir": "semantic_index",
                "base_workflow": "base.xml",
                "template_contracts": "contracts.json",
                "xml_plan": "plan.json",
                "serial_xml": "serial.xml",
                "operation_xml_dir": "operations",
                "flow_evidence_trace": "trace.json",
                "diagnostic_graph": "graph.json",
                "fused_workflow": "fused.xml",
                "arg_audit_report": "audit.json",
                "audit_resolution_report": "resolution.json",
                "llm_resolution_report": "llm.json",
                "regression_report": "regression.json",
                "pipeline_report": "pipeline.json",
            },
            local_index={
                "enabled": True,
                "mineru_output_dir": "mineru",
                "collection_name": "semantic_collection",
                "chunking_mode": "semantic",
                "include_graph": False,
                "timeout_seconds": 99,
            },
        )

        pipeline = workload_to_pipeline_config(manifest)
        first_step = pipeline["steps"][0]

        self.assertEqual(first_step["name"], "build_local_index")
        self.assertEqual(first_step["timeout_seconds"], 99)
        self.assertIn("--mineru-output-dir", first_step["command"])
        self.assertIn("mineru", first_step["command"])
        self.assertIn("--index-dir", first_step["command"])
        self.assertIn("semantic_index", first_step["command"])
        self.assertIn("--collection-name", first_step["command"])
        self.assertIn("semantic_collection", first_step["command"])
        self.assertIn("--chunking-mode", first_step["command"])
        self.assertIn("semantic", first_step["command"])
        self.assertIn("--no-graph", first_step["command"])
        self.assertEqual(pipeline["steps"][1]["name"], "generate_xml_plan")


if __name__ == "__main__":
    unittest.main()
