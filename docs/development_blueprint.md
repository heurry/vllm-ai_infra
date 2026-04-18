# 可开发蓝图

本文档定义项目下一阶段可直接开发的目录结构、模块职责和 API 清单。设计原则是：参考 HuixiangDou 的检索与 pipeline 思路，但不复制其群聊、Web 搜索、前端集成等业务壳。

## 1. 开发原则

| 原则 | 说明 |
| --- | --- |
| 先闭环后优化 | 先打通 `MinerU -> normalize -> retrieve -> generate DSL -> validate -> render` |
| 中间 DSL 优先 | 模型先输出可校验 DSL，不直接输出最终 C 代码 |
| 检索分层 | 协议文档走 dense，代码模板走 sparse，实体关系走 graph/light index |
| 可替换后端 | vLLM、embedding、reranker、vector store 都通过配置切换 |
| 可观测先行 | 从 MVP 开始记录 request_id、耗时、召回数量、校验结果 |

## 2. 推荐目录结构

```text
vllm-ai_infra/
├── README.md
├── pyproject.toml
├── configs/
│   ├── app.yaml
│   ├── mineru.yaml
│   ├── retrieval.yaml
│   ├── validation.yaml
│   ├── model/
│   │   ├── qwen_7b_single.yaml
│   │   ├── qwen_7b_dual_instance.yaml
│   │   └── qwen_tp2.yaml
│   └── prompt/
│       ├── diagnostic_dsl.yaml
│       └── query_rewrite.yaml
├── data/
│   ├── raw/
│   ├── processed/
│   │   └── mineru/
│   ├── knowledge/
│   ├── index/
│   │   ├── dense/
│   │   ├── sparse/
│   │   ├── graph/
│   │   └── metadata/
│   └── eval/
│       ├── retrieval/
│       ├── generation/
│       └── benchmark/
├── docs/
│   ├── implementation_path.md
│   └── development_blueprint.md
├── scripts/
│   ├── ingest_mineru.sh
│   ├── build_index.sh
│   ├── start_api.sh
│   ├── start_vllm_dual_instance.sh
│   └── benchmark.sh
├── src/
│   └── diagnostic_platform/
│       ├── app.py
│       ├── config.py
│       ├── schemas.py
│       ├── api/
│       │   ├── routes_documents.py
│       │   ├── routes_knowledge.py
│       │   ├── routes_retrieval.py
│       │   ├── routes_generation.py
│       │   ├── routes_validation.py
│       │   └── routes_benchmark.py
│       ├── ingestion/
│       │   ├── mineru_client.py
│       │   ├── batch.py
│       │   └── manifest.py
│       ├── normalizer/
│       │   ├── mineru.py
│       │   ├── code_template.py
│       │   └── entity_extractor.py
│       ├── indexing/
│       │   ├── chunker.py
│       │   ├── dense_store.py
│       │   ├── sparse_store.py
│       │   ├── graph_store.py
│       │   ├── metadata_store.py
│       │   └── builder.py
│       ├── retrieval/
│       │   ├── query_rewrite.py
│       │   ├── dense.py
│       │   ├── sparse.py
│       │   ├── graph.py
│       │   ├── hybrid.py
│       │   ├── reranker.py
│       │   └── context_packer.py
│       ├── generation/
│       │   ├── vllm_client.py
│       │   ├── prompt_builder.py
│       │   └── dsl_parser.py
│       ├── validation/
│       │   ├── rules.py
│       │   ├── schema_validator.py
│       │   └── fallback.py
│       ├── renderers/
│       │   └── c_template.py
│       ├── serving/
│       │   ├── router.py
│       │   ├── cache.py
│       │   └── metrics.py
│       └── evaluation/
│           ├── retrieval_eval.py
│           ├── generation_eval.py
│           └── benchmark.py
└── tests/
    ├── test_mineru_normalizer.py
    ├── test_rule_validator.py
    ├── test_retrieval_pipeline.py
    └── test_api_contract.py
```

## 3. 模块职责

| 模块 | 优先级 | 核心职责 | 首版交付 |
| --- | --- | --- | --- |
| `api` | P0 | 拆分 FastAPI routes，保持 app 入口轻量 | REST API 路由文件 |
| `ingestion` | P0 | 调用 MinerU，管理文件批处理和 manifest | PDF/图片解析任务 |
| `normalizer` | P0 | 将 MinerU、代码模板、人工规则转成统一知识单元 | `KnowledgeUnit` 列表 |
| `indexing` | P0 | 建 dense/sparse/metadata 索引 | 本地索引构建脚本 |
| `retrieval` | P0 | Query rewrite、多路召回、rerank、context pack | `RetrievalResponse` |
| `generation` | P0 | 调用 vLLM/OpenAI-compatible API，生成 DSL | `DiagnosticPlan` |
| `validation` | P0 | JSON schema 校验、诊断规则校验、失败回退 | `ValidationResult` |
| `renderers` | P0 | 将通过校验的 DSL 渲染成 C 模板 | C function string |
| `serving` | P1 | 请求路由、缓存、指标采集、双实例分流 | Router/metrics/cache |
| `evaluation` | P1 | 检索评测、生成评测、benchmark | 指标报告 JSON |
| `configs` | P0 | 管理模型、检索、prompt、服务配置 | YAML 配置 |
| `scripts` | P0 | 固化 ingest/build/start/benchmark 命令 | 可复现脚本 |

## 4. 数据对象

### 4.1 Document

```json
{
  "doc_id": "uds_bcm_v1",
  "doc_type": "pdf_protocol",
  "protocol": "UDS",
  "module": "security_access",
  "vehicle_model": "generic",
  "ecu": "BCM",
  "version": "v1.0",
  "source_path": "data/raw/uds_bcm_v1.pdf"
}
```

### 4.2 KnowledgeUnit

```json
{
  "chunk_id": "uds_bcm_v1_0001",
  "unit_type": "text_chunk",
  "content": "Enter Extended Session with 10 03 before Security Access.",
  "protocol": "UDS",
  "module": "security_access",
  "ecu": "BCM",
  "service_ids": ["10", "27"],
  "dids": [],
  "sessions": ["03"],
  "security_levels": ["05"],
  "page_idx": 12,
  "bbox": [62, 480, 946, 904],
  "source_path": "data/raw/uds_bcm_v1.pdf"
}
```

### 4.3 DiagnosticPlan

```json
{
  "function_name": "SecurityAccess_Level05",
  "preconditions": ["EnterExtendedSession"],
  "steps": [
    {"send": "10 03", "expect": "50 03"},
    {"send": "27 05", "expect": "67 05"},
    {"send": "27 06 12 34", "expect": "67 06"}
  ],
  "on_fail": "return FAILED;"
}
```

## 5. API 清单

### 5.1 当前已实现 API

| Method | Path | 职责 |
| --- | --- | --- |
| `GET` | `/healthz` | 服务存活检查 |
| `POST` | `/api/v1/knowledge/normalize` | 将 MinerU `content_list` 风格数据转成知识单元 |
| `POST` | `/api/v1/validation/diagnostic` | 校验诊断 DSL |
| `POST` | `/api/v1/render/c` | 将诊断 DSL 渲染为 C 模板 |

### 5.2 Phase 1 API

| Method | Path | 输入 | 输出 | 说明 |
| --- | --- | --- | --- | --- |
| `POST` | `/api/v1/documents/parse` | `source_path`, `metadata`, `backend` | `ParseJob` | 调用 MinerU 解析 PDF/图片 |
| `GET` | `/api/v1/documents/{doc_id}` | `doc_id` | `DocumentDetail` | 查询文档、MinerU 输出和入库状态 |
| `POST` | `/api/v1/documents/ingest` | `doc_id`, `mineru_output_dir` | `IngestResult` | 解析结果归一化并写入知识库 |
| `POST` | `/api/v1/index/build` | `index_types`, `doc_ids` | `IndexBuildJob` | 构建 dense/sparse/metadata 索引 |
| `GET` | `/api/v1/index/status` | `index_type` | `IndexStatus` | 查询索引版本与构建状态 |
| `POST` | `/api/v1/retrieval/query` | `query`, `filters`, `top_k` | `RetrievalResponse` | 执行 hybrid retrieval |
| `POST` | `/api/v1/generation/dsl` | `query`, `filters` | `DiagnosticPlan` | 检索增强生成 DSL |
| `POST` | `/api/v1/generation/code` | `query`, `filters` | `RenderedCode` | 生成、校验、渲染完整链路 |

### 5.3 Phase 2 API

| Method | Path | 输入 | 输出 | 说明 |
| --- | --- | --- | --- | --- |
| `POST` | `/api/v1/retrieval/eval` | `eval_set_path` | `RetrievalEvalReport` | 计算 Recall@k 和 MRR |
| `POST` | `/api/v1/generation/eval` | `eval_set_path` | `GenerationEvalReport` | 计算 schema/rule pass rate |
| `POST` | `/api/v1/benchmark/run` | `workload`, `topology` | `BenchmarkRun` | 启动 serving benchmark |
| `GET` | `/api/v1/benchmark/runs/{run_id}` | `run_id` | `BenchmarkReport` | 查询 benchmark 结果 |
| `GET` | `/metrics` | 无 | Prometheus text | 暴露运行时指标 |

## 6. API 详细定义

### 6.1 `POST /api/v1/documents/parse`

请求：

```json
{
  "source_path": "data/raw/uds_bcm_v1.pdf",
  "output_dir": "data/processed/mineru/uds_bcm_v1",
  "backend": "pipeline",
  "metadata": {
    "doc_id": "uds_bcm_v1",
    "doc_type": "pdf_protocol",
    "protocol": "UDS",
    "module": "security_access",
    "ecu": "BCM",
    "version": "v1.0"
  }
}
```

响应：

```json
{
  "job_id": "parse_20260418_0001",
  "status": "queued",
  "output_dir": "data/processed/mineru/uds_bcm_v1"
}
```

### 6.2 `POST /api/v1/retrieval/query`

请求：

```json
{
  "query": "实现 BCM security access level 05 的流程",
  "filters": {
    "protocol": "UDS",
    "ecu": "BCM",
    "module": "security_access"
  },
  "top_k": 8,
  "enable_dense": true,
  "enable_sparse": true,
  "enable_graph": false
}
```

响应：

```json
{
  "rewritten_query": "UDS BCM Security Access level 05 with extended session",
  "chunks": [
    {
      "chunk_id": "uds_bcm_v1_0001",
      "score": 0.91,
      "source": "dense",
      "content": "Enter Extended Session with 10 03..."
    }
  ],
  "context": "..."
}
```

### 6.3 `POST /api/v1/generation/code`

请求：

```json
{
  "query": "实现 BCM security access level 05 的 C 函数",
  "filters": {
    "protocol": "UDS",
    "ecu": "BCM",
    "module": "security_access"
  },
  "output_language": "c"
}
```

响应：

```json
{
  "diagnostic_plan": {
    "function_name": "SecurityAccess_Level05",
    "preconditions": ["EnterExtendedSession"],
    "steps": [
      {"send": "10 03", "expect": "50 03"},
      {"send": "27 05", "expect": "67 05"},
      {"send": "27 06 12 34", "expect": "67 06"}
    ],
    "on_fail": "return FAILED;"
  },
  "validation": {
    "valid": true,
    "issues": []
  },
  "code": "int SecurityAccess_Level05(void)\\n{\\n    ...\\n}"
}
```

## 7. 首批开发顺序

1. 拆分 `api/routes_*.py`，避免 `app.py` 继续膨胀。
2. 实现 `ingestion/mineru_client.py`，用 CLI 或 Python API 调用 `/home/xdu/MinerU`。
3. 实现 `indexing/builder.py`，先落本地 JSONL + BM25，dense store 可后补 Faiss。
4. 实现 `retrieval/hybrid.py`，先做到 dense/sparse merge + score normalize。
5. 实现 `generation/vllm_client.py` 和 `prompt_builder.py`，对接 OpenAI-compatible vLLM。
6. 扩展 `validation/rules.py`，加入 DID、NRC、timeout、retry、session/security 规则。
7. 实现 `evaluation/benchmark.py`，固定 workload 和指标记录格式。

## 8. 最小可验收标准

| 能力 | 验收标准 |
| --- | --- |
| 文档解析 | 10 份 PDF 能通过 MinerU 生成 Markdown 和 `content_list.json` |
| 知识入库 | 每份文档能生成可追溯 `KnowledgeUnit` |
| 检索 | 给定 ECU/module/service 能返回相关 chunk |
| 生成 | 模型能稳定输出 `DiagnosticPlan` JSON |
| 校验 | 错误的 security access 顺序会被拦截 |
| 渲染 | 通过校验的 DSL 能渲染为 C 函数模板 |
| Benchmark | 能输出 P50/P95、TTFT、TPOT、schema pass rate |

