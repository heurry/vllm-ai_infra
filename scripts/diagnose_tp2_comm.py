#!/usr/bin/env python3
"""Diagnose TP2 communication path from topology and vLLM startup logs."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-path", default="data/reports/benchmark/logs/vllm_tp2_long_context.log")
    parser.add_argument("--topology-path")
    parser.add_argument("--output-path", default="data/reports/infra/treg_20260402_tp2_comm_diagnosis.json")
    args = parser.parse_args()

    log_path = _resolve_path(args.log_path)
    output_path = _resolve_path(args.output_path)
    topology_path = _resolve_path(args.topology_path) if args.topology_path else None
    topo = _load_topology(topology_path)
    topo_text = _strip_ansi(topo["stdout"])
    relation = _parse_gpu_relation(topo_text, "GPU0", "GPU1")
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""

    findings = _collect_findings(relation, topo, log_text, log_path)
    report = {
        "valid": True,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "topology_relation_gpu0_gpu1": relation or "unknown",
        "topology_command": topo["command"],
        "topology_source": topo["source"],
        "topology_return_code": topo["return_code"],
        "log_path": str(log_path.relative_to(REPO_ROOT)) if log_path.exists() else str(log_path),
        "log_exists": log_path.exists(),
        "findings": findings,
        "environment": {
            key: os.environ.get(key, "")
            for key in ("NCCL_P2P_DISABLE", "NCCL_IB_DISABLE", "CUDA_VISIBLE_DEVICES", "VLLM_ALL2ALL_BACKEND")
        },
        "recommendations": _recommendations(relation, findings),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"valid": report["valid"], "output_path": str(output_path.relative_to(REPO_ROOT)), "relation": report["topology_relation_gpu0_gpu1"], "findings": findings}, ensure_ascii=False, indent=2))
    return 0


def _run_command(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    return {
        "command": command,
        "return_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _load_topology(path: Path | None) -> dict[str, Any]:
    if path is not None:
        return {
            "command": ["cat", str(path)],
            "return_code": 0,
            "stdout": path.read_text(encoding="utf-8", errors="replace"),
            "stderr": "",
            "source": "file",
        }
    payload = _run_command(["nvidia-smi", "topo", "-m"])
    payload["source"] = "command"
    return payload


def _parse_gpu_relation(text: str, left: str, right: str) -> str | None:
    header_tokens: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "CPU Affinity" in line and left in line and right in line:
            tokens = raw_line.split()
            if "CPU" in tokens:
                header_tokens = tokens[: tokens.index("CPU")]
            else:
                header_tokens = [token for token in tokens if token.startswith("GPU")]
            continue
        if line.startswith(left):
            row_tokens = raw_line.split()
            if not header_tokens or right not in header_tokens:
                return None
            target_index = header_tokens.index(right) + 1
            if target_index < len(row_tokens):
                return row_tokens[target_index]
    return None


def _collect_findings(
    relation: str | None,
    topo: dict[str, Any],
    log_text: str,
    log_path: Path,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if topo["return_code"] != 0 and not relation and not topo["stdout"].strip():
        findings.append(
            {
                "severity": "warning",
                "code": "TOPOLOGY_COMMAND_FAILED",
                "message": topo["stderr"][-400:] or "nvidia-smi topo -m failed",
            }
        )
    if relation:
        findings.append(
            {
                "severity": "info",
                "code": "GPU_TOPOLOGY_RELATION",
                "message": f"GPU0<->GPU1 relation is {relation}",
            }
        )
        if relation in {"NODE", "SYS"}:
            findings.append(
                {
                    "severity": "warning",
                    "code": "NO_HIGH_SPEED_DIRECT_LINK",
                    "message": f"Topology relation {relation} indicates TP2 traffic is not on a high-speed direct NVLink path.",
                }
            )
    if not log_path.exists():
        findings.append(
            {
                "severity": "warning",
                "code": "TP2_LOG_MISSING",
                "message": "TP2 startup log is missing; custom allreduce diagnosis is incomplete.",
            }
        )
        return findings

    patterns = {
        "CUSTOM_ALLREDUCE_DISABLED": r"Custom allreduce is disabled",
        "P2P_TEST_FAILED": r"P2P .*failed|lack[s]? GPU P2P capability|does not have GPU P2P capability",
        "NCCL_ERROR": r"\bNCCL\b.*(?:WARN|ERROR)",
    }
    for code, pattern in patterns.items():
        match = re.search(pattern, log_text, flags=re.I)
        if match:
            findings.append(
                {
                    "severity": "warning" if code != "NCCL_ERROR" else "error",
                    "code": code,
                    "message": match.group(0),
                }
            )
    if not any(item["code"] == "CUSTOM_ALLREDUCE_DISABLED" for item in findings):
        findings.append(
            {
                "severity": "info",
                "code": "CUSTOM_ALLREDUCE_NOT_DETECTED",
                "message": "Did not find custom allreduce disable warning in the inspected TP2 log.",
            }
        )
    return findings


def _recommendations(relation: str | None, findings: list[dict[str, Any]]) -> list[str]:
    codes = {item["code"] for item in findings}
    recommendations: list[str] = []
    if relation in {"NODE", "SYS"} or "CUSTOM_ALLREDUCE_DISABLED" in codes or "P2P_TEST_FAILED" in codes:
        recommendations.append("短请求优先走 2INST，长上下文再走 TP2，不要把 TP2 作为统一默认拓扑。")
    if "CUSTOM_ALLREDUCE_DISABLED" in codes:
        recommendations.append("后续排查重点放在 GPU P2P 能力、PCIe 拓扑和 custom allreduce 回退路径，而不是先调 prompt 层。")
    if relation in {"NODE", "SYS"}:
        recommendations.append("benchmark 需要分开统计 prefill 和 decode，不要只看总体吞吐，因为 TP2 的通信开销会主要拉高短请求 TTFT。")
    if not recommendations:
        recommendations.append("当前未发现明确的 TP2 通信阻塞证据，但仍建议保留 topology matrix 做回归。")
    return recommendations


def _resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


if __name__ == "__main__":
    raise SystemExit(main())
