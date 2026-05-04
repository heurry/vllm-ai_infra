#!/usr/bin/env python3
"""Benchmark concurrent OpenAI-compatible vLLM chat requests."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
from pathlib import Path
import statistics
import sys
import time
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from benchmark_vllm_chat import apply_cache_mode, build_prompt, chat_completion  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", action="append", default=[])
    parser.add_argument("--model", default="qwen-audit-resolver")
    parser.add_argument("--api-key", default="EMPTY")
    parser.add_argument("--prompt", default="请用 JSON 输出当前并发推理请求已完成。")
    parser.add_argument("--target-input-tokens", type=int, default=0)
    parser.add_argument("--prompt-unit", default="data")
    parser.add_argument("--cache-mode", choices=["warm", "cold"], default="warm")
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--min-tokens", type=int, default=0)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout-seconds", type=float, default=180)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--requests", type=int, default=0)
    parser.add_argument("--no-stream", action="store_true")
    args = parser.parse_args()

    base_urls = args.base_url or ["http://127.0.0.1:8008/v1"]
    request_count = args.requests or args.concurrency
    prompt = build_prompt(args.prompt, args.target_input_tokens, args.prompt_unit)

    started = time.monotonic()
    samples: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(
                _run_one,
                args,
                base_urls[index % len(base_urls)],
                apply_cache_mode(prompt, args.cache_mode, index + 1),
                index + 1,
            )
            for index in range(request_count)
        ]
        for future in as_completed(futures):
            samples.append(future.result())
    wall_duration = time.monotonic() - started

    report = _aggregate(samples, wall_duration, args.concurrency, request_count, base_urls, args.cache_mode)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["valid"] else 2


def _run_one(args: argparse.Namespace, base_url: str, prompt: str, request_id: int) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = chat_completion(
            base_url=base_url,
            model=args.model,
            api_key=args.api_key,
            prompt=prompt,
            max_tokens=args.max_tokens,
            min_tokens=args.min_tokens,
            temperature=args.temperature,
            timeout_seconds=args.timeout_seconds,
            stream=not args.no_stream,
        )
        result["request_id"] = request_id
        result["status"] = "passed" if result.get("valid") else "failed"
        return result
    except Exception as exc:  # pragma: no cover - exercised against live services
        return {
            "request_id": request_id,
            "status": "failed",
            "valid": False,
            "base_url": base_url,
            "error": str(exc),
            "duration_seconds": round(time.monotonic() - started, 6),
        }


def _aggregate(
    samples: list[dict[str, Any]],
    wall_duration: float,
    concurrency: int,
    request_count: int,
    base_urls: list[str],
    cache_mode: str,
) -> dict[str, Any]:
    passed = sum(1 for sample in samples if sample.get("status") == "passed")
    failed = len(samples) - passed
    completion_tokens = [float(sample.get("completion_tokens") or 0) for sample in samples]
    total_completion_tokens = int(sum(completion_tokens))
    metrics = {
        "valid": failed == 0 and len(samples) == request_count,
        "concurrency": concurrency,
        "requests": request_count,
        "endpoints": len(base_urls),
        "cache_mode": cache_mode,
        "passed": passed,
        "failed": failed,
        "success_rate": _safe_ratio(passed, request_count),
        "duration_seconds": round(wall_duration, 6),
        "requests_per_second": round(request_count / wall_duration, 6) if wall_duration > 0 else 0.0,
        "tokens_per_second_total": round(total_completion_tokens / wall_duration, 6) if wall_duration > 0 else 0.0,
        "completion_tokens_total": total_completion_tokens,
        "endpoint_stats": _endpoint_stats(samples),
    }
    for key in (
        "duration_seconds",
        "ttft_seconds",
        "tpot_seconds",
        "tokens_per_second",
        "completion_tokens",
        "prompt_tokens",
    ):
        values = [
            float(sample[key])
            for sample in samples
            if isinstance(sample.get(key), int | float) and not isinstance(sample.get(key), bool)
        ]
        if values:
            metrics[f"{key}_min"] = round(min(values), 6)
            metrics[f"{key}_max"] = round(max(values), 6)
            metrics[f"{key}_mean"] = round(statistics.fmean(values), 6)
            metrics[f"{key}_p50"] = _percentile(values, 50)
            metrics[f"{key}_p90"] = _percentile(values, 90)
            metrics[f"{key}_p95"] = _percentile(values, 95)
            metrics[f"{key}_p99"] = _percentile(values, 99)
            if len(values) > 1:
                stdev = statistics.stdev(values)
                mean = statistics.fmean(values)
                metrics[f"{key}_stdev"] = round(stdev, 6)
                metrics[f"{key}_cv"] = round(stdev / mean, 6) if mean else 0.0
            else:
                metrics[f"{key}_stdev"] = 0.0
                metrics[f"{key}_cv"] = 0.0

    metrics["samples"] = sorted(samples, key=lambda sample: int(sample.get("request_id") or 0))
    return metrics


def _endpoint_stats(samples: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        grouped.setdefault(str(sample.get("base_url") or "unknown"), []).append(sample)

    stats: dict[str, dict[str, Any]] = {}
    for base_url, endpoint_samples in sorted(grouped.items()):
        passed = sum(1 for sample in endpoint_samples if sample.get("status") == "passed")
        total_tokens = sum(int(sample.get("completion_tokens") or 0) for sample in endpoint_samples)
        durations = [
            float(sample["duration_seconds"])
            for sample in endpoint_samples
            if isinstance(sample.get("duration_seconds"), int | float)
        ]
        stats[base_url] = {
            "requests": len(endpoint_samples),
            "passed": passed,
            "failed": len(endpoint_samples) - passed,
            "success_rate": _safe_ratio(passed, len(endpoint_samples)),
            "completion_tokens_total": total_tokens,
            "duration_p95_seconds": _percentile(durations, 95) if durations else 0.0,
        }
    return stats


def _percentile(values: list[float], percentile: int) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile / 100 * len(ordered)) - 1))
    return round(ordered[index], 6)


def _safe_ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


if __name__ == "__main__":
    raise SystemExit(main())
