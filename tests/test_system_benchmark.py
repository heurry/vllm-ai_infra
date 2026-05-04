"""Tests for system benchmark runner."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.evaluation.benchmark import run_system_benchmark


class SystemBenchmarkTest(unittest.TestCase):
    def test_run_json_metric_scenario(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = {
                "workload": "fixture",
                "iterations": 2,
                "scenarios": [
                    {
                        "name": "json_metrics",
                        "min_iterations_for_stats": 2,
                        "command": [
                            "{python}",
                            "-c",
                            (
                                "import json; "
                                "print(json.dumps({'valid': True, 'ttft_seconds': 0.1, "
                                "'tokens_per_second': 5.0}))"
                            ),
                        ],
                        "thresholds": {
                            "success_rate_min": 1.0,
                            "stdout_ttft_seconds_p95_max": 1.0,
                            "stdout_tokens_per_second_p50_min": 1.0,
                        },
                    }
                ],
            }

            report = run_system_benchmark(config, repo_root=Path(temp_dir))

        self.assertTrue(report.valid)
        self.assertEqual(report.summary["samples"], 2)
        scenario = report.scenarios[0]
        self.assertTrue(scenario.valid)
        self.assertEqual(scenario.metrics["success_rate"], 1.0)
        self.assertEqual(scenario.metrics["stdout_ttft_seconds_p95"], 0.1)
        self.assertEqual(scenario.metrics["stdout_ttft_seconds_p99"], 0.1)
        self.assertEqual(scenario.metrics["stdout_tokens_per_second_p50"], 5.0)
        self.assertIn("duration_stdev_seconds", scenario.metrics)
        self.assertEqual(scenario.quality_warnings, ["warmup_iterations is 0; first-sample cold effects may affect metrics"])

    def test_failed_command_marks_scenario_invalid_without_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = {
                "workload": "fixture",
                "scenarios": [
                    {
                        "name": "failed_command",
                        "command": ["{python}", "-c", "raise SystemExit(3)"],
                    }
                ],
            }

            report = run_system_benchmark(config, repo_root=Path(temp_dir))

        self.assertFalse(report.valid)
        self.assertFalse(report.scenarios[0].valid)
        self.assertEqual(report.scenarios[0].metrics["failed"], 1)
        self.assertIn("one or more samples failed", report.scenarios[0].quality_warnings)


if __name__ == "__main__":
    unittest.main()
