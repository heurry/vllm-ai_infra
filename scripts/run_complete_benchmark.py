#!/usr/bin/env python3
"""Run the complete benchmark matrix, including vLLM topology changes."""

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


class ManagedVllmService:
    """A vLLM service process started for benchmark topology runs."""

    def __init__(
        self,
        *,
        name: str,
        port: int,
        cuda_visible_devices: str,
        tensor_parallel_size: int,
        max_model_len: int,
        gpu_memory_utilization: float,
        log_path: Path,
    ) -> None:
        self.name = name
        self.port = port
        self.cuda_visible_devices = cuda_visible_devices
        self.tensor_parallel_size = tensor_parallel_size
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
                "CUDA_VISIBLE_DEVICES": self.cuda_visible_devices,
                "TENSOR_PARALLEL_SIZE": str(self.tensor_parallel_size),
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
                    f"{self.name} exited during startup with code {self.process.returncode}; "
                    f"see {self.log_path}"
                )
            try:
                with urlopen(url, timeout=5) as response:
                    if response.status == 200:
                        return
            except (OSError, URLError):
                pass
            time.sleep(2)
        raise TimeoutError(f"{self.name} did not become ready within {timeout_seconds}s")

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
    parser.add_argument(
        "--output-path",
        default="data/reports/benchmark/treg_20260402_complete_benchmark.json",
    )
    parser.add_argument("--logs-dir", default="data/reports/benchmark/logs")
    parser.add_argument("--startup-timeout-seconds", type=float, default=900)
    parser.add_argument("--skip-topology", action="store_true")
    parser.add_argument("--no-stop-existing", action="store_true")
    parser.add_argument("--keep-final-tp2", action="store_true")
    args = parser.parse_args()

    report: dict[str, Any] = {
        "valid": True,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "workload": "treg_20260402_complete_benchmark",
        "sections": [],
    }
    output_path = REPO_ROOT / args.output_path
    logs_dir = REPO_ROOT / args.logs_dir

    if not args.no_stop_existing:
        _stop_existing_project_vllm()

    _append_section(
        report,
        "system_no_vllm",
        _run_config(
            config_path=REPO_ROOT / "configs/benchmark/treg_20260402_system.json",
            output_path=REPO_ROOT / "data/reports/benchmark/treg_20260402_system_benchmark.json",
        ),
    )

    tp2 = ManagedVllmService(
        name="tp2_long_context",
        port=8008,
        cuda_visible_devices="0,1",
        tensor_parallel_size=2,
        max_model_len=16384,
        gpu_memory_utilization=0.86,
        log_path=logs_dir / "vllm_tp2_long_context.log",
    )
    try:
        tp2.start()
        tp2.wait_ready(args.startup_timeout_seconds)
        _append_section(
            report,
            "vllm_tp2_expansion",
            _run_config(
                config_path=REPO_ROOT / "configs/benchmark/treg_20260402_vllm_expansion.json",
                output_path=REPO_ROOT / "data/reports/benchmark/treg_20260402_vllm_expansion_benchmark.json",
            ),
        )
        _append_section(
            report,
            "topology_tp2",
            _run_topology_scenario(
                name="vllm_topology_tp2",
                base_urls=["http://127.0.0.1:8008/v1"],
                concurrency=4,
                requests=4,
            ),
        )
    finally:
        if not args.keep_final_tp2:
            tp2.stop()

    if not args.skip_topology:
        _run_single_gpu_topology(report, logs_dir, args.startup_timeout_seconds)
        _run_dual_instance_topology(report, logs_dir, args.startup_timeout_seconds)

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["valid"] = all(section.get("valid") for section in report["sections"])
    report["summary"] = {
        "sections": len(report["sections"]),
        "passed": sum(1 for section in report["sections"] if section.get("valid")),
        "failed": sum(1 for section in report["sections"] if not section.get("valid")),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    payload = {
        "valid": report["valid"],
        "summary": report["summary"],
        "output_path": str(output_path.relative_to(REPO_ROOT)),
        "sections": [
            {
                "name": section["name"],
                "valid": section["valid"],
                "summary": section.get("summary"),
                "output_path": section.get("output_path"),
            }
            for section in report["sections"]
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if report["valid"] else 2


def _run_single_gpu_topology(report: dict[str, Any], logs_dir: Path, startup_timeout: float) -> None:
    service = ManagedVllmService(
        name="single_gpu",
        port=8008,
        cuda_visible_devices="0",
        tensor_parallel_size=1,
        max_model_len=2048,
        gpu_memory_utilization=0.86,
        log_path=logs_dir / "vllm_single_gpu.log",
    )
    try:
        service.start()
        service.wait_ready(startup_timeout)
        _append_section(
            report,
            "topology_single_gpu",
            _run_topology_scenario(
                name="vllm_topology_1gpu",
                base_urls=["http://127.0.0.1:8008/v1"],
                concurrency=4,
                requests=4,
            ),
        )
    finally:
        service.stop()


def _run_dual_instance_topology(report: dict[str, Any], logs_dir: Path, startup_timeout: float) -> None:
    services = [
        ManagedVllmService(
            name="dual_instance_gpu0",
            port=8008,
            cuda_visible_devices="0",
            tensor_parallel_size=1,
            max_model_len=2048,
            gpu_memory_utilization=0.86,
            log_path=logs_dir / "vllm_dual_instance_gpu0.log",
        ),
        ManagedVllmService(
            name="dual_instance_gpu1",
            port=8009,
            cuda_visible_devices="1",
            tensor_parallel_size=1,
            max_model_len=2048,
            gpu_memory_utilization=0.86,
            log_path=logs_dir / "vllm_dual_instance_gpu1.log",
        ),
    ]
    try:
        for service in services:
            service.start()
        for service in services:
            service.wait_ready(startup_timeout)
        _append_section(
            report,
            "topology_dual_instance",
            _run_topology_scenario(
                name="vllm_topology_2inst",
                base_urls=["http://127.0.0.1:8008/v1", "http://127.0.0.1:8009/v1"],
                concurrency=8,
                requests=8,
            ),
        )
    finally:
        for service in reversed(services):
            service.stop()


def _run_config(config_path: Path, output_path: Path) -> dict[str, Any]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    report = run_system_benchmark(config, repo_root=REPO_ROOT)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "valid": report.valid,
        "summary": report.summary,
        "output_path": str(output_path.relative_to(REPO_ROOT)),
        "report": report.model_dump(mode="json"),
    }


def _run_topology_scenario(
    *,
    name: str,
    base_urls: list[str],
    concurrency: int,
    requests: int,
) -> dict[str, Any]:
    command = [
        "{python}",
        "scripts/benchmark_vllm_concurrent.py",
        "--model",
        SERVED_MODEL_NAME,
        "--concurrency",
        str(concurrency),
        "--requests",
        str(requests),
        "--target-input-tokens",
        "512",
        "--cache-mode",
        "cold",
        "--max-tokens",
        "96",
        "--min-tokens",
        "32",
        "--timeout-seconds",
        "300",
    ]
    for base_url in base_urls:
        command.extend(["--base-url", base_url])
    config = {
        "workload": name,
        "iterations": 3,
        "warmup_iterations": 1,
        "metrics_urls": [base_url.replace("/v1", "/metrics") for base_url in base_urls],
        "scenarios": [
            {
                "name": name,
                "command": command,
                "timeout_seconds": 420,
                "thresholds": {
                    "iterations_min": 3,
                    "success_rate_min": 1.0,
                    "stdout_success_rate_p50_min": 1.0,
                    "stdout_requests_per_second_p50_min": 0.01,
                },
            }
        ],
    }
    report = run_system_benchmark(config, repo_root=REPO_ROOT)
    return {
        "valid": report.valid,
        "summary": report.summary,
        "report": report.model_dump(mode="json"),
    }


def _append_section(report: dict[str, Any], name: str, section: dict[str, Any]) -> None:
    section["name"] = name
    report["sections"].append(section)


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

    # vLLM can leave worker children briefly alive after the shell parent exits.
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


def _stop_existing_project_vllm() -> None:
    """Stop prior project vLLM servers that would occupy benchmark ports or GPUs."""

    ps = subprocess.run(
        ["ps", "-eo", "pid=,pgid=,cmd="],
        text=True,
        capture_output=True,
        check=False,
    )
    current_pid = os.getpid()
    target_pgids: set[int] = set()
    for line in ps.stdout.splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) != 3:
            continue
        pid_text, pgid_text, command = parts
        pid = int(pid_text)
        if pid == current_pid:
            continue
        is_project_vllm = "vllm serve" in command and MODEL_DIR in command
        if is_project_vllm:
            target_pgids.add(int(pgid_text))

    for pgid in sorted(target_pgids):
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if target_pgids:
        time.sleep(10)
    for port in (8008, 8009):
        _wait_endpoint_down(port, timeout_seconds=120)


def _wait_endpoint_down(port: int, timeout_seconds: float) -> None:
    """Wait until a local vLLM endpoint no longer accepts model requests."""

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
