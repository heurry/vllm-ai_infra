#!/usr/bin/env python3
"""Build ClassName argument contracts from an XML template registry."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from diagnostic_platform.resolution.template_contract import build_template_contracts  # noqa: E402
from diagnostic_platform.schemas import XmlTemplateContractBuildRequest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-path", default="data/processed/xml_template_registry/treg_20260402.json")
    parser.add_argument("--output-path", default="data/processed/xml_template_registry/treg_20260402_contracts.json")
    parser.add_argument("--required-observed-ratio", type=float, default=1.0)
    args = parser.parse_args()

    contracts = build_template_contracts(
        XmlTemplateContractBuildRequest(
            registry_path=args.registry_path,
            output_path=args.output_path,
            required_observed_ratio=args.required_observed_ratio,
        )
    )
    print(
        json.dumps(
            {
                "classes": len(contracts.class_contracts),
                "output_path": args.output_path,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

