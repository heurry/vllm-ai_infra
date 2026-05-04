"""Runtime shims for serving Qwen3-VL-MoE GGUF with pip vLLM.

This module is loaded only by ``scripts/start_vllm_qwen.sh`` via PYTHONPATH.
It avoids changing global project behavior while working around two vLLM 0.19.1
GGUF edge cases for Qwen3-VL-MoE:

1. Speculator detection asks transformers to parse the GGUF config directly.
2. The GGUF arch table uses ``qwen3vlmoe`` while HF config uses
   ``qwen3_vl_moe``.
"""

from __future__ import annotations


def _patch_speculator_detection() -> None:
    from vllm.transformers_utils import config as vllm_config
    from vllm.transformers_utils.gguf_utils import check_gguf_file

    original = vllm_config.maybe_override_with_speculators

    def patched_maybe_override_with_speculators(
        model: str,
        tokenizer: str | None,
        trust_remote_code: bool,
        revision: str | None = None,
        vllm_speculative_config: dict | None = None,
        hf_token: bool | str | None = None,
        **kwargs,
    ):
        if check_gguf_file(model):
            return model, tokenizer, vllm_speculative_config
        return original(
            model=model,
            tokenizer=tokenizer,
            trust_remote_code=trust_remote_code,
            revision=revision,
            vllm_speculative_config=vllm_speculative_config,
            hf_token=hf_token,
            **kwargs,
        )

    vllm_config.maybe_override_with_speculators = patched_maybe_override_with_speculators


def _patch_gguf_arch_names() -> None:
    import gguf

    for arch, name in list(gguf.MODEL_ARCH_NAMES.items()):
        if name == "qwen3vl":
            gguf.MODEL_ARCH_NAMES[arch] = "qwen3_vl"
        elif name == "qwen3vlmoe":
            gguf.MODEL_ARCH_NAMES[arch] = "qwen3_vl_moe"


def _patch_qwen3_vl_moe_vision_config() -> None:
    from transformers.models.qwen3_vl_moe.configuration_qwen3_vl_moe import (
        Qwen3VLMoeVisionConfig,
    )

    if not hasattr(Qwen3VLMoeVisionConfig, "num_hidden_layers"):
        Qwen3VLMoeVisionConfig.num_hidden_layers = property(  # type: ignore[attr-defined]
            lambda self: self.depth
        )


try:
    _patch_speculator_detection()
    _patch_gguf_arch_names()
    _patch_qwen3_vl_moe_vision_config()
except Exception:
    # Do not break unrelated Python startup if vLLM internals change.
    pass
