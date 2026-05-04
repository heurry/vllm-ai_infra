"""Tests for vLLM serving router."""

from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.schemas import ChatMessage, VllmModelConfig
from diagnostic_platform.serving.endpoint_pool import VllmEndpoint, VllmEndpointPool
from diagnostic_platform.serving.router import ContextTierPolicy, VllmRouter, build_default_router
from diagnostic_platform.generation.vllm_client import VllmClient, normalize_chat_messages_for_vllm


class ServingRouterTest(unittest.TestCase):
    def test_route_short_profile_round_robin(self) -> None:
        router = VllmRouter(
            pool=VllmEndpointPool(
                endpoints=[
                    VllmEndpoint(name="short0", base_url="http://127.0.0.1:8008/v1", profiles=("short_audit",)),
                    VllmEndpoint(name="short1", base_url="http://127.0.0.1:8009/v1", profiles=("short_audit",)),
                    VllmEndpoint(name="long", base_url="http://127.0.0.1:8010/v1", profiles=("long_context",)),
                ]
            )
        )

        first = router.route("short_audit", prompt_tokens=512)
        second = router.route("short_audit", prompt_tokens=512)

        self.assertEqual(first.endpoint.base_url, "http://127.0.0.1:8008/v1")
        self.assertEqual(second.endpoint.base_url, "http://127.0.0.1:8009/v1")
        self.assertEqual(first.context_tier, "short")

    def test_vllm_client_uses_routed_config(self) -> None:
        router = VllmRouter(
            pool=VllmEndpointPool(
                endpoints=[
                    VllmEndpoint(name="short", base_url="http://short/v1", model="short-model", profiles=("short_audit",)),
                ]
            )
        )
        client = _FakeRoutedClient(VllmModelConfig(base_url="http://default/v1", model="default"), router=router)

        text = client.chat([ChatMessage(role="user", content="resolve this")], profile="short_audit")

        self.assertEqual(text, "{}")
        self.assertEqual(client.last_url, "http://short/v1")
        self.assertEqual(client.last_payload["model"], "short-model")
        trace = router.recent_traces(limit=1)[0]
        self.assertTrue(trace.success)
        self.assertEqual(trace.selected_endpoint, "short")
        self.assertEqual(trace.context_tier, "short")

    def test_vllm_client_normalizes_developer_role(self) -> None:
        messages = normalize_chat_messages_for_vllm(
            [
                ChatMessage(role="system", content="system rules"),
                ChatMessage(role="developer", content="xml rules"),
                ChatMessage(role="user", content="generate"),
            ]
        )

        self.assertEqual([message.role for message in messages], ["system", "user"])
        self.assertIn("system rules", messages[0].content)
        self.assertIn("Developer instructions:", messages[0].content)
        self.assertIn("xml rules", messages[0].content)

    def test_router_routes_by_context_window_range(self) -> None:
        router = VllmRouter(
            pool=VllmEndpointPool(
                endpoints=[
                    VllmEndpoint(
                        name="standard",
                        base_url="http://standard/v1",
                        profiles=("long_context", "default"),
                        min_prompt_tokens=0,
                        max_prompt_tokens=32768,
                    ),
                    VllmEndpoint(
                        name="extended",
                        base_url="http://extended/v1",
                        profiles=("long_context", "default"),
                        min_prompt_tokens=32769,
                        max_prompt_tokens=65536,
                    ),
                ]
            ),
            context_tier_policy=ContextTierPolicy(
                short_max_tokens=4096,
                standard_max_tokens=32768,
                extended_max_tokens=65536,
                extreme_max_tokens=131072,
            ),
        )

        standard = router.route("long_context", prompt_tokens=24000)
        extended = router.route("long_context", prompt_tokens=48000)

        self.assertEqual(standard.endpoint.name, "standard")
        self.assertEqual(standard.context_tier, "standard")
        self.assertEqual(extended.endpoint.name, "extended")
        self.assertEqual(extended.context_tier, "extended")

    def test_build_default_router_adds_extended_and_extreme_tiers(self) -> None:
        router = build_default_router(
            short_base_urls=["http://short0/v1", "http://short1/v1"],
            long_base_url="http://standard/v1",
            extended_base_url="http://extended/v1",
            extreme_base_url="http://extreme/v1",
        )

        names = [endpoint.name for endpoint in router.pool.endpoints]

        self.assertIn("long-standard", names)
        self.assertIn("long-extended", names)
        self.assertIn("long-extreme", names)
        extreme = next(endpoint for endpoint in router.pool.endpoints if endpoint.name == "long-extreme")
        self.assertEqual(extreme.min_prompt_tokens, 65537)
        self.assertEqual(extreme.max_prompt_tokens, 131072)

    def test_router_opens_circuit_after_failure_threshold(self) -> None:
        router = VllmRouter(
            pool=VllmEndpointPool(
                endpoints=[
                    VllmEndpoint(name="short0", base_url="http://127.0.0.1:8008/v1", profiles=("short_audit",)),
                ]
            ),
            failure_threshold=1,
            recovery_cooldown_seconds=30.0,
        )

        route = router.route("short_audit", prompt_tokens=512)
        router.mark_failure(route, "boom")

        with self.assertRaises(ValueError):
            router.route("short_audit", prompt_tokens=512)
        snapshot = router.health_snapshot()["short0"]
        self.assertTrue(snapshot["circuit_open"])
        self.assertEqual(router.recent_traces(limit=1)[0].error, "boom")

    def test_router_recovers_after_cooldown_probe(self) -> None:
        probe_calls: list[str] = []

        def probe(endpoint, timeout_seconds):
            probe_calls.append(endpoint.name)
            return True, "", 0.01

        router = VllmRouter(
            pool=VllmEndpointPool(
                endpoints=[
                    VllmEndpoint(name="short0", base_url="http://127.0.0.1:8008/v1", profiles=("short_audit",)),
                ]
            ),
            failure_threshold=1,
            recovery_cooldown_seconds=0.01,
            probe_interval_seconds=0.0,
            enable_probing=True,
            probe_func=probe,
        )

        route = router.route("short_audit", prompt_tokens=512)
        router.mark_failure(route, "boom")
        time.sleep(0.02)

        recovered = router.route("short_audit", prompt_tokens=512)

        self.assertEqual(recovered.endpoint.name, "short0")
        self.assertIn("short0", probe_calls)
        self.assertFalse(router.health_snapshot()["short0"]["circuit_open"])


class _FakeRoutedClient(VllmClient):
    def __init__(self, config, router):
        super().__init__(config, router=router)
        self.last_url = ""
        self.last_payload = {}

    def _post_json(self, path, payload, config=None):
        self.last_url = (config or self.config).base_url
        self.last_payload = payload
        return {"choices": [{"message": {"content": "{}"}}]}


if __name__ == "__main__":
    unittest.main()
