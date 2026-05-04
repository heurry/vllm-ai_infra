"""vLLM metrics scraping and Prometheus text parsing."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen


METRIC_LINE_RE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>[-+0-9.eE]+)$")
LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="([^"]*)"')


@dataclass
class MetricsSnapshot:
    """A compact metrics snapshot from one endpoint."""

    url: str
    ok: bool
    metrics: dict[str, float] = field(default_factory=dict)
    error: str = ""


def scrape_metrics(url: str, timeout_seconds: float = 5.0) -> MetricsSnapshot:
    """Scrape a Prometheus text endpoint and aggregate samples by metric name."""

    try:
        with urlopen(url, timeout=timeout_seconds) as response:
            text = response.read().decode("utf-8", errors="replace")
        return MetricsSnapshot(url=url, ok=True, metrics=parse_prometheus_metrics(text))
    except (OSError, URLError, TimeoutError) as exc:
        return MetricsSnapshot(url=url, ok=False, error=str(exc))


def scrape_many(urls: list[str], timeout_seconds: float = 5.0) -> dict[str, Any]:
    """Scrape many endpoints and return JSON-serializable snapshots."""

    snapshots = [scrape_metrics(url, timeout_seconds=timeout_seconds) for url in urls]
    return {
        "endpoints": len(snapshots),
        "ok": sum(1 for snapshot in snapshots if snapshot.ok),
        "failed": sum(1 for snapshot in snapshots if not snapshot.ok),
        "snapshots": [
            {
                "url": snapshot.url,
                "ok": snapshot.ok,
                "metrics": snapshot.metrics,
                "error": snapshot.error,
            }
            for snapshot in snapshots
        ],
        "aggregate": aggregate_snapshots(snapshots),
    }


def parse_prometheus_metrics(text: str) -> dict[str, float]:
    """Parse Prometheus text and aggregate selected numeric metrics by name."""

    values: dict[str, list[float]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = METRIC_LINE_RE.match(line)
        if not match:
            continue
        metric_name = match.group("name")
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        if _is_noisy_bucket(metric_name, match.group("labels") or ""):
            continue
        values.setdefault(metric_name, []).append(value)

    aggregated: dict[str, float] = {}
    for name, metric_values in values.items():
        aggregated[name] = round(sum(metric_values), 6)
    return aggregated


def aggregate_snapshots(snapshots: list[MetricsSnapshot]) -> dict[str, float]:
    """Aggregate metrics across endpoints."""

    aggregate: dict[str, float] = {}
    for snapshot in snapshots:
        if not snapshot.ok:
            continue
        for name, value in snapshot.metrics.items():
            aggregate[name] = round(aggregate.get(name, 0.0) + value, 6)
    return aggregate


def metric_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, float]:
    """Compute aggregate metric deltas for counter-like metrics."""

    before_metrics = before.get("aggregate") or {}
    after_metrics = after.get("aggregate") or {}
    delta: dict[str, float] = {}
    for name, after_value in after_metrics.items():
        before_value = float(before_metrics.get(name, 0.0))
        delta[name] = round(float(after_value) - before_value, 6)
    return delta


def _is_noisy_bucket(metric_name: str, labels: str) -> bool:
    if metric_name.endswith("_bucket"):
        return True
    if 'le="' in labels:
        return True
    for _, value in LABEL_RE.findall(labels):
        if len(value) > 120:
            return True
    return False

