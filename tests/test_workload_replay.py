"""Tests for real workload replay dataset helpers."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.evaluation.replay import aggregate_replay_metrics, build_replay_dataset_from_artifacts


class WorkloadReplayTest(unittest.TestCase):
    def test_build_replay_dataset_from_artifacts(self) -> None:
        flow_trace_payload = {
            "steps": [
                {
                    "operations": [
                        {
                            "node_name": "Usage_Mode_Change_1",
                            "template_name": "GEEA30_VMM_Change",
                            "selected_args": [
                                {"name": "DID", "value": "0x7000", "selection_score": 19.5},
                            ],
                            "selected_evidence_ids": ["ev-1"],
                            "graph_paths": ["FlowNode:A --supported_by--> Evidence:ev-1"],
                            "notes": ["conflict with base workflow"],
                            "retrieval_matches": [
                                {
                                    "evidence_id": "ev-1",
                                    "score": 10.0,
                                    "matched_terms": ["mode", "change"],
                                    "content_excerpt": "Usage Mode Change DID 0x7000",
                                }
                            ],
                        }
                    ]
                }
            ]
        }
        resolution_payload = {
            "decisions": [
                {
                    "node_name": "Usage_Mode_Change_1",
                    "class_name": "GEEA30_VMM_Change",
                    "arg_name": "DID",
                    "requires_review": True,
                    "reason": "plan_conflicts_with_existing_workflow_value",
                    "confidence": 0.65,
                    "action": "keep_base",
                    "plan_value": "0x7000",
                    "base_value": "0xDD0A",
                    "fused_value": "0xDD0A",
                    "evidence_ids": ["ev-1"],
                }
            ]
        }

        dataset = build_replay_dataset_from_artifacts(
            workload="fixture",
            flow_trace_payload=flow_trace_payload,
            resolution_payload=resolution_payload,
            llm_resolution_payload=None,
            source_manifest="configs/workloads/fixture.json",
        )

        self.assertEqual(dataset.workload, "fixture")
        self.assertEqual(dataset.summary["items"], 4)
        self.assertEqual(dataset.summary["profiles"]["short_audit"], 1)
        self.assertEqual(dataset.summary["profiles"]["rerank"], 1)
        self.assertEqual(dataset.summary["profiles"]["long_context"], 1)
        self.assertEqual(dataset.summary["profiles"]["repair"], 1)

    def test_aggregate_replay_metrics(self) -> None:
        metrics = aggregate_replay_metrics(
            [
                {
                    "valid": True,
                    "duration_seconds": 1.0,
                    "ttft_seconds": 0.2,
                    "tpot_seconds": 0.01,
                    "tokens_per_second": 20.0,
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                },
                {
                    "valid": True,
                    "duration_seconds": 2.0,
                    "ttft_seconds": 0.4,
                    "tpot_seconds": 0.02,
                    "tokens_per_second": 15.0,
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                },
            ]
        )

        self.assertEqual(metrics["requests_total"], 2)
        self.assertEqual(metrics["completion_tokens_total"], 50)
        self.assertGreater(metrics["requests_per_second"], 0.0)
        self.assertAlmostEqual(metrics["ttft_seconds_p95"], 0.39, places=2)

    def test_aggregate_replay_metrics_tracks_xml_quality(self) -> None:
        metrics = aggregate_replay_metrics(
            [
                {
                    "valid": True,
                    "duration_seconds": 1.0,
                    "completion_tokens": 20,
                    "xml_quality_evaluated": True,
                    "xml_valid": True,
                    "xml_needs_review": False,
                },
                {
                    "valid": True,
                    "duration_seconds": 1.0,
                    "completion_tokens": 20,
                    "xml_quality_evaluated": True,
                    "xml_valid": False,
                    "xml_needs_review": True,
                },
            ]
        )

        self.assertEqual(metrics["xml_quality_samples"], 2)
        self.assertEqual(metrics["xml_valid_rate"], 0.5)
        self.assertEqual(metrics["xml_needs_review_rate"], 0.5)

    def test_build_replay_dataset_includes_llm_xml_profiles(self) -> None:
        dataset = build_replay_dataset_from_artifacts(
            workload="fixture",
            flow_trace_payload={"steps": []},
            resolution_payload={"decisions": []},
            llm_xml_payload={
                "contexts": [
                    {
                        "step_key": "step_001",
                        "node_name": "Demo_Node",
                        "template_name": "Demo_Class",
                        "class_name": "Demo_Class",
                        "allowed_arg_names": ["EcuBOMName", "DID"],
                        "required_arg_names": ["EcuBOMName"],
                        "context_items": [
                            {
                                "context_id": "candidate:1:ev-1",
                                "evidence_id": "ev-1",
                                "kind": "table_row",
                                "score": 10.0,
                                "parameter_fields": {"DID": "1234"},
                                "content": "DID 1234",
                            }
                        ],
                        "template_examples": [],
                    }
                ],
                "generations": [
                    {
                        "step_key": "step_001",
                        "node_name": "Demo_Node",
                        "template_name": "Demo_Class",
                        "class_name": "Demo_Class",
                        "xml": "<ScriptNode>",
                        "valid": False,
                        "xml_validation_errors": [{"code": "XML_PARSE_ERROR", "message": "bad"}],
                    }
                ],
            },
        )

        self.assertEqual(dataset.summary["profiles"]["xml_generation"], 1)
        self.assertEqual(dataset.summary["profiles"]["xml_repair"], 1)


if __name__ == "__main__":
    unittest.main()
