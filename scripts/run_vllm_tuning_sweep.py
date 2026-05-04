#!/usr/bin/env python3
"""Run a small vLLM tuning sweep against the real workload replay set."""

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

from diagnostic_platform.evaluation.replay import default_replay_output_path  # noqa: E402
from diagnostic_platform.orchestration.workload import load_workload_manifest  # noqa: E402


MODEL_DIR = "/home/xdu/huggingface/cyankiwi-Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit"
SERVED_MODEL_NAME = "qwen-audit-resolver"

DEFAULT_MATRIX = [
    {
        "name": "tp2_baseline",
        "env": {},
    },
    {
        "name": "tp2_prefix_cache",
        "env": {"ENABLE_PREFIX_CACHING": "1"},
    },
    {
        "name": "tp2_chunked_prefill",
        "env": {"ENABLE_CHUNKED_PREFILL": "1"},
    },
    {
        "name": "tp2_prefix_chunked",
        "env": {
            "ENABLE_PREFIX_CACHING": "1",
            "ENABLE_CHUNKED_PREFILL": "1",
        },
    },
    {
        "name": "tp2_prefix_chunked_batched",
        "env": {
            "ENABLE_PREFIX_CACHING": "1",
            "ENABLE_CHUNKED_PREFILL": "1",
            "MAX_NUM_SEQS": "32",
            "MAX_NUM_BATCHED_TOKENS": "16384",
        },
    },
]


class ManagedVllmService:
    """A vLLM service process for tuning runs."""

    def __init__(
        self,
        *,
        port: int,
        cuda_visible_devices: str,
        tensor_parallel_size: int,
        max_model_len: int,
        gpu_memory_utilization: float,
        log_path: Path,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self.port = port
        self.cuda_visible_devices = cuda_visible_devices
        self.tensor_parallel_size = tensor_parallel_size
        self.max_model_len = max_model_len
        self.gpu_memory_utilization = gpu_memory_utilization
        self.log_path = log_path
        self.extra_env = extra_env or {}
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
                "CUDA_VISIBLE_DEVICES": self.cuda_visible_devices,
                "TENSOR_PARALLEL_SIZE": str(self.tensor_parallel_size),
                "MAX_MODEL_LEN": str(self.max_model_len),
                "GPU_MEMORY_UTILIZATION": str(self.gpu_memory_utilization),
                "ENFORCE_EAGER": "1",
            }
        )
        env.update(self.extra_env)
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
    parser.add_argument("--workload", default="configs/workloads/treg_20260402.json")
    parser.add_argument("--dataset-path")
    parser.add_argument("--output-path", default="data/reports/tuning/treg_20260402_vllm_tuning_sweep.json")
    parser.add_argument("--logs-dir", default="data/reports/tuning/logs")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("--startup-timeout-seconds", type=float, default=900)
    args = parser.parse_args()

    manifest_path = _resolve_path(args.workload)
    manifest = load_workload_manifest(manifest_path)
    dataset_path = _resolve_path(
        args.dataset_path
        or manifest.paths.get("workload_replay_dataset")
        or str(default_replay_output_path(manifest.workload))
    )
    if not dataset_path.exists():
        raise FileNotFoundError(
            f"Replay dataset not found: {dataset_path}. Run scripts/build_workload_replay.py first."
        )

    output_path = _resolve_path(args.output_path)
    logs_dir = _resolve_path(args.logs_dir)
    report: dict[str, Any] = {
        "valid": True,
        "workload": manifest.workload,
        "dataset_path": str(dataset_path.relative_to(REPO_ROOT)),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "scenarios": [],
    }

    for scenario in DEFAULT_MATRIX:
        service = ManagedVllmService(
            port=8008,
            cuda_visible_devices="0,1",
            tensor_parallel_size=2,
            max_model_len=16384,
            gpu_memory_utilization=0.86,
            log_path=logs_dir / f"{scenario['name']}.log",
            extra_env=scenario["env"],
        )
        try:
            service.start()
            service.wait_ready(args.startup_timeout_seconds)
            benchmark = _run_replay_benchmark(
                dataset_path=dataset_path,
                concurrency=args.concurrency,
                timeout_seconds=args.timeout_seconds,
            )
            report["scenarios"].append(
                {
                    "name": scenario["name"],
                    "env": scenario["env"],
                    "valid": bool(benchmark.get("valid")),
                    "log_path": str((logs_dir / f"{scenario['name']}.log").relative_to(REPO_ROOT)),
                    "benchmark": benchmark,
                }
            )
        finally:
            service.stop()

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["valid"] = all(bool(scenario.get("valid")) for scenario in report["scenarios"])
    report["ranking"] = _rank_scenarios(report["scenarios"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"valid": report["valid"], "output_path": str(output_path.relative_to(REPO_ROOT)), "ranking": report["ranking"]}, ensure_ascii=False, indent=2))
    return 0 if report["valid"] else 2


def _run_replay_benchmark(*, dataset_path: Path, concurrency: int, timeout_seconds: float) -> dict[str, Any]:
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts/benchmark_vllm_replay.py"),
        "--dataset-path",
        str(dataset_path),
        "--base-url",
        "http://127.0.0.1:8008/v1",
        "--concurrency",
        str(concurrency),
        "--timeout-seconds",
        str(timeout_seconds),
    ]
    completed = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    payload = _parse_json(completed.stdout)
    if payload is None:
        payload = {"valid": False, "error": "benchmark_stdout_not_json", "stdout": completed.stdout[-2000:]}
    payload["return_code"] = completed.returncode
    payload["stderr_tail"] = completed.stderr[-2000:]
    return payload


def _rank_scenarios(scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = sorted(
        scenarios,
        key=lambda item: float((item.get("benchmark") or {}).get("requests_per_second") or 0.0),
        reverse=True,
    )
    return [
        {
            "name": item["name"],
            "requests_per_second": round(float((item.get("benchmark") or {}).get("requests_per_second") or 0.0), 6),
            "ttft_seconds_p95": round(float((item.get("benchmark") or {}).get("ttft_seconds_p95") or 0.0), 6),
            "tokens_per_second_mean": round(
                float((item.get("benchmark") or {}).get("tokens_per_second_mean") or 0.0),
                6,
            ),
        }
        for item in ranked
    ]


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
