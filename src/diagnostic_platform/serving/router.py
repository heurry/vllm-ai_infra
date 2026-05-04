"""Profile-aware vLLM endpoint router with runtime health state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import count
import time
from typing import Callable, Literal
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from diagnostic_platform.schemas import VllmModelConfig
from diagnostic_platform.serving.endpoint_pool import VllmEndpoint, VllmEndpointPool
from diagnostic_platform.serving.profiles import LONG_PROFILES, RequestProfile, SHORT_PROFILES


EndpointProbe = Callable[[VllmEndpoint, float], tuple[bool, str, float | None]]
ContextTier = Literal["short", "standard", "extended", "extreme", "overflow"]


@dataclass(frozen=True)
class ContextTierPolicy:
    """Prompt-token thresholds for context-aware routing."""

    short_max_tokens: int = 4096
    standard_max_tokens: int = 32768
    extended_max_tokens: int = 65536
    extreme_max_tokens: int = 131072

    @property
    def max_supported_tokens(self) -> int:
        return self.extreme_max_tokens

    def classify(self, prompt_tokens: int) -> ContextTier:
        if prompt_tokens <= self.short_max_tokens:
            return "short"
        if prompt_tokens <= self.standard_max_tokens:
            return "standard"
        if prompt_tokens <= self.extended_max_tokens:
            return "extended"
        if prompt_tokens <= self.extreme_max_tokens:
            return "extreme"
        return "overflow"


@dataclass
class EndpointHealthState:
    """Mutable health state for one endpoint."""

    consecutive_failures: int = 0
    total_successes: int = 0
    total_failures: int = 0
    circuit_open_until: float = 0.0
    last_error: str = ""
    last_probe_ok: bool | None = None
    last_probe_at: float = 0.0
    last_latency_seconds: float | None = None
    last_success_at: float = 0.0
    last_failure_at: float = 0.0

    def is_circuit_open(self, now: float) -> bool:
        return now < self.circuit_open_until


@dataclass
class RouteTrace:
    """One router decision with runtime outcome."""

    trace_id: str
    profile: RequestProfile
    prompt_tokens: int
    context_tier: ContextTier
    selected_endpoint: str
    candidate_endpoints: list[str] = field(default_factory=list)
    healthy_endpoints: list[str] = field(default_factory=list)
    degraded: bool = False
    fallback_reason: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: str | None = None
    success: bool | None = None
    latency_seconds: float | None = None
    error: str = ""


@dataclass
class VllmRoute:
    """Resolved vLLM route."""

    profile: RequestProfile
    endpoint: VllmEndpoint
    context_tier: ContextTier = "short"
    route_id: str = ""
    candidate_endpoints: tuple[str, ...] = ()
    healthy_endpoints: tuple[str, ...] = ()
    degraded: bool = False
    fallback_reason: str = ""

    def to_model_config(self, base: VllmModelConfig) -> VllmModelConfig:
        """Create a config using the selected endpoint while preserving runtime settings."""

        return base.model_copy(
            update={
                "base_url": self.endpoint.base_url,
                "model": self.endpoint.model or base.model,
            }
        )


@dataclass
class VllmRouter:
    """Select a vLLM endpoint by request profile, health, and prompt length."""

    pool: VllmEndpointPool = field(default_factory=VllmEndpointPool)
    failure_threshold: int = 2
    recovery_cooldown_seconds: float = 30.0
    probe_timeout_seconds: float = 2.0
    probe_interval_seconds: float = 15.0
    trace_limit: int = 256
    enable_probing: bool = False
    probe_func: EndpointProbe | None = None
    context_tier_policy: ContextTierPolicy = field(default_factory=ContextTierPolicy)
    _counter: count = field(default_factory=count, init=False, repr=False)
    _trace_counter: count = field(default_factory=count, init=False, repr=False)
    _health: dict[str, EndpointHealthState] = field(default_factory=dict, init=False, repr=False)
    _traces: list[RouteTrace] = field(default_factory=list, init=False, repr=False)
    _trace_index: dict[str, RouteTrace] = field(default_factory=dict, init=False, repr=False)

    def route(self, profile: RequestProfile = "default", prompt_tokens: int = 0) -> VllmRoute:
        """Select one endpoint using profile, prompt length, and health state."""

        if self.enable_probing:
            self.refresh_health(force=False)

        context_tier = self.classify_context_tier(prompt_tokens)
        candidates = self.pool.candidates(profile, prompt_tokens=prompt_tokens)
        if not candidates:
            raise ValueError(f"No enabled vLLM endpoint for profile={profile!r}, prompt_tokens={prompt_tokens}")

        now = time.monotonic()
        healthy_candidates = [endpoint for endpoint in candidates if self._is_endpoint_routable(endpoint, now)]
        if not healthy_candidates and self.enable_probing:
            self._probe_candidates(candidates)
            now = time.monotonic()
            healthy_candidates = [endpoint for endpoint in candidates if self._is_endpoint_routable(endpoint, now)]
        if not healthy_candidates:
            raise ValueError(
                "No healthy vLLM endpoint available for "
                f"profile={profile!r}, prompt_tokens={prompt_tokens}; "
                f"states={self.health_snapshot()}"
            )

        index = next(self._counter) % len(healthy_candidates)
        selected = healthy_candidates[index]
        candidate_names = [endpoint.name for endpoint in _unique_endpoints(candidates)]
        healthy_names = [endpoint.name for endpoint in _unique_endpoints(healthy_candidates)]
        skipped = [name for name in candidate_names if name not in healthy_names]
        fallback_reason = self._fallback_reason(skipped)
        trace_id = f"route-{next(self._trace_counter):06d}"
        trace = RouteTrace(
            trace_id=trace_id,
            profile=profile,
            prompt_tokens=prompt_tokens,
            context_tier=context_tier,
            selected_endpoint=selected.name,
            candidate_endpoints=candidate_names,
            healthy_endpoints=healthy_names,
            degraded=bool(skipped),
            fallback_reason=fallback_reason,
        )
        self._remember_trace(trace)
        return VllmRoute(
            profile=profile,
            endpoint=selected,
            context_tier=context_tier,
            route_id=trace_id,
            candidate_endpoints=tuple(candidate_names),
            healthy_endpoints=tuple(healthy_names),
            degraded=bool(skipped),
            fallback_reason=fallback_reason,
        )

    def classify_context_tier(self, prompt_tokens: int) -> ContextTier:
        """Classify prompt length into one of the routing tiers."""

        return self.context_tier_policy.classify(prompt_tokens)

    def mark_success(self, route: VllmRoute, latency_seconds: float | None = None) -> None:
        """Record a successful request routed to one endpoint."""

        state = self._state_for(route.endpoint)
        now = time.monotonic()
        state.consecutive_failures = 0
        state.circuit_open_until = 0.0
        state.last_error = ""
        state.total_successes += 1
        state.last_probe_ok = True
        state.last_latency_seconds = latency_seconds
        state.last_success_at = now
        trace = self._trace_index.get(route.route_id)
        if trace is not None:
            trace.success = True
            trace.latency_seconds = latency_seconds
            trace.completed_at = datetime.now(timezone.utc).isoformat()

    def mark_failure(self, route: VllmRoute, error: Exception | str) -> None:
        """Record a failed request and open the circuit after repeated failures."""

        state = self._state_for(route.endpoint)
        now = time.monotonic()
        state.consecutive_failures += 1
        state.total_failures += 1
        state.last_error = str(error)
        state.last_failure_at = now
        state.last_probe_ok = False
        if state.consecutive_failures >= self.failure_threshold:
            state.circuit_open_until = now + self.recovery_cooldown_seconds
        trace = self._trace_index.get(route.route_id)
        if trace is not None:
            trace.success = False
            trace.error = str(error)
            trace.completed_at = datetime.now(timezone.utc).isoformat()

    def refresh_health(self, force: bool = False) -> dict[str, dict[str, float | int | bool | str | None]]:
        """Probe endpoints on an interval and update circuit state."""

        if not self.enable_probing:
            return self.health_snapshot()

        now = time.monotonic()
        for endpoint in self.pool.endpoints:
            state = self._state_for(endpoint)
            if not endpoint.enabled:
                continue
            if not force and state.is_circuit_open(now):
                continue
            if not force and state.last_probe_at and (now - state.last_probe_at) < self.probe_interval_seconds:
                continue
            self._probe_endpoint(endpoint)
        return self.health_snapshot()

    def recent_traces(self, limit: int | None = None) -> list[RouteTrace]:
        """Return recent route traces."""

        traces = self._traces[-limit:] if limit else self._traces
        return [trace for trace in traces]

    def health_snapshot(self) -> dict[str, dict[str, float | int | bool | str | None]]:
        """Return a JSON-friendly health snapshot."""

        now = time.monotonic()
        snapshot: dict[str, dict[str, float | int | bool | str | None]] = {}
        for endpoint in self.pool.endpoints:
            state = self._state_for(endpoint)
            snapshot[endpoint.name] = {
                "base_url": endpoint.base_url,
                "consecutive_failures": state.consecutive_failures,
                "total_successes": state.total_successes,
                "total_failures": state.total_failures,
                "circuit_open": state.is_circuit_open(now),
                "circuit_open_seconds": round(max(state.circuit_open_until - now, 0.0), 6),
                "last_error": state.last_error,
                "last_probe_ok": state.last_probe_ok,
                "last_probe_at": round(state.last_probe_at, 6) if state.last_probe_at else None,
                "last_latency_seconds": state.last_latency_seconds,
            }
        return snapshot

    def _probe_candidates(self, candidates: list[VllmEndpoint]) -> None:
        now = time.monotonic()
        for endpoint in _unique_endpoints(candidates):
            state = self._state_for(endpoint)
            if state.is_circuit_open(now):
                continue
            self._probe_endpoint(endpoint)

    def _probe_endpoint(self, endpoint: VllmEndpoint) -> None:
        probe = self.probe_func or _default_probe
        ok, error, latency = probe(endpoint, self.probe_timeout_seconds)
        state = self._state_for(endpoint)
        now = time.monotonic()
        state.last_probe_at = now
        state.last_probe_ok = ok
        state.last_latency_seconds = latency
        if ok:
            state.consecutive_failures = 0
            state.circuit_open_until = 0.0
            state.last_error = ""
            return
        state.last_error = error
        state.consecutive_failures += 1
        if state.consecutive_failures >= self.failure_threshold:
            state.circuit_open_until = now + self.recovery_cooldown_seconds

    def _fallback_reason(self, skipped_names: list[str]) -> str:
        if not skipped_names:
            return ""
        reasons = []
        for name in skipped_names:
            state = self._health.get(name)
            if state is None:
                continue
            reason = state.last_error or "circuit_open"
            reasons.append(f"{name}:{reason}")
        return "; ".join(reasons[:4])

    def _is_endpoint_routable(self, endpoint: VllmEndpoint, now: float) -> bool:
        state = self._state_for(endpoint)
        return not state.is_circuit_open(now)

    def _state_for(self, endpoint: VllmEndpoint) -> EndpointHealthState:
        return self._health.setdefault(endpoint.name, EndpointHealthState())

    def _remember_trace(self, trace: RouteTrace) -> None:
        self._traces.append(trace)
        self._trace_index[trace.trace_id] = trace
        if len(self._traces) > self.trace_limit:
            expired = self._traces.pop(0)
            self._trace_index.pop(expired.trace_id, None)


def build_default_router(
    *,
    short_base_urls: list[str] | None = None,
    long_base_url: str = "http://127.0.0.1:8008/v1",
    extended_base_url: str | None = None,
    extreme_base_url: str | None = None,
    model: str = "qwen-audit-resolver",
    enable_probing: bool = True,
    failure_threshold: int = 2,
    recovery_cooldown_seconds: float = 30.0,
    probe_timeout_seconds: float = 2.0,
    probe_interval_seconds: float = 15.0,
    context_tier_policy: ContextTierPolicy | None = None,
) -> VllmRouter:
    """Build the default local routing policy.

    Short requests prefer independent instances. Long-context requests are split by
    context tier so online, extended, and extreme windows can be mapped explicitly.
    """

    policy = context_tier_policy or ContextTierPolicy()
    endpoints: list[VllmEndpoint] = []
    for index, base_url in enumerate(short_base_urls or ["http://127.0.0.1:8008/v1", "http://127.0.0.1:8009/v1"]):
        endpoints.append(
            VllmEndpoint(
                name=f"short-{index}",
                base_url=base_url,
                model=model,
                profiles=tuple(sorted(SHORT_PROFILES)),
                min_prompt_tokens=0,
                max_prompt_tokens=policy.short_max_tokens,
            )
        )
    endpoints.append(
        VllmEndpoint(
            name="long-standard",
            base_url=long_base_url,
            model=model,
            profiles=tuple(sorted(LONG_PROFILES | {"default"})),
            min_prompt_tokens=0,
            max_prompt_tokens=policy.standard_max_tokens,
        )
    )
    if extended_base_url:
        endpoints.append(
            VllmEndpoint(
                name="long-extended",
                base_url=extended_base_url,
                model=model,
                profiles=tuple(sorted(LONG_PROFILES | {"default"})),
                min_prompt_tokens=policy.standard_max_tokens + 1,
                max_prompt_tokens=policy.extended_max_tokens,
            )
        )
    if extreme_base_url:
        endpoints.append(
            VllmEndpoint(
                name="long-extreme",
                base_url=extreme_base_url,
                model=model,
                profiles=tuple(sorted(LONG_PROFILES | {"default"})),
                min_prompt_tokens=policy.extended_max_tokens + 1,
                max_prompt_tokens=policy.extreme_max_tokens,
            )
        )
    return VllmRouter(
        pool=VllmEndpointPool(endpoints=endpoints),
        enable_probing=enable_probing,
        failure_threshold=failure_threshold,
        recovery_cooldown_seconds=recovery_cooldown_seconds,
        probe_timeout_seconds=probe_timeout_seconds,
        probe_interval_seconds=probe_interval_seconds,
        context_tier_policy=policy,
    )


def estimate_prompt_tokens_from_messages(messages: list) -> int:
    """Approximate chat prompt tokens without adding a tokenizer dependency."""

    total_chars = 0
    for message in messages:
        content = getattr(message, "content", "")
        total_chars += len(str(content))
    return max(1, total_chars // 4)


def _default_probe(endpoint: VllmEndpoint, timeout_seconds: float) -> tuple[bool, str, float | None]:
    started = time.monotonic()
    last_error = "probe_failed"
    for url in _probe_urls(endpoint.base_url):
        try:
            with urlrequest.urlopen(url, timeout=timeout_seconds) as response:
                if 200 <= response.status < 300:
                    return True, "", round(time.monotonic() - started, 6)
                last_error = f"http_status={response.status}"
        except (HTTPError, OSError, URLError, TimeoutError) as exc:
            last_error = str(exc)
    return False, last_error, round(time.monotonic() - started, 6)


def _probe_urls(base_url: str) -> list[str]:
    trimmed = base_url.rstrip("/")
    urls = [f"{trimmed}/models"]
    if trimmed.endswith("/v1"):
        urls.insert(0, f"{trimmed[:-3]}/health")
    else:
        urls.insert(0, f"{trimmed}/health")
    return urls


def _unique_endpoints(endpoints: list[VllmEndpoint]) -> list[VllmEndpoint]:
    seen: set[str] = set()
    unique: list[VllmEndpoint] = []
    for endpoint in endpoints:
        if endpoint.name in seen:
            continue
        seen.add(endpoint.name)
        unique.append(endpoint)
    return unique
