#!/usr/bin/env python3
"""Replay real workload prompts against an OpenAI-compatible vLLM endpoint."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from benchmark_vllm_chat import apply_cache_mode, chat_completion  # noqa: E402
from diagnostic_platform.evaluation.replay import (  # noqa: E402
    aggregate_replay_metrics,
    aggregate_replay_profiles,
    load_replay_dataset,
)
from diagnostic_platform.generation.vllm_client import extract_json_object  # noqa: E402
from diagnostic_platform.schemas import XmlValidationRequest  # noqa: E402
from diagnostic_platform.validation.xml_validator import validate_xml  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-path", default="data/processed/replay/treg_20260402_workload_replay.json")
    parser.add_argument("--base-url", action="append", default=None)
    parser.add_argument("--model", default="qwen-audit-resolver")
    parser.add_argument("--api-key", default="EMPTY")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--profile", action="append", default=None)
    parser.add_argument("--cache-mode", choices=["warm", "cold"])
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--no-stream", action="store_true")
    args = parser.parse_args()

    dataset_path = _resolve_path(args.dataset_path)
    dataset = load_replay_dataset(dataset_path)
    base_urls = args.base_url or ["http://127.0.0.1:8008/v1"]
    selected_profiles = set(args.profile or [])
    items = [
        item
        for item in dataset.items
        if not selected_profiles or item.profile in selected_profiles
    ]
    if args.limit > 0:
        items = items[: args.limit]

    started = time.monotonic()
    results = _run_replay(
        items=items,
        base_urls=base_urls,
        model=args.model,
        api_key=args.api_key,
        timeout_seconds=args.timeout_seconds,
        concurrency=max(1, args.concurrency),
        stream=not args.no_stream,
        cache_mode_override=args.cache_mode,
    )
    wall_time = time.monotonic() - started

    overall = aggregate_replay_metrics(results)
    overall["wall_time_seconds"] = round(wall_time, 6)
    overall["requests_per_second"] = (
        round(overall["valid_requests"] / wall_time, 6) if wall_time > 0 else 0.0
    )
    payload = {
        "valid": overall["invalid_requests"] == 0,
        "dataset_path": str(dataset_path.relative_to(REPO_ROOT)),
        "workload": dataset.workload,
        "profiles_requested": sorted(selected_profiles),
        "concurrency": max(1, args.concurrency),
        **overall,
        "profiles": aggregate_replay_profiles(results),
        "samples": results,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["valid"] else 2


def _run_replay(
    *,
    items: list,
    base_urls: list[str],
    model: str,
    api_key: str,
    timeout_seconds: float,
    concurrency: int,
    stream: bool,
    cache_mode_override: str | None,
) -> list[dict[str, Any]]:
    if concurrency <= 1:
        return [
            _run_one(
                index=index,
                item=item,
                base_url=base_urls[index % len(base_urls)],
                model=model,
                api_key=api_key,
                timeout_seconds=timeout_seconds,
                stream=stream,
                cache_mode_override=cache_mode_override,
            )
            for index, item in enumerate(items)
        ]

    futures = {}
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        for index, item in enumerate(items):
            future = executor.submit(
                _run_one,
                index=index,
                item=item,
                base_url=base_urls[index % len(base_urls)],
                model=model,
                api_key=api_key,
                timeout_seconds=timeout_seconds,
                stream=stream,
                cache_mode_override=cache_mode_override,
            )
            futures[future] = index
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: int(item.get("request_index") or 0))
    return results


def _run_one(
    *,
    index: int,
    item,
    base_url: str,
    model: str,
    api_key: str,
    timeout_seconds: float,
    stream: bool,
    cache_mode_override: str | None,
) -> dict[str, Any]:
    cache_mode = cache_mode_override or item.cache_mode
    prompt = apply_cache_mode(item.prompt, cache_mode, request_id=index)
    started = time.monotonic()
    try:
        result = chat_completion(
            base_url=base_url,
            model=model,
            api_key=api_key,
            prompt=prompt,
            max_tokens=item.max_tokens,
            min_tokens=item.min_tokens,
            temperature=0.0,
            timeout_seconds=timeout_seconds,
            stream=stream,
        )
        result.update(
            {
                "item_id": item.item_id,
                "profile": item.profile,
                "cache_mode": cache_mode,
                "request_index": index,
                "tags": item.tags,
            }
        )
        _attach_xml_generation_quality(result, item.profile)
        return result
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return {
            "valid": False,
            "item_id": item.item_id,
            "profile": item.profile,
            "cache_mode": cache_mode,
            "request_index": index,
            "base_url": base_url,
            "model": model,
            "duration_seconds": round(time.monotonic() - started, 6),
            "error": str(exc),
            "completion_tokens": 0,
            "tokens_per_second": 0.0,
        }


def _resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


def _attach_xml_generation_quality(result: dict[str, Any], profile: str) -> None:
    if profile not in {"xml_generation", "long_context_xml_generation", "xml_repair"}:
        return
    result["xml_quality_evaluated"] = True
    content = str(result.get("content") or result.get("content_preview") or "")
    try:
        payload = extract_json_object(content)
    except Exception as exc:  # noqa: BLE001 - benchmark should record parse failures, not raise.
        result.update(
            {
                "xml_valid": False,
                "xml_needs_review": True,
                "xml_validation_issue_codes": ["JSON_PARSE_FAILED"],
                "xml_quality_error": str(exc),
            }
        )
        return

    xml = str(payload.get("xml") or "")
    validation = validate_xml(XmlValidationRequest(xml=xml, min_script_nodes=1))
    script_nodes = int(validation.stats.get("script_nodes") or 0)
    xml_valid = bool(validation.valid and script_nodes == 1)
    issue_codes = [issue.code for issue in validation.issues]
    if script_nodes != 1:
        issue_codes.append("SCRIPT_NODE_COUNT_NOT_ONE")
    result.update(
        {
            "xml_valid": xml_valid,
            "xml_needs_review": bool(payload.get("needs_review")),
            "xml_validation_issue_codes": issue_codes,
        }
    )


if __name__ == "__main__":
    raise SystemExit(main())
