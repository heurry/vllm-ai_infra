"""Endpoint pool primitives for vLLM routing."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count
from typing import Iterable

from diagnostic_platform.serving.profiles import RequestProfile


@dataclass(frozen=True)
class VllmEndpoint:
    """One OpenAI-compatible vLLM endpoint."""

    name: str
    base_url: str
    model: str = "qwen-audit-resolver"
    profiles: tuple[RequestProfile, ...] = ("default",)
    min_prompt_tokens: int = 0
    max_prompt_tokens: int = 4096
    weight: int = 1
    enabled: bool = True


@dataclass
class VllmEndpointPool:
    """Round-robin endpoint pool filtered by request profile and prompt size."""

    endpoints: list[VllmEndpoint] = field(default_factory=list)
    _counter: count = field(default_factory=count, init=False, repr=False)

    def candidates(self, profile: RequestProfile, prompt_tokens: int = 0) -> list[VllmEndpoint]:
        """Return enabled endpoints that can serve the request profile."""

        direct = [
            endpoint
            for endpoint in self.endpoints
            if endpoint.enabled
            and endpoint.min_prompt_tokens <= prompt_tokens
            and prompt_tokens <= endpoint.max_prompt_tokens
            and (profile in endpoint.profiles or "default" in endpoint.profiles)
        ]
        if direct:
            return _expand_by_weight(direct)
        fallback = [
            endpoint
            for endpoint in self.endpoints
            if endpoint.enabled
            and endpoint.min_prompt_tokens <= prompt_tokens
            and prompt_tokens <= endpoint.max_prompt_tokens
            and "default" in endpoint.profiles
        ]
        return _expand_by_weight(fallback)

    def select(self, profile: RequestProfile, prompt_tokens: int = 0) -> VllmEndpoint:
        """Select one endpoint using a deterministic round-robin cursor."""

        candidates = self.candidates(profile, prompt_tokens)
        if not candidates:
            raise ValueError(f"No enabled vLLM endpoint for profile={profile!r}, prompt_tokens={prompt_tokens}")
        index = next(self._counter) % len(candidates)
        return candidates[index]


def _expand_by_weight(endpoints: Iterable[VllmEndpoint]) -> list[VllmEndpoint]:
    expanded: list[VllmEndpoint] = []
    for endpoint in endpoints:
        expanded.extend([endpoint] * max(1, endpoint.weight))
    return expanded
