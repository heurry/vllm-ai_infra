#!/usr/bin/env python3
"""Fuse an XML plan into a base workflow XML."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from diagnostic_platform.renderers.workflow_fusion import fuse_workflow  # noqa: E402
from diagnostic_platform.schemas import WorkflowFusionRequest, XmlGenerationPlan, XmlValidationRequest  # noqa: E402
from diagnostic_platform.validation.xml_validator import validate_xml  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-path", default="data/processed/xml_plan/treg_20260402/xml_plan.json")
    parser.add_argument("--base-xml-path", default="pdf-loop/20260402_new_v2/0402/TREG/P177__FHC（主流程）.xml")
    parser.add_argument("--output-path", default="data/processed/workflow/treg_20260402/fused_workflow.xml")
    parser.add_argument("--target-serial-name", default="FHC_ALL")
    parser.add_argument("--arg-merge-strategy", choices=["fill_missing", "overwrite"], default="fill_missing")
    parser.add_argument("--update-class-name", action="store_true")
    parser.add_argument("--append-missing-args", action="store_true")
    parser.add_argument("--insert-missing-nodes", action="store_true")
    args = parser.parse_args()

    plan = _load_plan(Path(args.plan_path))
    fusion = fuse_workflow(
        WorkflowFusionRequest(
            plan=plan,
            base_xml_path=args.base_xml_path,
            output_path=args.output_path,
            target_serial_name=args.target_serial_name,
            arg_merge_strategy=args.arg_merge_strategy,
            update_class_name=args.update_class_name,
            append_missing_args=args.append_missing_args,
            insert_missing_nodes=args.insert_missing_nodes,
        )
    )
    validation = validate_xml(
        XmlValidationRequest(
            xml=fusion.xml,
            min_script_nodes=1,
        )
    )
    print(
        json.dumps(
            {
                "valid": validation.valid,
                "root_tag": fusion.root_tag,
                "matched_nodes": fusion.matched_nodes,
                "updated_nodes": fusion.updated_nodes,
                "inserted_nodes": fusion.inserted_nodes,
                "missing_nodes": fusion.missing_nodes,
                "updated_args": fusion.updated_args,
                "appended_args": fusion.appended_args,
                "output_path": fusion.output_path,
                "xml_stats": validation.stats,
                "issues": [issue.model_dump(mode="json") for issue in validation.issues],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if validation.valid else 2


def _load_plan(path: Path) -> XmlGenerationPlan:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "plan" in payload:
        payload = payload["plan"]
    return XmlGenerationPlan.model_validate(payload)


if __name__ == "__main__":
    raise SystemExit(main())
