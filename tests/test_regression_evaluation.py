"""Tests for pipeline regression evaluation."""

from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.evaluation.regression import run_regression_evaluation


class RegressionEvaluationTest(unittest.TestCase):
    def test_run_regression_evaluation_passes_minimal_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            operation_dir = root / "operations"
            operation_dir.mkdir()

            script_xml = (
                '<?xml version="1.0"?>\n'
                '<ScriptNode Name="Demo_Node" DeadMS="0" RetryTimes="0" ScriptType="NORMAL" '
                'InteruptStart="false" ClassName="Demo_Class"><Args><Arg ArgName="DID">0x1234</Arg></Args></ScriptNode>\n'
            )
            script_body = script_xml.removeprefix('<?xml version="1.0"?>\n')
            serial_xml = (
                '<?xml version="1.0"?>\n'
                '<SerialNode Name="MAC_ALL" DeadMS="0"><Serials>'
                '<ParallelNode Name="step_001_parallel" DeadMS="0"><Tasks>'
                f"{script_body}"
                '</Tasks></ParallelNode></Serials></SerialNode>\n'
            )
            (operation_dir / "step_001_Demo_Node.xml").write_text(script_xml, encoding="utf-8")
            (root / "serial_node.xml").write_text(serial_xml, encoding="utf-8")
            (root / "fused_workflow.xml").write_text(serial_xml, encoding="utf-8")

            _write_json(
                root / "xml_plan.json",
                {
                    "flow_plan": {"steps": [{"step_key": "step_001"}]},
                    "plan": {
                        "serial": {
                            "scripts": [
                                {
                                    "node_name": "Demo_Node",
                                    "class_name": "Demo_Class",
                                    "args": [
                                        {
                                            "name": "DID",
                                            "value": "0x1234",
                                            "evidence_ids": ["ev_001"],
                                            "selection_score": 10.0,
                                        }
                                    ],
                                }
                            ]
                        },
                        "nodes": [{"node_name": "Demo_Node"}],
                    },
                    "validation": {"valid": True},
                    "operation_xml_files": [{"path": str(operation_dir / "step_001_Demo_Node.xml")}],
                },
            )
            _write_json(
                root / "flow_evidence_trace.json",
                {
                    "steps": [
                        {
                            "operations": [
                                {
                                    "node_name": "Demo_Node",
                                    "retrieval_matches": [{"evidence_id": "ev_001"}],
                                    "graph_paths": ["FlowNode:Demo_Node --uses_did--> DID:1234"],
                                }
                            ]
                        }
                    ]
                },
            )
            _write_json(root / "diagnostic_graph.json", {"entities": [{"entity_id": "FlowNode:Demo_Node"}], "relations": [{"source_id": "a"}]})
            _write_json(root / "arg_audit_report.json", {"summary": {"review_items": 0}})
            _write_json(root / "audit_resolution_report.json", {"summary": {"review_required": 0}})

            report = run_regression_evaluation(
                {
                    "workload": "fixture",
                    "paths": {
                        "xml_plan": str(root / "xml_plan.json"),
                        "flow_evidence_trace": str(root / "flow_evidence_trace.json"),
                        "diagnostic_graph": str(root / "diagnostic_graph.json"),
                        "serial_xml": str(root / "serial_node.xml"),
                        "fused_workflow": str(root / "fused_workflow.xml"),
                        "arg_audit_report": str(root / "arg_audit_report.json"),
                        "audit_resolution_report": str(root / "audit_resolution_report.json"),
                        "operation_xml_dir": str(operation_dir),
                    },
                    "expectations": {
                        "flow_steps": 1,
                        "planned_nodes": 1,
                        "trace_operations": 1,
                        "operation_xml_files": 1,
                        "serial_script_nodes": 1,
                        "serial_parallel_nodes": 1,
                        "fused_script_nodes_min": 1,
                        "fused_parallel_nodes_min": 1,
                        "graph_entities_min": 1,
                        "graph_relations_min": 1,
                        "graph_paths_min": 1,
                        "arg_selection_score_coverage_min": 1.0,
                        "operation_graph_path_coverage_min": 1.0,
                        "operation_retrieval_coverage_min": 1.0,
                        "audit_review_items_max": 0,
                        "resolution_review_required_max": 0,
                    },
                    "critical_args": [
                        {"node_name": "Demo_Node", "arg_name": "DID", "value": "0x1234"}
                    ],
                }
            )

        failed = [check.model_dump(mode="json") for check in report.checks if check.status == "fail"]
        self.assertTrue(report.valid, failed)
        self.assertEqual(report.metrics["trace_operations"], 1)
        self.assertFalse(failed)

    def test_retrieval_eval_metrics_from_trace(self) -> None:
        trace_operations = [
            {
                "node_name": "Demo_Node",
                "retrieval_matches": [
                    {
                        "evidence_id": "ev_001",
                        "source": "hybrid",
                        "metadata": {"hybrid_sources": ["dense", "sparse"], "source_id": "ev_001"},
                    }
                ],
                "retrieval_trace": {
                    "dense": {
                        "latency_seconds": 0.02,
                        "results": [
                            {"chunk_id": "md_ev_001", "metadata": {"source_evidence_id": "ev_001"}}
                        ],
                    },
                    "hybrid": {"results": [{"chunk_id": "ev_001", "metadata": {"source_id": "ev_001"}}]},
                },
            }
        ]

        from diagnostic_platform.evaluation.regression import _retrieval_eval_metrics

        metrics = _retrieval_eval_metrics(
            trace_operations,
            {
                "top_k": 3,
                "cases": [{"node_name": "Demo_Node", "expected_evidence_ids": ["ev_001"]}],
            },
        )

        self.assertEqual(metrics["dense_recall_at_k"], 1.0)
        self.assertEqual(metrics["hybrid_recall_at_k"], 1.0)
        self.assertEqual(metrics["dense_mrr"], 1.0)
        self.assertEqual(metrics["average_dense_latency_seconds"], 0.02)

    def test_llm_xml_regression_tracks_generation_trace_and_baseline_args(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            operation_dir = root / "llm_operations"
            operation_dir.mkdir()
            script_xml = (
                '<?xml version="1.0"?>\n'
                '<ScriptNode Name="Demo_Node" DeadMS="0" RetryTimes="0" ScriptType="NORMAL" '
                'InteruptStart="false" ClassName="Demo_Class"><Args><Arg ArgName="DID">0x1234</Arg></Args></ScriptNode>\n'
            )
            script_body = script_xml.removeprefix('<?xml version="1.0"?>\n')
            serial_xml = (
                '<?xml version="1.0"?>\n'
                '<SerialNode Name="MAC_ALL" DeadMS="0"><Serials>'
                '<ParallelNode Name="step_001_parallel" DeadMS="0"><Tasks>'
                f"{script_body}"
                '</Tasks></ParallelNode></Serials></SerialNode>\n'
            )
            (operation_dir / "step_001_Demo_Node.xml").write_text(script_xml, encoding="utf-8")
            (root / "llm_serial_node.xml").write_text(serial_xml, encoding="utf-8")
            (root / "fused_workflow.xml").write_text(serial_xml, encoding="utf-8")

            plan_payload = {
                "flow_plan": {"steps": [{"step_key": "step_001"}]},
                "plan": {
                    "serial": {
                        "scripts": [
                            {
                                "node_name": "Demo_Node",
                                "class_name": "Demo_Class",
                                "args": [
                                    {
                                        "name": "DID",
                                        "value": "0x1234",
                                        "evidence_ids": ["ev_001"],
                                        "selection_score": 0.91,
                                    }
                                ],
                            }
                        ]
                    },
                    "nodes": [{"node_name": "Demo_Node"}],
                },
                "plan_validation": {"valid": True},
                "operation_xml_files": [{"path": str(operation_dir / "step_001_Demo_Node.xml")}],
            }
            _write_json(root / "llm_xml_plan.json", plan_payload)
            _write_json(root / "baseline_xml_plan.json", {**plan_payload, "validation": {"valid": True}})
            _write_json(
                root / "llm_generation_trace.json",
                {
                    "summary": {"generations": 1, "valid_generations": 1, "needs_review": 0, "repair_attempts": 1},
                    "generations": [
                        {
                            "node_name": "Demo_Node",
                            "valid": True,
                            "needs_review": False,
                            "repair_attempts": [{"attempt": 1}],
                            "xml_validation_errors": [],
                            "raw_llm_args": [{"name": "DID", "value": "0x9999", "evidence_ids": []}],
                            "post_guardrail_args": [{"name": "DID", "value": "0x1234", "evidence_ids": ["ev_001"]}],
                            "guardrail_corrections": [
                                {
                                    "node_name": "Demo_Node",
                                    "arg_name": "DID",
                                    "raw_value": "0x9999",
                                    "corrected_value": "0x1234",
                                    "correction_source": "deterministic_plan",
                                }
                            ],
                            "prompt_estimated_tokens": 100,
                            "output_estimated_tokens": 50,
                            "generation_latency_seconds": 0.25,
                        }
                    ],
                },
            )
            _write_json(
                root / "flow_evidence_trace.json",
                {"steps": [{"operations": [{"node_name": "Demo_Node", "retrieval_matches": [{"evidence_id": "ev_001"}], "graph_paths": ["p"]}]}]},
            )
            _write_json(root / "diagnostic_graph.json", {"entities": [{"entity_id": "FlowNode:Demo_Node"}], "relations": [{"source_id": "a"}]})
            _write_json(root / "arg_audit_report.json", {"summary": {"review_items": 0}})
            _write_json(root / "audit_resolution_report.json", {"summary": {"review_required": 0}})

            report = run_regression_evaluation(
                {
                    "workload": "fixture",
                    "generation": {"active_plan": "llm_xml_plan", "mode": "llm_node"},
                    "paths": {
                        "xml_plan": str(root / "llm_xml_plan.json"),
                        "baseline_xml_plan": str(root / "baseline_xml_plan.json"),
                        "llm_generation_trace": str(root / "llm_generation_trace.json"),
                        "flow_evidence_trace": str(root / "flow_evidence_trace.json"),
                        "diagnostic_graph": str(root / "diagnostic_graph.json"),
                        "serial_xml": str(root / "llm_serial_node.xml"),
                        "fused_workflow": str(root / "fused_workflow.xml"),
                        "arg_audit_report": str(root / "arg_audit_report.json"),
                        "audit_resolution_report": str(root / "audit_resolution_report.json"),
                        "operation_xml_dir": str(operation_dir),
                    },
                    "expectations": {
                        "flow_steps": 1,
                        "planned_nodes": 1,
                        "trace_operations": 1,
                        "operation_xml_files": 1,
                        "serial_script_nodes": 1,
                        "serial_parallel_nodes": 1,
                        "fused_script_nodes_min": 1,
                        "fused_parallel_nodes_min": 1,
                        "graph_entities_min": 1,
                        "graph_relations_min": 1,
                        "graph_paths_min": 1,
                        "arg_selection_score_coverage_min": 1.0,
                        "operation_graph_path_coverage_min": 1.0,
                        "operation_retrieval_coverage_min": 1.0,
                        "llm_valid_generations_min": 1,
                        "llm_needs_review_max": 0,
                        "llm_repair_attempts_max": 1,
                        "post_guardrail_critical_arg_accuracy_min": 1.0,
                        "audit_review_items_max": 0,
                        "resolution_review_required_max": 0,
                    },
                    "critical_args": [{"node_name": "Demo_Node", "arg_name": "DID", "value": "0x1234"}],
                    "node_golden_checks": [
                        {"node_name": "Demo_Node", "class_name": "Demo_Class", "args": {"DID": "0x1234"}}
                    ],
                }
            )

        failed = [check.model_dump(mode="json") for check in report.checks if check.status == "fail"]
        self.assertTrue(report.valid, failed)
        self.assertEqual(report.metrics["llm_valid_generations"], 1)
        self.assertEqual(report.metrics["llm_repair_attempts"], 1)
        self.assertEqual(report.metrics["raw_llm_critical_arg_accuracy"], 0.0)
        self.assertEqual(report.metrics["post_guardrail_critical_arg_accuracy"], 1.0)
        self.assertEqual(report.metrics["guardrail_correction_count"], 1)
        self.assertEqual(report.metrics["node_golden_accuracy"], 1.0)
        self.assertFalse(failed)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
