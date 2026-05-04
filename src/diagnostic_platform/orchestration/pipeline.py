"""Run configurable end-to-end project pipelines."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field


StepStatus = Literal["passed", "failed", "skipped"]
ProgressCallback = Callable[[dict[str, Any]], None]


class PipelineStepResult(BaseModel):
    """Execution result for one pipeline step."""

    name: str
    status: StepStatus
    command: list[str] = Field(default_factory=list)
    return_code: int | None = None
    duration_seconds: float = 0.0
    stdout: str = ""
    stderr: str = ""
    parsed_stdout: dict[str, Any] | None = None
    message: str = ""


class PipelineRunReport(BaseModel):
    """Machine-readable pipeline run report."""

    valid: bool
    workload: str
    duration_seconds: float
    steps: list[PipelineStepResult] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


def run_pipeline(
    config: dict[str, Any],
    repo_root: Path,
    skip_steps: set[str] | None = None,
    enable_steps: set[str] | None = None,
    progress_callback: ProgressCallback | None = None,
    stream_output: bool = False,
) -> PipelineRunReport:
    """Run pipeline steps from config and stop on required failures."""

    skip_steps = skip_steps or set()
    enable_steps = enable_steps or set()
    workload = str(config.get("workload") or "default")
    steps_config = list(config.get("steps") or [])
    results: list[PipelineStepResult] = []
    started = time.monotonic()

    total_steps = len(steps_config)
    for step_index, raw_step in enumerate(steps_config, start=1):
        name = str(raw_step.get("name") or "")
        if not name:
            continue

        enabled = bool(raw_step.get("enabled", True)) or name in enable_steps
        required = bool(raw_step.get("required", True))
        if name in skip_steps or not enabled:
            result = PipelineStepResult(
                name=name,
                status="skipped",
                command=_resolve_command(raw_step.get("command") or [], repo_root),
                message="step skipped",
            )
            results.append(result)
            _emit_progress(
                progress_callback,
                "pipeline_step_skipped",
                step_index,
                total_steps,
                result,
            )
            continue

        _emit_progress(
            progress_callback,
            "pipeline_step_start",
            step_index,
            total_steps,
            PipelineStepResult(
                name=name,
                status="skipped",
                command=_resolve_command(raw_step.get("command") or [], repo_root),
            ),
        )
        result = _run_step(raw_step, repo_root, stream_output=stream_output)
        results.append(result)
        _emit_progress(
            progress_callback,
            "pipeline_step_done",
            step_index,
            total_steps,
            result,
        )
        if result.status == "failed" and required:
            break

    duration = round(time.monotonic() - started, 6)
    failed_required = [
        result
        for result, raw_step in zip(results, steps_config, strict=False)
        if result.status == "failed" and bool(raw_step.get("required", True))
    ]
    passed = sum(1 for result in results if result.status == "passed")
    failed = sum(1 for result in results if result.status == "failed")
    skipped = sum(1 for result in results if result.status == "skipped")

    return PipelineRunReport(
        valid=not failed_required,
        workload=workload,
        duration_seconds=duration,
        steps=results,
        summary={
            "steps": len(results),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "failed_required": len(failed_required),
        },
    )


def _run_step(raw_step: dict[str, Any], repo_root: Path, stream_output: bool = False) -> PipelineStepResult:
    name = str(raw_step.get("name"))
    command = _resolve_command(raw_step.get("command") or [], repo_root)
    timeout_seconds = raw_step.get("timeout_seconds")
    started = time.monotonic()
    if stream_output:
        return _run_step_streaming(
            name=name,
            command=command,
            repo_root=repo_root,
            timeout_seconds=float(timeout_seconds) if timeout_seconds else None,
            started=started,
        )
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            text=True,
            capture_output=True,
            timeout=float(timeout_seconds) if timeout_seconds else None,
            check=False,
        )
        duration = round(time.monotonic() - started, 6)
        return PipelineStepResult(
            name=name,
            status="passed" if completed.returncode == 0 else "failed",
            command=command,
            return_code=completed.returncode,
            duration_seconds=duration,
            stdout=completed.stdout,
            stderr=completed.stderr,
            parsed_stdout=_parse_json_stdout(completed.stdout),
            message="" if completed.returncode == 0 else "command failed",
        )
    except subprocess.TimeoutExpired as exc:
        duration = round(time.monotonic() - started, 6)
        return PipelineStepResult(
            name=name,
            status="failed",
            command=command,
            return_code=None,
            duration_seconds=duration,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            message=f"command timed out after {timeout_seconds} seconds",
        )


def _run_step_streaming(
    name: str,
    command: list[str],
    repo_root: Path,
    timeout_seconds: float | None,
    started: float,
) -> PipelineStepResult:
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    process = subprocess.Popen(
        command,
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
    )

    def _read_pipe(pipe: Any, sink: list[str]) -> None:
        try:
            for line in iter(pipe.readline, ""):
                sink.append(line)
                print(line, end="", file=sys.stderr, flush=True)
        finally:
            pipe.close()

    threads = [
        threading.Thread(target=_read_pipe, args=(process.stdout, stdout_parts), daemon=True),
        threading.Thread(target=_read_pipe, args=(process.stderr, stderr_parts), daemon=True),
    ]
    for thread in threads:
        thread.start()

    try:
        return_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        return_code = None
        message = f"command timed out after {timeout_seconds:g} seconds" if timeout_seconds else "command timed out"
    else:
        message = "" if return_code == 0 else "command failed"

    for thread in threads:
        thread.join(timeout=5)

    duration = round(time.monotonic() - started, 6)
    return PipelineStepResult(
        name=name,
        status="passed" if return_code == 0 else "failed",
        command=command,
        return_code=return_code,
        duration_seconds=duration,
        stdout="".join(stdout_parts),
        stderr="".join(stderr_parts),
        parsed_stdout=_parse_json_stdout("".join(stdout_parts)),
        message=message,
    )


def _resolve_command(command: list[Any], repo_root: Path) -> list[str]:
    resolved: list[str] = []
    for part in command:
        value = str(part)
        if value == "{python}":
            resolved.append(sys.executable)
        elif value == "{repo_root}":
            resolved.append(str(repo_root))
        else:
            resolved.append(value)
    return resolved


def _parse_json_stdout(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        return None


def _emit_progress(
    callback: ProgressCallback | None,
    event: str,
    step_index: int,
    total_steps: int,
    result: PipelineStepResult,
) -> None:
    if not callback:
        return
    callback(
        {
            "event": event,
            "step_index": step_index,
            "total_steps": total_steps,
            "step_name": result.name,
            "status": result.status,
            "duration_seconds": result.duration_seconds,
            "return_code": result.return_code,
            "message": result.message,
        }
    )
