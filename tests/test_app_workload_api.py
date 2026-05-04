"""Tests for workload API endpoints."""

from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from diagnostic_platform.app import (
        get_workload_manifest,
        run_workload_pipeline,
    )
    from diagnostic_platform.orchestration.workload import WorkloadPipelineRunRequest
except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional runtime deps in system python
    FASTAPI_IMPORT_ERROR = exc
else:
    FASTAPI_IMPORT_ERROR = None


@unittest.skipIf(FASTAPI_IMPORT_ERROR is not None, f"FastAPI is not installed: {FASTAPI_IMPORT_ERROR}")
class WorkloadApiTest(unittest.TestCase):
    def test_get_default_workload_manifest(self) -> None:
        response = get_workload_manifest()

        self.assertEqual(response.workload, "treg_20260402")

    def test_run_pipeline_endpoint_can_skip_all_steps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = _write_minimal_manifest(Path(temp_dir))
            response = run_workload_pipeline(
                WorkloadPipelineRunRequest(
                    workload_path=str(manifest_path),
                    skip_steps=[
                        "generate_xml_plan",
                        "fuse_workflow",
                        "audit_workflow_args",
                        "resolve_audit_report",
                        "regression_eval",
                    ],
                )
            )

        self.assertTrue(response.valid)
        self.assertEqual(response.summary["skipped"], 6)


def _write_minimal_manifest(root: Path) -> Path:
    paths = {
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
        "regression_report": str(root / "regression.json"),
        "pipeline_report": str(root / "pipeline.json"),
    }
    manifest_path = root / "workload.json"
    manifest_path.write_text(
        json.dumps({"workload": "api_fixture", "paths": paths}, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest_path


if __name__ == "__main__":
    unittest.main()
