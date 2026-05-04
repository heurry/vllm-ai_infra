#!/usr/bin/env python3
"""Build an XML ScriptNode template registry from existing XML files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from diagnostic_platform.renderers.template_registry import build_template_registry  # noqa: E402
from diagnostic_platform.schemas import XmlTemplateRegistryBuildRequest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", default="data/xml_template")
    parser.add_argument("--output-path", default="data/processed/xml_template_registry/treg_20260402.json")
    parser.add_argument("--exclude-workflows", action="store_true")
    args = parser.parse_args()

    registry = build_template_registry(
        XmlTemplateRegistryBuildRequest(
            source_dir=args.source_dir,
            output_path=args.output_path,
            include_workflow_files=not args.exclude_workflows,
        )
    )
    print(
        json.dumps(
            {
                "source_dir": registry.source_dir,
                "templates": len(registry.templates),
                "class_names": len(registry.by_class_name),
                "node_names": len(registry.by_node_name),
                "output_path": args.output_path,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
