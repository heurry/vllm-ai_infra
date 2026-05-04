#!/usr/bin/env python3
"""Build TaskNode template families from XML examples and flowchart descriptions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from diagnostic_platform.schemas import TemplateFamilyRegistryBuildRequest  # noqa: E402
from diagnostic_platform.template_family.registry_builder import build_template_family_registry  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xml-template-dir", default="data/xml_template")
    parser.add_argument("--flowchart-markdown", default="流程图.md")
    parser.add_argument("--output-path", default="data/processed/template_family_registry/template_families.json")
    parser.add_argument("--no-xml-examples", action="store_true")
    parser.add_argument("--max-example-chars", type=int, default=12000)
    args = parser.parse_args()

    registry = build_template_family_registry(
        TemplateFamilyRegistryBuildRequest(
            xml_template_dir=args.xml_template_dir,
            flowchart_markdown_path=args.flowchart_markdown,
            output_path=args.output_path,
            include_xml_examples=not args.no_xml_examples,
            max_example_chars=args.max_example_chars,
        )
    )
    print(
        json.dumps(
            {
                "xml_template_dir": registry.xml_template_dir,
                "flowchart_markdown_path": registry.flowchart_markdown_path,
                "family_count": len(registry.families),
                "tasknode_template_count": registry.summary.get("tasknode_template_count", 0),
                "families_with_flowcharts": registry.summary.get("families_with_flowcharts", 0),
                "output_path": args.output_path,
                "families": [
                    {
                        "family_id": family.family_id,
                        "templates": family.template_class_names,
                        "flowcharts": [flowchart.title for flowchart in family.flowcharts],
                        "required_evidence": family.required_evidence,
                    }
                    for family in registry.families
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
