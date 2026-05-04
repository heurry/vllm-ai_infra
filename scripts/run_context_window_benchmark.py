#!/usr/bin/env python3
"""Run a rigorous context-window benchmark matrix with warmup and measured samples."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from diagnostic_platform.evaluation.benchmark import run_system_benchmark  # noqa: E402


MODEL_DIR = "/home/xdu/huggingface/cyankiwi-Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit"
SERVED_MODEL_NAME = "qwen-audit-resolver"
DEFAULT_GROUPS = [
    {"max_model_len": 32768, "targets": [16000, 24000, 32000]},
    {"max_model_len": 49152, "targets": [48000]},
    {"max_model_len": 65536, "targets": [64000]},
    {"max_model_len": 98304, "targets": [96000]},
    {"max_model_len": 131072, "targets": [128000]},
]


class ManagedVllmService:
    """A TP2 vLLM service process for one context-window benchmark group."""

    def __init__(
        self,
        *,
        port: int,
        max_model_len: int,
        gpu_memory_utilization: float,
        log_path: Path,
    ) -> None:
        self.port = port
        self.max_model_len = max_model_len
        self.gpu_memory_utilization = gpu_memory_utilization
        self.log_path = log_path
        self.process: subprocess.Popen | None = None
        self._log_file = None

    def start(self) -> None:
        _wait_endpoint_down(self.port, timeout_seconds=120)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_file = self.log_path.open("w", encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "MODEL_DIR": MODEL_DIR,
                "MODEL_PATH": MODEL_DIR,
                "TOKENIZER_PATH": MODEL_DIR,
                "HF_CONFIG_PATH": MODEL_DIR,
                "SERVED_MODEL_NAME": SERVED_MODEL_NAME,
                "PORT": str(self.port),
                "CUDA_VISIBLE_DEVICES": "0,1",
                "TENSOR_PARALLEL_SIZE": "2",
                "MAX_MODEL_LEN": str(self.max_model_len),
                "GPU_MEMORY_UTILIZATION": str(self.gpu_memory_utilization),
                "ENFORCE_EAGER": "1",
            }
        )
        self.process = subprocess.Popen(
            [str(REPO_ROOT / "scripts/start_vllm_qwen_coder_awq.sh")],
            cwd=REPO_ROOT,
            env=env,
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            text=True,
            preexec_fn=os.setsid,
        )

    def wait_ready(self, timeout_seconds: float) -> None:
        started = time.monotonic()
        url = f"http://127.0.0.1:{self.port}/v1/models"
        while time.monotonic() - started < timeout_seconds:
            if self.process and self.process.poll() is not None:
                raise RuntimeError(
                    f"vLLM exited during startup with code {self.process.returncode}; see {self.log_path}"
                )
            try:
                with urlopen(url, timeout=5) as response:
                    if response.status == 200:
                        return
            except (OSError, URLError):
                pass
            time.sleep(2)
        raise TimeoutError(f"vLLM did not become ready within {timeout_seconds}s")

    def stop(self) -> None:
        if not self.process:
            return
        pgid = _process_group_id(self.process)
        if pgid is not None:
            _terminate_process_group(pgid, self.process)
        if self._log_file:
            self._log_file.close()
        _wait_endpoint_down(self.port, timeout_seconds=120)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="data/reports/context")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--warmup-iterations", type=int, default=1)
    parser.add_argument("--startup-timeout-seconds", type=float, default=900)
    parser.add_argument("--request-timeout-seconds", type=float, default=1800)
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.90)
    args = parser.parse_args()

    started_at = datetime.now(timezone.utc).isoformat()
    output_dir = _resolve_path(args.output_dir)
    logs_dir = output_dir / "logs"
    report_path = output_dir / "context_window_benchmark.json"
    chart_path = output_dir / "context_window_benchmark.svg"
    markdown_path = output_dir / "context_window_benchmark.md"

    sections: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for group in DEFAULT_GROUPS:
        max_model_len = int(group["max_model_len"])
        service = ManagedVllmService(
            port=8008,
            max_model_len=max_model_len,
            gpu_memory_utilization=args.gpu_memory_utilization,
            log_path=logs_dir / f"context_benchmark_{max_model_len}.log",
        )
        try:
            service.start()
            service.wait_ready(args.startup_timeout_seconds)
            config = _group_config(
                max_model_len=max_model_len,
                targets=[int(target) for target in group["targets"]],
                iterations=args.iterations,
                warmup_iterations=args.warmup_iterations,
                request_timeout_seconds=args.request_timeout_seconds,
            )
            report = run_system_benchmark(config, repo_root=REPO_ROOT)
            section = {
                "max_model_len": max_model_len,
                "log_path": str(service.log_path.relative_to(REPO_ROOT)),
                "report": report.model_dump(mode="json"),
                "valid": report.valid,
                "summary": report.summary,
            }
            sections.append(section)
            rows.extend(_scenario_rows(section))
        finally:
            service.stop()

    payload = {
        "valid": all(section["valid"] for section in sections),
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "model_dir": MODEL_DIR,
        "iterations": args.iterations,
        "warmup_iterations": args.warmup_iterations,
        "sections": sections,
        "rows": rows,
        "summary": _summary(rows),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    chart_path.write_text(_build_svg_chart(rows), encoding="utf-8")
    markdown_path.write_text(_build_markdown(rows, chart_path, report_path), encoding="utf-8")

    print(
        json.dumps(
            {
                "valid": payload["valid"],
                "output_path": str(report_path.relative_to(REPO_ROOT)),
                "chart_path": str(chart_path.relative_to(REPO_ROOT)),
                "markdown_path": str(markdown_path.relative_to(REPO_ROOT)),
                "summary": payload["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if payload["valid"] else 2


def _group_config(
    *,
    max_model_len: int,
    targets: list[int],
    iterations: int,
    warmup_iterations: int,
    request_timeout_seconds: float,
) -> dict[str, Any]:
    scenarios = []
    for target in targets:
        label = f"{int(round(target / 1000))}k"
        scenarios.append(
            {
                "name": f"context_window_{label}",
                "command": [
                    "{python}",
                    "scripts/benchmark_vllm_chat.py",
                    "--base-url",
                    "http://127.0.0.1:8008/v1",
                    "--model",
                    SERVED_MODEL_NAME,
                    "--target-input-tokens",
                    str(target),
                    "--cache-mode",
                    "cold",
                    "--max-tokens",
                    "32",
                    "--min-tokens",
                    "16",
                    "--timeout-seconds",
                    str(request_timeout_seconds),
                ],
                "timeout_seconds": request_timeout_seconds + 60,
                "thresholds": {
                    "success_rate_min": 1.0,
                    "stdout_tokens_per_second_p50_min": 0.01,
                },
            }
        )
    return {
        "workload": f"context_window_benchmark_{max_model_len}",
        "iterations": iterations,
        "warmup_iterations": warmup_iterations,
        "metrics_urls": ["http://127.0.0.1:8008/metrics"],
        "scenarios": scenarios,
    }


def _scenario_rows(section: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    report = section["report"]
    for scenario in report.get("scenarios") or []:
        metrics = scenario.get("metrics") or {}
        prompt_tokens = float(metrics.get("stdout_prompt_tokens_p95") or 0.0)
        ttft = float(metrics.get("stdout_ttft_seconds_p95") or 0.0)
        prefill_tokens_per_second = round(prompt_tokens / ttft, 6) if ttft > 0 else 0.0
        rows.append(
            {
                "name": scenario.get("name"),
                "max_model_len": section["max_model_len"],
                "log_path": section["log_path"],
                "valid": scenario.get("valid"),
                "iterations": scenario.get("iterations"),
                "warmup_iterations": scenario.get("warmup_iterations"),
                "prompt_tokens_p95": prompt_tokens,
                "target_input_tokens_p95": float(metrics.get("stdout_target_input_tokens_p95") or 0.0),
                "ttft_seconds_p95": ttft,
                "duration_seconds_p95": float(metrics.get("stdout_duration_seconds_p95") or 0.0),
                "tpot_seconds_p95": float(metrics.get("stdout_tpot_seconds_p95") or 0.0),
                "tokens_per_second_p50": float(metrics.get("stdout_tokens_per_second_p50") or 0.0),
                "prefill_tokens_per_second_p95derived": prefill_tokens_per_second,
                "completion_tokens_p95": float(metrics.get("stdout_completion_tokens_p95") or 0.0),
                "success_rate": float(metrics.get("success_rate") or 0.0),
                "quality_warnings": scenario.get("quality_warnings") or [],
            }
        )
    return rows


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [row for row in rows if row.get("valid")]
    max_prompt = max((float(row.get("prompt_tokens_p95") or 0.0) for row in successful), default=0.0)
    max_target = max((float(row.get("target_input_tokens_p95") or 0.0) for row in successful), default=0.0)
    max_ttft = max((float(row.get("ttft_seconds_p95") or 0.0) for row in successful), default=0.0)
    return {
        "rows": len(rows),
        "successful_rows": len(successful),
        "max_prompt_tokens_p95": round(max_prompt, 6),
        "max_target_input_tokens_p95": round(max_target, 6),
        "max_ttft_seconds_p95": round(max_ttft, 6),
    }


def _build_markdown(rows: list[dict[str, Any]], chart_path: Path, report_path: Path) -> str:
    lines = [
        "# Context Window Benchmark",
        "",
        f"- Report: `{report_path.relative_to(REPO_ROOT)}`",
        f"- Chart: `{chart_path.relative_to(REPO_ROOT)}`",
        "",
        f"![Context Window Benchmark]({chart_path.name})",
        "",
        "| Scenario | Target Input P95 | Prompt Tokens P95 | Max Model Len | TTFT P95 (s) | Prefill Tok/s | Duration P95 (s) | TPOT P95 (s) | Decode Tok/s P50 | Success |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in sorted(rows, key=lambda item: float(item.get("target_input_tokens_p95") or 0.0)):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.get("name") or ""),
                    _fmt_float(row.get("target_input_tokens_p95"), digits=0),
                    _fmt_float(row.get("prompt_tokens_p95"), digits=0),
                    _fmt_float(row.get("max_model_len"), digits=0),
                    _fmt_float(row.get("ttft_seconds_p95")),
                    _fmt_float(row.get("prefill_tokens_per_second_p95derived")),
                    _fmt_float(row.get("duration_seconds_p95")),
                    _fmt_float(row.get("tpot_seconds_p95")),
                    _fmt_float(row.get("tokens_per_second_p50")),
                    _fmt_float(row.get("success_rate")),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _build_svg_chart(rows: list[dict[str, Any]]) -> str:
    points = sorted(rows, key=lambda item: float(item.get("target_input_tokens_p95") or 0.0))
    width = 980
    height = 680
    left = 90
    right = 40
    top = 50
    panel_gap = 60
    panel_height = 220
    panel_width = width - left - right
    first_top = top
    second_top = top + panel_height + panel_gap

    max_x = max((float(point.get("target_input_tokens_p95") or 0.0) for point in points), default=1.0)
    max_ttft = max((float(point.get("ttft_seconds_p95") or 0.0) for point in points), default=1.0)
    max_prefill = max((float(point.get("prefill_tokens_per_second_p95derived") or 0.0) for point in points), default=1.0)

    def x_pos(value: float) -> float:
        return left + (value / max_x) * panel_width

    def panel_y(top_offset: float, value: float, max_value: float) -> float:
        return top_offset + panel_height - (value / max_value) * panel_height

    grid = []
    for panel_top, max_value, label in (
        (first_top, max_ttft, "TTFT P95 (s)"),
        (second_top, max_prefill, "Prefill Tokens/s"),
    ):
        for index in range(6):
            tick = max_value * index / 5
            y = panel_y(panel_top, tick, max_value)
            grid.append(
                f'<line x1="{left}" y1="{y:.2f}" x2="{width-right}" y2="{y:.2f}" stroke="#d9e2ec" stroke-width="1"/>'
            )
            grid.append(
                f'<text x="{left-12}" y="{y+4:.2f}" text-anchor="end" font-size="12" fill="#334e68">{tick:.1f}</text>'
            )
        grid.append(
            f'<text x="{left}" y="{panel_top-12}" font-size="14" font-weight="600" fill="#102a43">{label}</text>'
        )

    x_labels = []
    for point in points:
        x = x_pos(float(point.get("target_input_tokens_p95") or 0.0))
        label = f"{int(round(float(point.get('target_input_tokens_p95') or 0.0) / 1000))}k"
        x_labels.append(
            f'<text x="{x:.2f}" y="{height-28}" text-anchor="middle" font-size="12" fill="#334e68">{label}</text>'
        )

    ttft_path = _polyline(
        points,
        key="ttft_seconds_p95",
        x_fn=x_pos,
        y_fn=lambda value: panel_y(first_top, value, max_ttft),
    )
    prefill_path = _polyline(
        points,
        key="prefill_tokens_per_second_p95derived",
        x_fn=x_pos,
        y_fn=lambda value: panel_y(second_top, value, max_prefill),
    )

    dots = []
    for point in points:
        x = x_pos(float(point.get("target_input_tokens_p95") or 0.0))
        dots.append(
            f'<circle cx="{x:.2f}" cy="{panel_y(first_top, float(point.get("ttft_seconds_p95") or 0.0), max_ttft):.2f}" r="4" fill="#1f77b4"/>'
        )
        dots.append(
            f'<circle cx="{x:.2f}" cy="{panel_y(second_top, float(point.get("prefill_tokens_per_second_p95derived") or 0.0), max_prefill):.2f}" r="4" fill="#2ca02c"/>'
        )

    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
            '<rect width="100%" height="100%" fill="#f8fbff"/>',
            '<text x="40" y="28" font-size="22" font-weight="700" fill="#102a43">Rigorous Context Window Benchmark</text>',
            '<text x="40" y="46" font-size="12" fill="#486581">Each scenario uses 1 warmup + 3 measured samples with cold-cache prompts.</text>',
            *grid,
            f'<line x1="{left}" y1="{first_top}" x2="{left}" y2="{first_top+panel_height}" stroke="#243b53" stroke-width="1.5"/>',
            f'<line x1="{left}" y1="{first_top+panel_height}" x2="{width-right}" y2="{first_top+panel_height}" stroke="#243b53" stroke-width="1.5"/>',
            f'<line x1="{left}" y1="{second_top}" x2="{left}" y2="{second_top+panel_height}" stroke="#243b53" stroke-width="1.5"/>',
            f'<line x1="{left}" y1="{second_top+panel_height}" x2="{width-right}" y2="{second_top+panel_height}" stroke="#243b53" stroke-width="1.5"/>',
            f'<path d="{ttft_path}" fill="none" stroke="#1f77b4" stroke-width="3"/>',
            f'<path d="{prefill_path}" fill="none" stroke="#2ca02c" stroke-width="3"/>',
            *dots,
            *x_labels,
            f'<text x="{width/2:.2f}" y="{height-6}" text-anchor="middle" font-size="13" fill="#102a43">Target Input Tokens</text>',
            "</svg>",
        ]
    )


def _polyline(points: list[dict[str, Any]], *, key: str, x_fn, y_fn) -> str:
    parts: list[str] = []
    for index, point in enumerate(points):
        x = x_fn(float(point.get("target_input_tokens_p95") or 0.0))
        y = y_fn(float(point.get(key) or 0.0))
        parts.append(("M" if index == 0 else "L") + f" {x:.2f} {y:.2f}")
    return " ".join(parts)


def _fmt_float(value: Any, digits: int = 3) -> str:
    if not isinstance(value, int | float):
        return ""
    return f"{float(value):.{digits}f}"


def _resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


def _process_group_id(process: subprocess.Popen) -> int | None:
    try:
        return os.getpgid(process.pid)
    except ProcessLookupError:
        return None


def _terminate_process_group(pgid: int, process: subprocess.Popen) -> None:
    _signal_process_group(pgid, signal.SIGTERM)
    try:
        process.wait(timeout=45)
    except subprocess.TimeoutExpired:
        _signal_process_group(pgid, signal.SIGKILL)
        process.wait(timeout=15)
    if _process_group_alive(pgid):
        _signal_process_group(pgid, signal.SIGTERM)
        _wait_process_group_exit(pgid, timeout_seconds=15)
    if _process_group_alive(pgid):
        _signal_process_group(pgid, signal.SIGKILL)
        _wait_process_group_exit(pgid, timeout_seconds=15)


def _signal_process_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def _process_group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def _wait_process_group_exit(pgid: int, timeout_seconds: float) -> None:
    started = time.monotonic()
    while time.monotonic() - started < timeout_seconds:
        if not _process_group_alive(pgid):
            return
        time.sleep(0.5)


def _wait_endpoint_down(port: int, timeout_seconds: float) -> None:
    url = f"http://127.0.0.1:{port}/v1/models"
    started = time.monotonic()
    while time.monotonic() - started < timeout_seconds:
        try:
            with urlopen(url, timeout=2):
                time.sleep(2)
                continue
        except (OSError, URLError, TimeoutError):
            return
    raise TimeoutError(f"Port {port} still responds after {timeout_seconds}s")


if __name__ == "__main__":
    raise SystemExit(main())
