#!/usr/bin/env python3
"""Build a local diagnostic index from existing MinerU output."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from diagnostic_platform.indexing.local_builder import build_local_index  # noqa: E402
from diagnostic_platform.schemas import LocalIndexBuildRequest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mineru-output-dir",
        default="data/processed/mineru/treg_20260402_api/extracted",
        help="Directory containing MinerU extracted document folders.",
    )
    parser.add_argument(
        "--index-dir",
        default="data/index/treg_20260402",
        help="Output directory for JSONL index artifacts.",
    )
    parser.add_argument("--collection-name", default="treg_20260402")
    parser.add_argument("--protocol", default="UDS")
    parser.add_argument("--doc-type", default="pdf_protocol")
    parser.add_argument("--source", default="mineru")
    parser.add_argument("--no-graph", action="store_true", help="Skip diagnostic_graph.json generation.")
    parser.add_argument(
        "--chunking-mode",
        choices=["legacy", "semantic"],
        default="legacy",
        help="Use legacy MinerU block chunks or semantic chunks with provenance.",
    )
    args = parser.parse_args()

    result = build_local_index(
        LocalIndexBuildRequest(
            mineru_output_dir=args.mineru_output_dir,
            index_dir=args.index_dir,
            collection_name=args.collection_name,
            protocol=args.protocol,
            doc_type=args.doc_type,
            source=args.source,
            include_graph=not args.no_graph,
            chunking_mode=args.chunking_mode,
        )
    )
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
