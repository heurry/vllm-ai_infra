#!/usr/bin/env python3
"""Build a Milvus vector index from an existing local JSONL index directory."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from diagnostic_platform.indexing.vector_index import build_vector_index  # noqa: E402
from diagnostic_platform.schemas import VectorIndexBuildRequest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index-dir", required=True)
    parser.add_argument("--collection-name", default="")
    parser.add_argument("--milvus-uri", default=os.environ.get("MILVUS_URI", "http://127.0.0.1:19530"))
    parser.add_argument(
        "--embedding-model",
        default=os.environ.get("EMBEDDING_MODEL", "/home/xdu/LLM/models/Qwen3-Embedding-0.6B"),
    )
    parser.add_argument("--metric-type", default="COSINE")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--drop-existing", action="store_true")
    parser.add_argument("--knowledge-only", action="store_true")
    parser.add_argument("--evidence-only", action="store_true")
    parser.add_argument("--manifest-path", default="")
    parser.add_argument(
        "--milvus-timeout-seconds",
        type=float,
        default=float(os.environ.get("MILVUS_TIMEOUT_SECONDS") or "10"),
        help="Milvus client connection timeout. Defaults to MILVUS_TIMEOUT_SECONDS or 10.",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable progress logs on stderr.",
    )
    args = parser.parse_args()

    include_knowledge = not args.evidence_only
    include_evidence = not args.knowledge_only
    result = build_vector_index(
        VectorIndexBuildRequest(
            index_dir=args.index_dir,
            collection_name=args.collection_name,
            milvus_uri=args.milvus_uri,
            embedding_model=args.embedding_model,
            metric_type=args.metric_type,
            batch_size=args.batch_size,
            drop_existing=args.drop_existing,
            include_knowledge_units=include_knowledge,
            include_evidence_units=include_evidence,
            manifest_path=args.manifest_path or None,
        ),
        progress_callback=None if args.no_progress else _print_progress,
        milvus_timeout_seconds=args.milvus_timeout_seconds,
    )
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


def _print_progress(payload: dict[str, Any]) -> None:
    event = str(payload.get("event") or "")
    elapsed = float(payload.get("elapsed_seconds") or 0.0)
    prefix = f"[vector-index {elapsed:7.1f}s]"
    if event == "vector_index_records_loaded":
        message = f"{prefix} loaded {payload.get('record_count')} records from {payload.get('index_dir')}"
    elif event == "vector_index_embed_batch_start":
        message = (
            f"{prefix} embedding batch {payload.get('batch_index')}/{payload.get('total_batches')} "
            f"({payload.get('embedded_count')}/{payload.get('total_records')} records done)"
        )
    elif event == "vector_index_embed_batch_done":
        message = (
            f"{prefix} embedded batch {payload.get('batch_index')}/{payload.get('total_batches')} "
            f"({payload.get('embedded_count')}/{payload.get('total_records')} records, "
            f"{float(payload.get('batch_duration_seconds') or 0.0):.1f}s)"
        )
    elif event == "vector_index_collection_start":
        message = (
            f"{prefix} preparing Milvus collection {payload.get('collection_name')} "
            f"dim={payload.get('dimension')} metric={payload.get('metric_type')} "
            f"drop_existing={payload.get('drop_existing')}"
        )
    elif event == "vector_index_milvus_connect_start":
        message = (
            f"{prefix} connecting Milvus {payload.get('milvus_uri')} "
            f"collection={payload.get('collection_name')} timeout={payload.get('timeout_seconds')}s"
        )
    elif event == "vector_index_upsert_batch_done":
        message = (
            f"{prefix} upserted batch {payload.get('batch_index')}/{payload.get('total_batches')} "
            f"({payload.get('upserted_count')}/{payload.get('total_records')} records)"
        )
    elif event == "vector_index_flush_start":
        message = f"{prefix} flushing Milvus collection ({payload.get('upserted_count')} records)"
    elif event == "vector_index_done":
        message = (
            f"{prefix} done collection={payload.get('collection_name')} "
            f"vectors={payload.get('vector_count')} manifest={payload.get('manifest_path')}"
        )
    else:
        message = f"{prefix} {json.dumps(payload, ensure_ascii=False)}"
    print(message, file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
