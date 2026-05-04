#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

DEFAULT_MODEL_DIR="/home/xdu/huggingface/Qwen3-VL-30B-A3B-Instruct-Q8_0-GGUF"
DEFAULT_TOKENIZER_DIR="/home/xdu/.cache/huggingface/hub/models--Qwen--Qwen3-VL-30B-A3B-Instruct/snapshots/9c4b90e1e4ba969fd3b5378b57d966d725f1b86c"
DEFAULT_RUNTIME_MODEL_DIR="/tmp/vllm-qwen3-vl-30b-a3b-instruct-q8_0-gguf"

MODEL_DIR="${MODEL_DIR:-${DEFAULT_MODEL_DIR}}"
TOKENIZER_PATH="${TOKENIZER_PATH:-${DEFAULT_TOKENIZER_DIR}}"
RUNTIME_MODEL_DIR="${RUNTIME_MODEL_DIR:-${DEFAULT_RUNTIME_MODEL_DIR}}"
if [[ -z "${MODEL_PATH:-}" ]]; then
  mkdir -p "${RUNTIME_MODEL_DIR}"
  for file in config.json generation_config.json tokenizer.json tokenizer_config.json preprocessor_config.json merges.txt vocab.json; do
    if [[ -e "${TOKENIZER_PATH}/${file}" ]]; then
      ln -sfn "${TOKENIZER_PATH}/${file}" "${RUNTIME_MODEL_DIR}/${file}"
    fi
  done
  ln -sfn "${MODEL_DIR}/qwen3-vl-30b-a3b-instruct-q8_0.gguf" "${RUNTIME_MODEL_DIR}/qwen3-vl-30b-a3b-instruct-q8_0.gguf"
  ln -sfn "${MODEL_DIR}/mmproj-qwen3-vl-30b-a3b-instruct-q8_0.gguf" "${RUNTIME_MODEL_DIR}/mmproj-qwen3-vl-30b-a3b-instruct-q8_0.gguf"
  MODEL_PATH="${RUNTIME_MODEL_DIR}/qwen3-vl-30b-a3b-instruct-q8_0.gguf"
fi
HF_CONFIG_PATH="${HF_CONFIG_PATH:-$(dirname "${MODEL_PATH}")}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-qwen-audit-resolver}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8008}"
CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0,1}"
TENSOR_PARALLEL_SIZE="${TENSOR_PARALLEL_SIZE:-2}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-8192}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"
VLLM_BIN="${VLLM_BIN:-${REPO_ROOT}/.venv/bin/vllm}"
DRY_RUN="${DRY_RUN:-0}"
ENFORCE_EAGER="${ENFORCE_EAGER:-0}"
ENABLE_PREFIX_CACHING="${ENABLE_PREFIX_CACHING:-0}"
ENABLE_CHUNKED_PREFILL="${ENABLE_CHUNKED_PREFILL:-0}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-}"
MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-}"

export CUDA_VISIBLE_DEVICES
export PYTHONPATH="${REPO_ROOT}/scripts/vllm_compat${PYTHONPATH:+:${PYTHONPATH}}"

cmd=(
  "${VLLM_BIN}" serve "${MODEL_PATH}"
  --host "${HOST}"
  --port "${PORT}"
  --served-model-name "${SERVED_MODEL_NAME}"
  --tokenizer "${TOKENIZER_PATH}"
  --hf-config-path "${HF_CONFIG_PATH}"
  --tensor-parallel-size "${TENSOR_PARALLEL_SIZE}"
  --max-model-len "${MAX_MODEL_LEN}"
  --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}"
  --generation-config vllm
)

if [[ "${ENFORCE_EAGER}" == "1" ]]; then
  cmd+=(--enforce-eager)
fi

if [[ "${ENABLE_PREFIX_CACHING}" == "1" ]]; then
  cmd+=(--enable-prefix-caching)
fi

if [[ "${ENABLE_CHUNKED_PREFILL}" == "1" ]]; then
  cmd+=(--enable-chunked-prefill)
fi

if [[ -n "${MAX_NUM_SEQS}" ]]; then
  cmd+=(--max-num-seqs "${MAX_NUM_SEQS}")
fi

if [[ -n "${MAX_NUM_BATCHED_TOKENS}" ]]; then
  cmd+=(--max-num-batched-tokens "${MAX_NUM_BATCHED_TOKENS}")
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  printf 'CUDA_VISIBLE_DEVICES=%s ' "${CUDA_VISIBLE_DEVICES}"
  printf '%q ' "${cmd[@]}"
  printf '\n'
  exit 0
fi

"${cmd[@]}"
