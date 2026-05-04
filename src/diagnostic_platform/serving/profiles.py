"""Request profiles used by the vLLM serving router."""

from __future__ import annotations

from typing import Literal


RequestProfile = Literal[
    "short_audit",
    "rerank",
    "long_context",
    "repair",
    "xml_generation",
    "xml_repair",
    "long_context_xml_generation",
    "default",
]


SHORT_PROFILES: set[RequestProfile] = {"short_audit", "rerank"}
LONG_PROFILES: set[RequestProfile] = {
    "long_context",
    "repair",
    "xml_generation",
    "xml_repair",
    "long_context_xml_generation",
}
