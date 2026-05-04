"""Tests for the end-to-end acceptance runner."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class E2EAcceptanceRunnerTest(unittest.TestCase):
    def test_dry_run_writes_three_scenario_manifests_and_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workload_path = root / "workload.json"
            report_path = root / "acceptance.json"
            summary_path = root / "acceptance.md"
            workload_path.write_text(
                json.dumps(_fixture_workload(), ensure_ascii=False),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/run_e2e_acceptance.py",
                    "--workload",
                    str(workload_path),
                    "--output-path",
                    str(report_path),
                    "--summary-path",
                    str(summary_path),
                    "--dry-run",
                    "--milvus-uri",
                    "http://127.0.0.1:19530",
                    "--vllm-base-url",
                    "http://127.0.0.1:8008/v1",
                    "--embedding-model",
                    "hashing:16",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["valid"])
            self.assertTrue(payload["dry_run"])
            self.assertEqual(len(payload["scenarios"]), 3)
            self.assertTrue(summary_path.exists())

            scenarios = {item["name"]: item for item in payload["scenarios"]}
            sparse_commands = {item["name"]: item["command"] for item in scenarios["sparse_deterministic"]["commands"]}
            hybrid_commands = {item["name"]: item["command"] for item in scenarios["hybrid_deterministic"]["commands"]}
            llm_commands = {item["name"]: item["command"] for item in scenarios["hybrid_llm_xml"]["commands"]}
            self.assertNotIn("build_vector_index", sparse_commands)
            self.assertIn("build_vector_index", hybrid_commands)
            self.assertIn("--enable-dense", hybrid_commands["generate_xml_plan"])
            self.assertIn("generate_llm_xml", llm_commands)
            self.assertIn("--use-llm-xml", llm_commands["regression_eval"])

            manifest_dir = root / "manifests" / "fixture"
            sparse_manifest = json.loads((manifest_dir / "sparse_deterministic.json").read_text(encoding="utf-8"))
            hybrid_manifest = json.loads((manifest_dir / "hybrid_deterministic.json").read_text(encoding="utf-8"))
            llm_manifest = json.loads((manifest_dir / "hybrid_llm_xml.json").read_text(encoding="utf-8"))
            self.assertFalse(sparse_manifest["hybrid_retrieval"]["enabled"])
            self.assertFalse(sparse_manifest["vector_index"]["enabled"])
            self.assertNotIn("dense_recall_at_k_min", sparse_manifest["regression_expectations"])
            self.assertTrue(hybrid_manifest["hybrid_retrieval"]["enabled"])
            self.assertTrue(hybrid_manifest["vector_index"]["enabled"])
            self.assertEqual(llm_manifest["generation"]["mode"], "llm_node")
            self.assertTrue(llm_manifest["generation"]["use_llm_xml_for_fusion"])


def _fixture_workload() -> dict:
    return {
        "workload": "fixture",
        "description": "fixture workload",
        "paths": {
            "flow_path": "flow.xlsx",
            "index_dir": "index",
            "base_workflow": "base.xml",
            "template_registry": "templates.json",
            "template_contracts": "contracts.json",
            "xml_plan": "out/xml_plan.json",
            "serial_xml": "out/serial.xml",
            "operation_xml_dir": "out/operations",
            "flow_evidence_trace": "out/trace.json",
            "diagnostic_graph": "out/graph.json",
            "llm_xml_plan": "out/llm_plan.json",
            "llm_serial_xml": "out/llm_serial.xml",
            "llm_operation_xml_dir": "out/llm_operations",
            "llm_generation_trace": "out/llm_trace.json",
            "llm_raw_source_dir": "out/raw_sources",
            "fused_workflow": "out/fused.xml",
            "arg_audit_report": "out/audit.json",
            "audit_resolution_report": "out/resolution.json",
            "llm_resolution_report": "out/llm_resolution.json",
            "regression_report": "reports/regression.json",
            "pipeline_report": "reports/pipeline.json",
            "acceptance_report": "reports/acceptance.json",
            "acceptance_summary": "reports/acceptance.md",
        },
        "generation": {
            "mode": "llm_node",
            "use_llm_xml_for_fusion": True,
            "top_k_per_node": 5,
            "llm_xml": {
                "vllm_base_url": "http://127.0.0.1:8008/v1",
                "vllm_model": "qwen-audit-resolver",
            },
        },
        "vector_index": {
            "enabled": True,
            "collection_name": "diagnostic_knowledge_fixture",
            "milvus_uri": "http://127.0.0.1:19530",
            "embedding_model": "hashing:16",
            "drop_existing": True,
        },
        "hybrid_retrieval": {
            "enabled": True,
            "vector_collection": "diagnostic_knowledge_fixture",
            "milvus_uri": "http://127.0.0.1:19530",
            "embedding_model": "hashing:16",
            "dense_top_k": 4,
            "hybrid_top_k": 4,
        },
        "fusion": {"target_serial_name": "FHC_ALL"},
        "retrieval_eval": {"top_k": 4, "cases": []},
        "regression_expectations": {
            "operation_dense_retrieval_coverage_min": 1.0,
            "operation_hybrid_retrieval_coverage_min": 1.0,
            "dense_recall_at_k_min": 0.5,
            "hybrid_recall_at_k_min": 0.5,
            "llm_valid_generations_min": 1,
            "llm_needs_review_max": 0,
        },
    }


if __name__ == "__main__":
    unittest.main()
