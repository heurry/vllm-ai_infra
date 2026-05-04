#!/usr/bin/env python3
"""Run a dense or hybrid retrieval smoke query against a local Milvus vector index."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from diagnostic_platform.retrieval.local_sparse import query_local_index  # noqa: E402
from diagnostic_platform.schemas import RetrievalFilters, RetrievalQueryRequest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True)
    parser.add_argument("--index-dir", required=True)
    parser.add_argument("--collection-name", default="")
    parser.add_argument("--milvus-uri", default=os.environ.get("MILVUS_URI", "http://127.0.0.1:19530"))
    parser.add_argument(
        "--embedding-model",
        default=os.environ.get("EMBEDDING_MODEL", "/home/xdu/LLM/models/Qwen3-Embedding-0.6B"),
    )
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--dense-top-k", type=int, default=8)
    parser.add_argument("--hybrid-top-k", type=int, default=8)
    parser.add_argument("--dense-only", action="store_true")
    parser.add_argument("--sparse-only", action="store_true")
    parser.add_argument("--ecu", default="")
    parser.add_argument("--module", default="")
    parser.add_argument("--did", action="append", default=[])
    parser.add_argument("--service-id", action="append", default=[])
    parser.add_argument("--manifest-path", default="")
    args = parser.parse_args()

    response = query_local_index(
        RetrievalQueryRequest(
            query=args.query,
            index_dir=args.index_dir,
            filters=RetrievalFilters(
                ecu=args.ecu or None,
                module=args.module or None,
                dids=args.did,
                service_ids=args.service_id,
            ),
            top_k=args.top_k,
            enable_sparse=not args.dense_only,
            enable_dense=not args.sparse_only,
            dense_top_k=args.dense_top_k,
            hybrid_top_k=args.hybrid_top_k,
            milvus_uri=args.milvus_uri,
            vector_collection=args.collection_name,
            embedding_model=args.embedding_model,
            vector_manifest_path=args.manifest_path or None,
        )
    )
    print(json.dumps(response.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
