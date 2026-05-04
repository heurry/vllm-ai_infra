#!/usr/bin/env python3
"""Run staircase extreme-context trials and generate a summary chart."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import time
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]

MODEL_DIR = "/home/xdu/huggingface/cyankiwi-Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit"
SERVED_MODEL_NAME = "qwen-audit-resolver"
HISTORICAL_BENCHMARK_PATH = REPO_ROOT / "data/reports/benchmark/treg_20260402_complete_benchmark.json"
DEFAULT_GROUPS = [
    {"max_model_len": 49152, "targets": [24000, 32000, 48000]},
    {"max_model_len": 65536, "targets": [64000]},
    {"max_model_len": 98304, "targets": [96000]},
    {"max_model_len": 131072, "targets": [128000]},
]


class ManagedVllmService:
    """A TP2 vLLM service process for one context-window trial group."""

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
    parser.add_argument("--startup-timeout-seconds", type=float, default=900)
    parser.add_argument("--request-timeout-seconds", type=float, default=1800)
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.90)
    parser.add_argument("--skip-historical", action="store_true")
    args = parser.parse_args()

    started_at = datetime.now(timezone.utc).isoformat()
    output_dir = _resolve_path(args.output_dir)
    logs_dir = output_dir / "logs"
    report_path = output_dir / "context_window_staircase.json"
    chart_path = output_dir / "context_window_staircase.svg"
    markdown_path = output_dir / "context_window_staircase.md"

    rows: list[dict[str, Any]] = []
    if not args.skip_historical:
        historical = _load_historical_16k()
        if historical:
            rows.append(historical)

    for group in DEFAULT_GROUPS:
        service = ManagedVllmService(
            port=8008,
            max_model_len=int(group["max_model_len"]),
            gpu_memory_utilization=args.gpu_memory_utilization,
            log_path=logs_dir / f"context_{int(group['max_model_len'])}.log",
        )
        try:
            service.start()
            service.wait_ready(args.startup_timeout_seconds)
            for target in group["targets"]:
                rows.append(
                    _run_trial(
                        target_input_tokens=int(target),
                        service_max_model_len=int(group["max_model_len"]),
                        request_timeout_seconds=args.request_timeout_seconds,
                        log_path=service.log_path,
                    )
                )
        finally:
            service.stop()

    report = {
        "valid": all(bool(row.get("valid")) for row in rows if row.get("source") != "historical_benchmark"),
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "model_dir": MODEL_DIR,
        "rows": rows,
        "summary": _summary(rows),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    chart_path.write_text(_build_svg_chart(rows), encoding="utf-8")
    markdown_path.write_text(_build_markdown(rows, chart_path, report_path), encoding="utf-8")

    print(
        json.dumps(
            {
                "valid": report["valid"],
                "output_path": str(report_path.relative_to(REPO_ROOT)),
                "chart_path": str(chart_path.relative_to(REPO_ROOT)),
                "markdown_path": str(markdown_path.relative_to(REPO_ROOT)),
                "summary": report["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["valid"] else 2


def _run_trial(
    *,
    target_input_tokens: int,
    service_max_model_len: int,
    request_timeout_seconds: float,
    log_path: Path,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat()
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/benchmark_vllm_chat.py"),
            "--base-url",
            "http://127.0.0.1:8008/v1",
            "--model",
            SERVED_MODEL_NAME,
            "--target-input-tokens",
            str(target_input_tokens),
            "--cache-mode",
            "cold",
            "--max-tokens",
            "32",
            "--min-tokens",
            "16",
            "--timeout-seconds",
            str(request_timeout_seconds),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    payload = _parse_json(completed.stdout) or {
        "valid": False,
        "error": "benchmark_stdout_not_json",
        "stdout_tail": completed.stdout[-2000:],
    }
    payload.update(
        {
            "source": "extreme_trial",
            "requested_context_tokens": target_input_tokens,
            "service_max_model_len": service_max_model_len,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "return_code": completed.returncode,
            "log_path": str(log_path.relative_to(REPO_ROOT)),
            "stderr_tail": completed.stderr[-2000:],
        }
    )
    return payload


def _load_historical_16k() -> dict[str, Any] | None:
    if not HISTORICAL_BENCHMARK_PATH.exists():
        return None
    payload = json.loads(HISTORICAL_BENCHMARK_PATH.read_text(encoding="utf-8"))
    for section in payload.get("sections") or []:
        if section.get("name") != "vllm_tp2_expansion":
            continue
        for scenario in (section.get("report") or {}).get("scenarios") or []:
            if scenario.get("name") != "vllm_input_16k":
                continue
            metrics = scenario.get("metrics") or {}
            return {
                "valid": True,
                "source": "historical_benchmark",
                "requested_context_tokens": int(metrics.get("stdout_target_input_tokens_p95") or 15360),
                "service_max_model_len": 16384,
                "prompt_tokens": int(metrics.get("stdout_prompt_tokens_p95") or 0),
                "duration_seconds": float(metrics.get("stdout_duration_seconds_p95") or 0.0),
                "ttft_seconds": float(metrics.get("stdout_ttft_seconds_p95") or 0.0),
                "tpot_seconds": float(metrics.get("stdout_tpot_seconds_p95") or 0.0),
                "tokens_per_second": float(metrics.get("stdout_tokens_per_second_p50") or 0.0),
                "completion_tokens": int(metrics.get("stdout_completion_tokens_p95") or 0),
                "finish_reason": "historical_p95",
                "label": "16k historical benchmark",
                "log_path": "data/reports/benchmark/treg_20260402_complete_benchmark.json",
            }
    return None


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [row for row in rows if row.get("valid")]
    successful_trials = [row for row in successful if row.get("source") == "extreme_trial"]
    max_success = max((int(row.get("requested_context_tokens") or 0) for row in successful_trials), default=0)
    max_prompt = max((int(row.get("prompt_tokens") or 0) for row in successful), default=0)
    ttft_values = [
        float(row.get("ttft_seconds") or 0.0)
        for row in successful
        if isinstance(row.get("ttft_seconds"), int | float)
    ]
    return {
        "rows": len(rows),
        "successful_rows": len(successful),
        "max_successful_requested_context_tokens": max_success,
        "max_successful_prompt_tokens": max_prompt,
        "max_ttft_seconds": round(max(ttft_values), 6) if ttft_values else 0.0,
        "median_ttft_seconds": round(statistics.median(ttft_values), 6) if ttft_values else 0.0,
    }


def _build_markdown(rows: list[dict[str, Any]], chart_path: Path, report_path: Path) -> str:
    lines = [
        "# Context Window Staircase",
        "",
        f"- Report: `{report_path.relative_to(REPO_ROOT)}`",
        f"- Chart: `{chart_path.relative_to(REPO_ROOT)}`",
        "",
        f"![Context Window Staircase]({chart_path.name})",
        "",
        "| Source | Requested Context | Prompt Tokens | Max Model Len | Valid | TTFT (s) | Duration (s) | TPOT (s) | Tokens/s | Finish |",
        "| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in sorted(rows, key=lambda item: int(item.get("requested_context_tokens") or 0)):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.get("source") or ""),
                    _fmt_int(row.get("requested_context_tokens")),
                    _fmt_int(row.get("prompt_tokens")),
                    _fmt_int(row.get("service_max_model_len")),
                    "yes" if row.get("valid") else "no",
                    _fmt_float(row.get("ttft_seconds")),
                    _fmt_float(row.get("duration_seconds")),
                    _fmt_float(row.get("tpot_seconds")),
                    _fmt_float(row.get("tokens_per_second")),
                    str(row.get("finish_reason") or ""),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _build_svg_chart(rows: list[dict[str, Any]]) -> str:
    points = [
        row
        for row in sorted(rows, key=lambda item: int(item.get("requested_context_tokens") or 0))
        if row.get("valid")
        and isinstance(row.get("ttft_seconds"), int | float)
        and isinstance(row.get("duration_seconds"), int | float)
    ]
    width = 920
    height = 520
    left = 80
    right = 30
    top = 50
    bottom = 70
    plot_width = width - left - right
    plot_height = height - top - bottom
    max_x = max((int(point.get("requested_context_tokens") or 0) for point in points), default=1)
    max_y = max(
        max(float(point.get("ttft_seconds") or 0.0), float(point.get("duration_seconds") or 0.0))
        for point in points
    ) if points else 1.0
    max_y = max(max_y, 1.0)

    def x_pos(value: int) -> float:
        return left + (value / max_x) * plot_width

    def y_pos(value: float) -> float:
        return top + plot_height - (value / max_y) * plot_height

    grid_lines = []
    for index in range(6):
        y_value = max_y * index / 5
        y = y_pos(y_value)
        grid_lines.append(
            f'<line x1="{left}" y1="{y:.2f}" x2="{width-right}" y2="{y:.2f}" stroke="#d9e2ec" stroke-width="1"/>'
        )
        grid_lines.append(
            f'<text x="{left-10}" y="{y+4:.2f}" text-anchor="end" font-size="12" fill="#334e68">{y_value:.1f}s</text>'
        )

    x_labels = []
    for point in points:
        x = x_pos(int(point.get("requested_context_tokens") or 0))
        label = f"{int(round(int(point.get('requested_context_tokens') or 0) / 1000))}k"
        x_labels.append(
            f'<text x="{x:.2f}" y="{height-bottom+25}" text-anchor="middle" font-size="12" fill="#334e68">{label}</text>'
        )

    ttft_path = _polyline(points, key="ttft_seconds", x_fn=x_pos, y_fn=y_pos)
    duration_path = _polyline(points, key="duration_seconds", x_fn=x_pos, y_fn=y_pos)
    dots = []
    for point in points:
        x = x_pos(int(point.get("requested_context_tokens") or 0))
        dots.append(
            f'<circle cx="{x:.2f}" cy="{y_pos(float(point.get("ttft_seconds") or 0.0)):.2f}" r="4" fill="#1f77b4"/>'
        )
        dots.append(
            f'<circle cx="{x:.2f}" cy="{y_pos(float(point.get("duration_seconds") or 0.0)):.2f}" r="4" fill="#d62728"/>'
        )

    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
            '<rect width="100%" height="100%" fill="#f8fbff"/>',
            '<text x="40" y="28" font-size="22" font-weight="700" fill="#102a43">Context Window Staircase</text>',
            '<text x="40" y="46" font-size="12" fill="#486581">Historical 16k baseline + new 24k/32k/48k/64k/96k/128k trials</text>',
            *grid_lines,
            f'<line x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}" stroke="#243b53" stroke-width="1.5"/>',
            f'<line x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}" stroke="#243b53" stroke-width="1.5"/>',
            f'<path d="{ttft_path}" fill="none" stroke="#1f77b4" stroke-width="3"/>',
            f'<path d="{duration_path}" fill="none" stroke="#d62728" stroke-width="3"/>',
            *dots,
            *x_labels,
            f'<text x="{width/2:.2f}" y="{height-20}" text-anchor="middle" font-size="13" fill="#102a43">Requested Context Tokens</text>',
            f'<text transform="translate(24 {height/2:.2f}) rotate(-90)" text-anchor="middle" font-size="13" fill="#102a43">Seconds</text>',
            '<rect x="640" y="62" width="16" height="4" fill="#1f77b4"/>',
            '<text x="664" y="67" font-size="12" fill="#102a43">TTFT</text>',
            '<rect x="740" y="62" width="16" height="4" fill="#d62728"/>',
            '<text x="764" y="67" font-size="12" fill="#102a43">Total Duration</text>',
            "</svg>",
        ]
    )


def _polyline(points: list[dict[str, Any]], *, key: str, x_fn, y_fn) -> str:
    path_parts: list[str] = []
    for index, point in enumerate(points):
        x = x_fn(int(point.get("requested_context_tokens") or 0))
        y = y_fn(float(point.get(key) or 0.0))
        path_parts.append(("M" if index == 0 else "L") + f" {x:.2f} {y:.2f}")
    return " ".join(path_parts)


def _parse_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


def _fmt_float(value: Any) -> str:
    if not isinstance(value, int | float):
        return ""
    return f"{float(value):.3f}"


def _fmt_int(value: Any) -> str:
    if not isinstance(value, int | float):
        return ""
    return str(int(value))


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
