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

## API Endpoints

- `GET /healthz`
- `POST /api/v1/knowledge/normalize`
- `POST /api/v1/validation/diagnostic`
- `POST /api/v1/render/c`

## Implementation Notes

The intended near-term flow is:

1. Parse PDF and scanned files with MinerU.
2. Normalize MinerU output into project-specific knowledge units.
3. Build dense retrieval for protocol text and sparse retrieval for code
   templates.
4. Generate a structured diagnostic DSL with Qwen on vLLM.
5. Validate the DSL before rendering final C code.

## Development Blueprint

See [docs/development_blueprint.md](docs/development_blueprint.md) for the
developable directory structure, module responsibilities, and API list.
