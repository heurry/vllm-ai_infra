#!/usr/bin/env python3
"""Benchmark an OpenAI-compatible vLLM chat completion endpoint."""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8008/v1")
    parser.add_argument("--model", default="qwen-audit-resolver")
    parser.add_argument("--api-key", default="EMPTY")
    parser.add_argument("--prompt", default="用一句话说明你已准备好进行 XML 审计。")
    parser.add_argument("--target-input-tokens", type=int, default=0)
    parser.add_argument("--prompt-unit", default="data")
    parser.add_argument("--cache-mode", choices=["warm", "cold"], default="warm")
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--min-tokens", type=int, default=0)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--no-stream", action="store_true")
    args = parser.parse_args()

    try:
        prompt = build_prompt(args.prompt, args.target_input_tokens, args.prompt_unit)
        prompt = apply_cache_mode(prompt, args.cache_mode)
        result = chat_completion(
            base_url=args.base_url,
            model=args.model,
            api_key=args.api_key,
            prompt=prompt,
            max_tokens=args.max_tokens,
            min_tokens=args.min_tokens,
            temperature=args.temperature,
            timeout_seconds=args.timeout_seconds,
            stream=not args.no_stream,
        )
        result["cache_mode"] = args.cache_mode
        result["target_input_tokens"] = args.target_input_tokens
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["valid"] else 2
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "valid": False,
                    "error": str(exc),
                    "base_url": args.base_url,
                    "model": args.model,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2


def build_prompt(prompt: str, target_input_tokens: int = 0, prompt_unit: str = "data") -> str:
    """Build a synthetic prompt for input-length benchmark buckets."""

    if target_input_tokens <= 0:
        return prompt

    unit = (prompt_unit or "data").strip() or "data"
    repeated_context = " ".join(unit for _ in range(target_input_tokens))
    return (
        f"{prompt}\n\n"
        "以下是用于推理 benchmark 的合成诊断上下文，内容重复但长度稳定：\n"
        f"{repeated_context}\n\n"
        "请只输出一个 JSON 对象，包含 status 和 summary 两个字段。"
    )


def apply_cache_mode(prompt: str, cache_mode: str, request_id: int | None = None) -> str:
    """Add deterministic salt for cold-cache benchmark samples."""

    if cache_mode != "cold":
        return prompt
    scenario = os.environ.get("BENCHMARK_SCENARIO", "scenario")
    iteration = os.environ.get("BENCHMARK_ITERATION", "0")
    request_part = f":{request_id}" if request_id is not None else ""
    return f"benchmark_salt={scenario}:{iteration}{request_part}\n\n{prompt}"


def chat_completion(
    *,
    base_url: str,
    model: str,
    api_key: str,
    prompt: str,
    max_tokens: int,
    min_tokens: int = 0,
    temperature: float,
    timeout_seconds: float,
    stream: bool = True,
) -> dict[str, Any]:
    """Call an OpenAI-compatible chat completion endpoint and return benchmark metrics."""

    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if min_tokens > 0:
        payload["min_tokens"] = min_tokens
    if stream:
        payload["stream_options"] = {"include_usage": True}

    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    started = time.monotonic()
    if not stream:
        with urlopen(request, timeout=timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
        duration = time.monotonic() - started
        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        usage = data.get("usage") or {}
        completion_tokens = int(usage.get("completion_tokens") or 0)
        return _result(
            valid=bool(content) and _enough_completion_tokens(completion_tokens, min_tokens),
            duration=duration,
            ttft=None,
            content=content,
            chunks=1 if content else 0,
            usage=usage,
            completion_tokens=completion_tokens,
            streamed=False,
            model=model,
            base_url=base_url,
            prompt_chars=len(prompt),
            max_tokens=max_tokens,
            min_tokens=min_tokens,
            finish_reason=choice.get("finish_reason"),
        )

    content_parts: list[str] = []
    chunks = 0
    ttft: float | None = None
    usage: dict = {}
    finish_reason: str | None = None
    with urlopen(request, timeout=timeout_seconds) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data:"):
                continue
            data_text = line.removeprefix("data:").strip()
            if data_text == "[DONE]":
                break
            event = json.loads(data_text)
            if event.get("usage"):
                usage = event["usage"]
            choices = event.get("choices") or []
            if not choices:
                continue
            if choices[0].get("finish_reason"):
                finish_reason = choices[0].get("finish_reason")
            delta = choices[0].get("delta") or {}
            token = delta.get("content") or ""
            if token:
                if ttft is None:
                    ttft = time.monotonic() - started
                content_parts.append(token)
                chunks += 1

    duration = time.monotonic() - started
    content = "".join(content_parts)
    completion_tokens = int(usage.get("completion_tokens") or chunks)
    return _result(
        valid=bool(content) and _enough_completion_tokens(completion_tokens, min_tokens),
        duration=duration,
        ttft=ttft,
        content=content,
        chunks=chunks,
        usage=usage,
        completion_tokens=completion_tokens,
        streamed=True,
        model=model,
        base_url=base_url,
        prompt_chars=len(prompt),
        max_tokens=max_tokens,
        min_tokens=min_tokens,
        finish_reason=finish_reason,
    )


def _result(
    valid: bool,
    duration: float,
    ttft: float | None,
    content: str,
    chunks: int,
    usage: dict,
    completion_tokens: int,
    streamed: bool,
    model: str,
    base_url: str,
    prompt_chars: int,
    max_tokens: int,
    min_tokens: int,
    finish_reason: str | None,
) -> dict[str, Any]:
    tpot = None
    if ttft is not None and completion_tokens > 1:
        tpot = max(duration - ttft, 0.0) / (completion_tokens - 1)
    tokens_per_second = completion_tokens / duration if duration > 0 and completion_tokens else 0.0
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    return {
        "valid": valid,
        "model": model,
        "base_url": base_url,
        "streamed": streamed,
        "prompt_chars": prompt_chars,
        "prompt_tokens": prompt_tokens,
        "max_tokens": max_tokens,
        "min_tokens": min_tokens,
        "finish_reason": finish_reason,
        "duration_seconds": round(duration, 6),
        "ttft_seconds": round(ttft, 6) if ttft is not None else None,
        "tpot_seconds": round(tpot, 6) if tpot is not None else None,
        "tokens_per_second": round(tokens_per_second, 6),
        "completion_tokens": completion_tokens,
        "chunks": chunks,
        "output_chars": len(content),
        "content": content,
        "content_preview": content[:300],
        "usage": usage,
    }


def _enough_completion_tokens(completion_tokens: int, min_tokens: int) -> bool:
    return min_tokens <= 0 or completion_tokens >= min_tokens


if __name__ == "__main__":
    raise SystemExit(main())
