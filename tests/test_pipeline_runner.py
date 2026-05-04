"""Tests for configurable pipeline runner."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.orchestration.pipeline import run_pipeline


class PipelineRunnerTest(unittest.TestCase):
    def test_run_pipeline_skips_disabled_and_parses_json_stdout(self) -> None:
        report = run_pipeline(
            config={
                "workload": "fixture",
                "steps": [
                    {
                        "name": "ok",
                        "command": [
                            "{python}",
                            "-c",
                            "import json; print(json.dumps({'valid': True, 'value': 3}))",
                        ],
                        "required": True,
                    },
                    {
                        "name": "disabled",
                        "command": ["{python}", "-c", "raise SystemExit(1)"],
                        "required": False,
                        "enabled": False,
                    },
                ],
            },
            repo_root=Path(__file__).resolve().parents[1],
        )

        self.assertTrue(report.valid)
        self.assertEqual(report.summary["passed"], 1)
        self.assertEqual(report.summary["skipped"], 1)
        self.assertEqual(report.steps[0].parsed_stdout, {"valid": True, "value": 3})

    def test_required_failure_stops_pipeline(self) -> None:
        report = run_pipeline(
            config={
                "workload": "fixture",
                "steps": [
                    {
                        "name": "fail",
                        "command": ["{python}", "-c", "raise SystemExit(7)"],
                        "required": True,
                    },
                    {
                        "name": "not_run",
                        "command": ["{python}", "-c", "print('no')"],
                        "required": True,
                    },
                ],
            },
            repo_root=Path(__file__).resolve().parents[1],
        )

        self.assertFalse(report.valid)
        self.assertEqual(len(report.steps), 1)
        self.assertEqual(report.steps[0].return_code, 7)


if __name__ == "__main__":
    unittest.main()
