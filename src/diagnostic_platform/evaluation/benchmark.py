"""System benchmark runner for repeatable project workflows."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time
from typing import Any, Literal
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from diagnostic_platform.serving.metrics import metric_delta, scrape_many


SampleStatus = Literal["passed", "failed", "timeout"]


class BenchmarkSample(BaseModel):
    """One measured command execution."""

    iteration: int
    status: SampleStatus
    duration_seconds: float
    return_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    parsed_stdout: dict[str, Any] | None = None


class BenchmarkScenarioReport(BaseModel):
    """Metrics for one benchmark scenario."""

    name: str
    valid: bool
    command: list[str]
    iterations: int
    warmup_iterations: int = 0
    samples: list[BenchmarkSample] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    quality_warnings: list[str] = Field(default_factory=list)
    observability: dict[str, Any] = Field(default_factory=dict)


class SystemBenchmarkReport(BaseModel):
    """Full benchmark report."""

    valid: bool
    workload: str
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float
    scenarios: list[BenchmarkScenarioReport] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


def run_system_benchmark(config: dict[str, Any], repo_root: Path) -> SystemBenchmarkReport:
    """Run benchmark scenarios from config."""

    started_at = datetime.now(timezone.utc)
    started = time.monotonic()
    workload = str(config.get("workload") or "default")
    default_iterations = int(config.get("iterations", 1))
    default_warmup = int(config.get("warmup_iterations", 0))
    default_metrics_urls = config.get("metrics_urls") or []
    scenarios: list[BenchmarkScenarioReport] = []

    for raw_scenario in config.get("scenarios") or []:
        if not raw_scenario.get("enabled", True):
            continue
        scenario_config = dict(raw_scenario)
        scenario_config.setdefault("metrics_urls", default_metrics_urls)
        scenarios.append(
            _run_scenario(
                raw_scenario=scenario_config,
                repo_root=repo_root,
                default_iterations=default_iterations,
                default_warmup=default_warmup,
            )
        )

    finished_at = datetime.now(timezone.utc)
    duration = round(time.monotonic() - started, 6)
    failed = sum(1 for scenario in scenarios if not scenario.valid)
    return SystemBenchmarkReport(
        valid=failed == 0,
        workload=workload,
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
        duration_seconds=duration,
        scenarios=scenarios,
        summary={
            "scenarios": len(scenarios),
            "passed": len(scenarios) - failed,
            "failed": failed,
            "samples": sum(len(scenario.samples) for scenario in scenarios),
        },
    )


def _run_scenario(
    raw_scenario: dict[str, Any],
    repo_root: Path,
    default_iterations: int,
    default_warmup: int,
) -> BenchmarkScenarioReport:
    name = str(raw_scenario.get("name") or "scenario")
    command = _resolve_command(raw_scenario.get("command") or [], repo_root)
    iterations = int(raw_scenario.get("iterations", default_iterations))
    warmup_iterations = int(raw_scenario.get("warmup_iterations", default_warmup))
    timeout_seconds = raw_scenario.get("timeout_seconds")
    metrics_urls = [str(url) for url in raw_scenario.get("metrics_urls") or []]

    for _ in range(warmup_iterations):
        _run_command(command, repo_root, timeout_seconds, scenario=name)

    metrics_before = scrape_many(metrics_urls) if metrics_urls else {}
    samples = [
        _run_command(command, repo_root, timeout_seconds, iteration=index, scenario=name)
        for index in range(1, iterations + 1)
    ]
    metrics_after = scrape_many(metrics_urls) if metrics_urls else {}
    metrics = _compute_metrics(samples)
    observability = _observability(metrics_before, metrics_after)
    metrics.update(_observability_metrics(observability))
    checks = _check_thresholds(metrics, raw_scenario.get("thresholds") or {})
    quality_warnings = _quality_warnings(
        samples=samples,
        warmup_iterations=warmup_iterations,
        min_iterations_for_stats=int(raw_scenario.get("min_iterations_for_stats", 3)),
    )
    has_command_failure = any(sample.status != "passed" for sample in samples)
    return BenchmarkScenarioReport(
        name=name,
        valid=not has_command_failure and not any(not check["passed"] for check in checks),
        command=command,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        samples=samples,
        metrics=metrics,
        checks=checks,
        quality_warnings=quality_warnings,
        observability=observability,
    )


def _run_command(
    command: list[str],
    repo_root: Path,
    timeout_seconds: Any,
    iteration: int = 0,
    scenario: str = "",
) -> BenchmarkSample:
    started = time.monotonic()
    env = os.environ.copy()
    env["BENCHMARK_ITERATION"] = str(iteration)
    env["BENCHMARK_SCENARIO"] = scenario
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            env=env,
            text=True,
            capture_output=True,
            timeout=float(timeout_seconds) if timeout_seconds else None,
            check=False,
        )
        duration = round(time.monotonic() - started, 6)
        return BenchmarkSample(
            iteration=iteration,
            status="passed" if completed.returncode == 0 else "failed",
            duration_seconds=duration,
            return_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            parsed_stdout=_parse_json_stdout(completed.stdout),
        )
    except subprocess.TimeoutExpired as exc:
        duration = round(time.monotonic() - started, 6)
        return BenchmarkSample(
            iteration=iteration,
            status="timeout",
            duration_seconds=duration,
            return_code=None,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
        )


def _compute_metrics(samples: list[BenchmarkSample]) -> dict[str, Any]:
    durations = [sample.duration_seconds for sample in samples]
    passed = sum(1 for sample in samples if sample.status == "passed")
    failed = sum(1 for sample in samples if sample.status == "failed")
    timeout = sum(1 for sample in samples if sample.status == "timeout")
    metrics: dict[str, Any] = {
        "iterations": len(samples),
        "passed": passed,
        "failed": failed,
        "timeout": timeout,
        "success_rate": _safe_ratio(passed, len(samples)),
        "failed_rate": _safe_ratio(failed, len(samples)),
        "timeout_rate": _safe_ratio(timeout, len(samples)),
    }
    if durations:
        metrics.update(_series_metrics("duration", durations, suffix="_seconds"))
    metrics.update(_parsed_stdout_metrics(samples))
    return metrics


def _parsed_stdout_metrics(samples: list[BenchmarkSample]) -> dict[str, Any]:
    values_by_key: dict[str, list[float]] = {}
    for sample in samples:
        payload = sample.parsed_stdout or {}
        for key, value in payload.items():
            if isinstance(value, bool) or not isinstance(value, int | float):
                continue
            values_by_key.setdefault(key, []).append(float(value))

    metrics: dict[str, Any] = {}
    for key, values in sorted(values_by_key.items()):
        if not values:
            continue
        metrics.update(_series_metrics(f"stdout_{key}", values))
    return metrics


def _series_metrics(prefix: str, values: list[float], suffix: str = "") -> dict[str, Any]:
    """Compute stable descriptive metrics for a numeric series."""

    metrics: dict[str, Any] = {
        f"{prefix}_count{suffix}": len(values),
        f"{prefix}_min{suffix}": round(min(values), 6),
        f"{prefix}_max{suffix}": round(max(values), 6),
        f"{prefix}_mean{suffix}": round(statistics.fmean(values), 6),
        f"{prefix}_p50{suffix}": _percentile(values, 50),
        f"{prefix}_p90{suffix}": _percentile(values, 90),
        f"{prefix}_p95{suffix}": _percentile(values, 95),
        f"{prefix}_p99{suffix}": _percentile(values, 99),
    }
    if len(values) > 1:
        stdev = statistics.stdev(values)
        mean = statistics.fmean(values)
        metrics[f"{prefix}_stdev{suffix}"] = round(stdev, 6)
        metrics[f"{prefix}_cv{suffix}"] = round(stdev / mean, 6) if mean else 0.0
    else:
        metrics[f"{prefix}_stdev{suffix}"] = 0.0
        metrics[f"{prefix}_cv{suffix}"] = 0.0
    return metrics


def _observability(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    if not before and not after:
        return {}
    return {
        "metrics_before": before,
        "metrics_after": after,
        "metric_delta": metric_delta(before, after),
    }


def _observability_metrics(observability: dict[str, Any]) -> dict[str, Any]:
    delta = observability.get("metric_delta") or {}
    metrics: dict[str, Any] = {}
    for name, value in delta.items():
        safe_name = _safe_metric_name(name)
        metrics[f"vllm_delta_{safe_name}"] = value
    after_aggregate = ((observability.get("metrics_after") or {}).get("aggregate") or {})
    for name, value in after_aggregate.items():
        safe_name = _safe_metric_name(name)
        metrics[f"vllm_after_{safe_name}"] = value
    return metrics


def _safe_metric_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_")


def _check_thresholds(metrics: dict[str, Any], thresholds: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for key, expected in thresholds.items():
        actual = metrics.get(key)
        if key.endswith("_max"):
            metric_key = key.removesuffix("_max")
            actual = metrics.get(metric_key)
            passed = actual is not None and actual <= expected
            expected_text = f"<= {expected}"
        elif key.endswith("_min"):
            metric_key = key.removesuffix("_min")
            actual = metrics.get(metric_key)
            passed = actual is not None and actual >= expected
            expected_text = f">= {expected}"
        else:
            passed = actual == expected
            expected_text = expected
        checks.append(
            {
                "name": key,
                "passed": bool(passed),
                "actual": actual,
                "expected": expected_text,
            }
        )
    return checks


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


def _quality_warnings(
    *,
    samples: list[BenchmarkSample],
    warmup_iterations: int,
    min_iterations_for_stats: int,
) -> list[str]:
    warnings: list[str] = []
    if len(samples) < min_iterations_for_stats:
        warnings.append(
            f"measured_iterations {len(samples)} is below recommended minimum {min_iterations_for_stats}"
        )
    if warmup_iterations <= 0:
        warnings.append("warmup_iterations is 0; first-sample cold effects may affect metrics")
    if any(sample.status == "timeout" for sample in samples):
        warnings.append("one or more samples timed out")
    if any(sample.status == "failed" for sample in samples):
        warnings.append("one or more samples failed")
    return warnings


def _parse_json_stdout(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        return None


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile / 100 * len(ordered)) - 1))
    return round(ordered[index], 6)


def _safe_ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)
