"""Tests for vLLM metrics parsing."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.serving.metrics import metric_delta, parse_prometheus_metrics


class ServingMetricsTest(unittest.TestCase):
    def test_parse_prometheus_metrics_aggregates_by_name(self) -> None:
        text = """
# HELP vllm:prompt_tokens_total prompt tokens
vllm:prompt_tokens_total{model_name="qwen"} 10
vllm:prompt_tokens_total{model_name="qwen2"} 5
vllm:e2e_request_latency_seconds_bucket{le="1"} 3
vllm:num_requests_running 2
"""

        metrics = parse_prometheus_metrics(text)

        self.assertEqual(metrics["vllm:prompt_tokens_total"], 15.0)
        self.assertEqual(metrics["vllm:num_requests_running"], 2.0)
        self.assertNotIn("vllm:e2e_request_latency_seconds_bucket", metrics)

    def test_metric_delta(self) -> None:
        before = {"aggregate": {"a": 1.0, "b": 3.0}}
        after = {"aggregate": {"a": 4.0, "b": 2.0}}

        self.assertEqual(metric_delta(before, after), {"a": 3.0, "b": -1.0})


if __name__ == "__main__":
    unittest.main()

