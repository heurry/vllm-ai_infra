"""Small OpenAI-compatible client for vLLM chat completions."""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from diagnostic_platform.schemas import ChatMessage, VllmModelConfig
from diagnostic_platform.serving.profiles import RequestProfile
from diagnostic_platform.serving.router import VllmRoute, VllmRouter, estimate_prompt_tokens_from_messages


class VllmClientError(RuntimeError):
    """Raised when the vLLM OpenAI-compatible endpoint fails."""


class VllmClient:
    """HTTP client for a vLLM OpenAI-compatible chat endpoint."""

    def __init__(
        self,
        config: VllmModelConfig,
        router: VllmRouter | None = None,
        profile: RequestProfile = "default",
    ) -> None:
        self.config = config
        self.router = router
        self.profile = profile

    def chat(
        self,
        messages: list[ChatMessage],
        extra_body: dict[str, Any] | None = None,
        profile: RequestProfile | None = None,
    ) -> str:
        """Send chat messages and return the assistant text."""

        config, route = self._resolve_route(messages, profile=profile)
        wire_messages = normalize_chat_messages_for_vllm(messages)
        payload: dict[str, Any] = {
            "model": config.model,
            "messages": [message.model_dump(mode="json") for message in wire_messages],
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
        }
        if extra_body:
            payload.update(extra_body)

        try:
            started = time.monotonic()
            response = self._post_json("/chat/completions", payload, config=config)
            try:
                text = str(response["choices"][0]["message"]["content"])
            except (KeyError, IndexError, TypeError) as exc:
                raise VllmClientError(f"Unexpected vLLM response shape: {response}") from exc
            if self.router is not None and route is not None:
                self.router.mark_success(route, latency_seconds=round(time.monotonic() - started, 6))
            return text
        except Exception as exc:
            if self.router is not None and route is not None:
                self.router.mark_failure(route, exc)
            raise

    def chat_json(
        self,
        messages: list[ChatMessage],
        extra_body: dict[str, Any] | None = None,
        profile: RequestProfile | None = None,
    ) -> dict[str, Any]:
        """Send chat messages and parse the first JSON object from the assistant text."""

        text = self.chat(messages, extra_body=extra_body, profile=profile)
        return extract_json_object(text)

    def _post_json(self, path: str, payload: dict[str, Any], config: VllmModelConfig | None = None) -> dict[str, Any]:
        config = config or self.config
        base_url = config.base_url.rstrip("/")
        url = f"{base_url}{path}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.api_key}",
        }
        req = urlrequest.Request(url, data=body, headers=headers, method="POST")
        try:
            with urlrequest.urlopen(req, timeout=config.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise VllmClientError(f"vLLM HTTP error {exc.code}: {detail}") from exc
        except URLError as exc:
            raise VllmClientError(f"vLLM connection error: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise VllmClientError("vLLM response is not valid JSON") from exc

    def _resolve_route(
        self,
        messages: list[ChatMessage],
        profile: RequestProfile | None = None,
    ) -> tuple[VllmModelConfig, VllmRoute | None]:
        if self.router is None:
            return self.config, None
        route = self.router.route(profile or self.profile, prompt_tokens=estimate_prompt_tokens_from_messages(messages))
        return route.to_model_config(self.config), route


def normalize_chat_messages_for_vllm(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Convert newer OpenAI roles to roles commonly supported by vLLM chat templates."""

    normalized: list[ChatMessage] = []
    developer_blocks: list[str] = []
    for message in messages:
        if message.role == "developer":
            developer_blocks.append(message.content)
        else:
            normalized.append(message)

    if not developer_blocks:
        return normalized

    developer_content = "\n\n".join(developer_blocks)
    if normalized and normalized[0].role == "system":
        first = normalized[0]
        normalized[0] = ChatMessage(
            role="system",
            content=f"{first.content}\n\nDeveloper instructions:\n{developer_content}",
        )
    else:
        normalized.insert(0, ChatMessage(role="system", content=f"Developer instructions:\n{developer_content}"))
    return normalized


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract and parse the first JSON object from model output."""

    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)

    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.S)
        if not match:
            raise VllmClientError(f"No JSON object found in model output: {text[:200]}")
        value = json.loads(match.group(0))

    if not isinstance(value, dict):
        raise VllmClientError(f"Expected JSON object, got: {type(value).__name__}")
    return value
