# Diagnostic LLM Platform

This repository contains the initial implementation skeleton for a vehicle
diagnostic knowledge retrieval and code generation platform.

The project uses:

- `MinerU` for document parsing and structured extraction
- `vLLM + Qwen` for online generation
- selective ideas from `HuixiangDou`, instead of copying the full project

What is reused from HuixiangDou:

- dense and sparse retrieval split
- retrieval orchestration and rerank-oriented pipeline design
- OpenAI-compatible serving mindset

What is intentionally not reused:

- group chat workflow
- WeChat/Lark integrations
- web search and chat-specific rejection pipeline
- all-in-one frontend stack

## Current Scope

This repository currently provides:

- a minimal FastAPI service
- a MinerU normalization layer
- a local JSONL index builder for existing MinerU outputs
- a dependency-free sparse retrieval endpoint
- a deterministic XML intermediate plan generator
- a conservative workflow XML fusion renderer
- a workflow parameter audit report
- a template-contract based audit resolver
- a pip-installed vLLM/OpenAI-compatible audit resolver hook
- workload-driven pipeline, regression, and system benchmark runners
- a diagnostic DSL validator
- a C template renderer
- a phased implementation document

## Repository Layout

```text
.
├── README.md
├── docs/
│   └── implementation_path.md
├── pyproject.toml
├── src/
│   └── diagnostic_platform/
│       ├── app.py
│       ├── config.py
│       ├── schemas.py
│       ├── normalizer/
│       │   └── mineru.py
│       ├── renderers/
│       │   └── c_template.py
│       └── validation/
│           └── rules.py
└── tests/
    ├── test_mineru_normalizer.py
    └── test_rule_validator.py
```

## Quick Start

Create an environment and install the package:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Run the API:

```bash
uvicorn diagnostic_platform.app:app --reload
```

Run tests:

```bash
python -m unittest discover -s tests
```

Install and start vLLM with pip-installed runtime:

```bash
scripts/install_vllm_pip.sh
scripts/start_vllm_qwen_coder_awq.sh
```

The currently verified startup path is the local AWQ model
`/home/xdu/huggingface/cyankiwi-Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit`,
served as `qwen-audit-resolver` on `http://127.0.0.1:8008/v1`, using
`CUDA_VISIBLE_DEVICES=0,1`, `--tensor-parallel-size 2`, `--max-model-len 4096`,
and `--enforce-eager`.

The Qwen3-VL GGUF startup path assembles a runtime model directory under
`/tmp/vllm-qwen3-vl-30b-a3b-instruct-q8_0-gguf` and serves the local GGUF model
`qwen3-vl-30b-a3b-instruct-q8_0.gguf`
with the same service name. With pip `vllm==0.19.1`, this GGUF path currently
does not fully load because the upstream GGUF loader cannot map all
`Qwen3VLMoeForConditionalGeneration` weights. Override with
`MODEL_DIR`, `MODEL_PATH`, `RUNTIME_MODEL_DIR`, `TOKENIZER_PATH`, `HF_CONFIG_PATH`,
`SERVED_MODEL_NAME`, `PORT`, `CUDA_VISIBLE_DEVICES`, `TENSOR_PARALLEL_SIZE`,
or `VLLM_BIN` when needed.

Build a local index from existing MinerU output and generate an XML plan:

```bash
python scripts/build_mineru_index.py
python scripts/generate_xml_plan.py --default-arg SourceAddress=0x0E80
python scripts/build_template_registry.py
python scripts/build_template_contracts.py
python scripts/fuse_workflow.py
python scripts/audit_workflow_args.py
python scripts/resolve_audit_report.py
python scripts/resolve_audit_with_llm.py
```

Generate operation-level XML with the LLM-controlled path while keeping system
assembly, validation, repair, and trace guardrails:

```bash
python scripts/generate_xml_with_llm.py \
  --flow-path pdf-loop/20260402_new_v2/0402/0402/flow.xlsx \
  --index-dir data/index/treg_20260402 \
  --template-registry data/processed/xml_templates/template_registry.json \
  --template-contracts data/processed/xml_templates/template_contracts.json \
  --base-workflow path/to/base_workflow.xml
```

This writes `llm_xml_plan.json`, `serial_node.xml`, per-operation XML files,
`llm_generation_trace.json`, and raw evidence snippets. The deterministic XML
plan remains the baseline and regression comparison path.

Build and query the optional Milvus vector index after the JSONL index exists:

```bash
python -m pip install -r requirements/vector.txt

python scripts/build_vector_index.py \
  --index-dir data/index/treg_20260402 \
  --collection-name diagnostic_knowledge_treg_20260402 \
  --embedding-model /home/xdu/LLM/models/Qwen3-Embedding-0.6B

python scripts/query_vector_index.py \
  --index-dir data/index/treg_20260402 \
  --collection-name diagnostic_knowledge_treg_20260402 \
  --query "ZCUD1D01 Change VMM D134"
```

Dense retrieval is opt-in. Existing sparse and Graph RAG paths remain the
default baseline. To feed hybrid retrieval into Graph RAG evidence bundles and
LLM XML generation, pass the vector config explicitly:

```bash
python scripts/generate_xml_plan.py \
  --flow-path pdf-loop/20260402_new_v2/0402/0402/flow.xlsx \
  --index-dir data/index/treg_20260402 \
  --enable-dense \
  --vector-config configs/retrieval/milvus_vector.json

python scripts/generate_xml_with_llm.py \
  --flow-path pdf-loop/20260402_new_v2/0402/0402/flow.xlsx \
  --index-dir data/index/treg_20260402 \
  --template-contracts data/processed/xml_template_registry/treg_20260402_contracts.json \
  --base-workflow "pdf-loop/20260402_new_v2/0402/TREG/P177__FHC（主流程）.xml" \
  --enable-dense \
  --vector-config configs/retrieval/milvus_vector.json
```

Real Milvus integration tests are explicit and do not silently downgrade:

```bash
RUN_MILVUS_INTEGRATION=1 MILVUS_URI=http://127.0.0.1:19530 \
  python -m unittest tests.test_milvus_integration
```

Run the fixed workload pipeline, regression, and benchmark:

```bash
python scripts/run_workload_pipeline.py --workload configs/workloads/treg_20260402.json --print-steps
python scripts/run_workload_regression.py --workload configs/workloads/treg_20260402.json
python scripts/run_system_benchmark.py
```

Run the real-service acceptance loop when Milvus and vLLM are available. The
runner writes sparse deterministic, hybrid deterministic, and hybrid LLM XML
scenario manifests, then summarizes pipeline, regression, retrieval, audit, and
blocked-service status in JSON and Markdown:

```bash
python scripts/run_e2e_acceptance.py \
  --workload configs/workloads/treg_20260402_hybrid_llm.json \
  --milvus-uri http://127.0.0.1:19530 \
  --vllm-base-url http://127.0.0.1:8008/v1 \
  --rebuild-vector-index
```

Use `--dry-run` to inspect the generated scenario commands without executing
the pipeline.

Run the optional vLLM inference benchmark after the local OpenAI-compatible
service is listening on `http://127.0.0.1:8008/v1`:

```bash
python scripts/run_system_benchmark.py --enable-scenario vllm_chat_stream --iterations 1
```

Run the complete benchmark matrix, including 1k/4k/8k/16k input buckets,
128/256/512 output buckets, concurrency 1/4/8/16, and 1GPU/TP2/2-instance
topologies:

```bash
python scripts/run_complete_benchmark.py
```

See [docs/benchmark_matrix.md](docs/benchmark_matrix.md) for the runnable
baseline matrix and the implemented input length, output length, concurrency,
and topology expansion matrix. See [docs/optimization_steps.md](docs/optimization_steps.md)
for the current serving router, prompt budget, and vLLM metrics optimization
record.

## API Endpoints

- `GET /healthz`
- `POST /api/v1/knowledge/normalize`
- `POST /api/v1/index/build`
- `POST /api/v1/retrieval/query`
- `POST /api/v1/xml/plan/generate`
- `POST /api/v1/xml/plan/validate`
- `POST /api/v1/xml/templates/build`
- `POST /api/v1/xml/templates/contracts/build`
- `POST /api/v1/xml/workflow/render`
- `POST /api/v1/xml/workflow/audit`
- `POST /api/v1/xml/workflow/resolve-audit`
- `POST /api/v1/xml/workflow/resolve-audit-llm`
- `POST /api/v1/validation/diagnostic`
- `POST /api/v1/render/c`

## Implementation Notes

The intended near-term flow is:

1. Parse PDF and scanned files with MinerU.
2. Normalize MinerU output into project-specific knowledge units.
3. Build dense retrieval for protocol text and sparse retrieval for code
   templates.
4. Use Qwen on vLLM only where deterministic extraction is weak, including
   unresolved audit conflicts and operation-level XML generation from bounded
   evidence packs.
5. Validate and audit the DSL/XML before rendering or fusing final workflow XML.

## Development Blueprint

See [docs/development_blueprint.md](docs/development_blueprint.md) for the
developable directory structure, module responsibilities, and API list.
