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
| `POST` | `/api/v1/documents/parse` | 调用 MinerU 解析 PDF/图片并发现输出文件 |
| `POST` | `/api/v1/documents/load-mineru` | 将已有 MinerU 输出目录加载为 `NormalizeMinerURequest` |
| `POST` | `/api/v1/knowledge/normalize` | 将 MinerU `content_list` 风格数据转成知识单元 |
| `POST` | `/api/v1/validation/diagnostic` | 校验诊断 DSL |
| `POST` | `/api/v1/render/c` | 将诊断 DSL 渲染为 C 模板 |
| `POST` | `/api/v1/xml/flow/parse` | 解析 `flow.xlsx` 为 `FlowStepPlan` |
| `POST` | `/api/v1/xml/evidence/from-mineru` | 将 MinerU 输出转成可追溯 `EvidenceUnit` |
| `POST` | `/api/v1/xml/evidence/build` | 将 `FlowStepPlan` 与 `EvidenceUnit` 关联成 `StepEvidenceBundle` |
| `POST` | `/api/v1/xml/plan/generate` | 将 `StepEvidenceBundle` 转成可校验 XML 中间 DSL |
| `POST` | `/api/v1/xml/plan/validate` | 校验 XML 中间 DSL 的节点、必填参数、占位符和证据引用 |
| `POST` | `/api/v1/xml/templates/build` | 扫描历史 XML，建立 `ScriptNode` 模板注册表 |
| `POST` | `/api/v1/xml/templates/contracts/build` | 从模板注册表推断 `ClassName -> allowed/required Args` 参数契约 |
| `POST` | `/api/v1/xml/workflow/render` | 将 XML 中间 DSL 融合进历史主流程 workflow XML |
| `POST` | `/api/v1/xml/workflow/audit` | 对比 `xml_plan`、原始 workflow、融合 workflow，生成参数差异和人工复核报告 |
| `POST` | `/api/v1/xml/workflow/resolve-audit` | 用模板参数契约解析审计项，自动忽略非脚本参数并保留真实冲突复核清单 |
| `POST` | `/api/v1/xml/workflow/resolve-audit-llm` | 调用 pip 安装的 vLLM OpenAI-compatible 服务，对剩余复核项做保守仲裁 |
| `POST` | `/api/v1/xml/graph/build` | 从步骤计划和证据包构建轻量诊断图谱 |
| `POST` | `/api/v1/xml/graph/search` | 查询诊断图谱中的实体关系路径 |
| `POST` | `/api/v1/xml/task/render` | 将 XML 中间 DSL 渲染为 `ScriptNode` / `SerialNode` XML |
| `POST` | `/api/v1/xml/validate` | 校验 XML 结构、占位符、必填参数和脚本节点 |
| `POST` | `/api/v1/index/build` | 将已有 MinerU 输出批量构建为本地 JSONL 知识索引和轻量图谱 |
| `POST` | `/api/v1/retrieval/query` | 基于本地 JSONL 知识索引执行无外部依赖的 sparse 检索 |

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

## 7. PDF 到最终 XML 生成流程

本节描述当前项目应采用的 PDF 到最终 XML 代码生成链路。这里参考 `pdf-loop` 的目标形态：从 PDF、流程表、参数说明和模板中生成可落地的工作流 XML；但不沿用其具体实现方式。新的实现需要把 MinerU 解析、结构化知识建模、Hybrid Retrieval、GraphRAG、规则校验和模板渲染统一到一条更严谨的工程链路中。

### 7.1 总体目标

目标不是让模型直接生成 XML，而是形成一条可追溯、可校验、可回放的生成流水线：

```text
PDF / flow.xlsx / 历史 XML 模板
  -> MinerU 文档解析
  -> 结构化知识单元
  -> 诊断实体与流程图谱
  -> Hybrid Retrieval + GraphRAG
  -> XML 中间 DSL
  -> 规则校验与图一致性校验
  -> XML 模板渲染
  -> 主流程融合
  -> 最终 workflow XML
```

最终产物应是完整可解析的工程 XML，而不是片段化 XML 或单个模板替换结果。典型输出包括：

- 每个步骤的结构化证据包；
- 每个步骤的 XML 参数集合；
- 每个步骤的 TaskNode / ScriptNode XML；
- 每个操作单独生成的 ScriptNode XML；
- 每个 `flow.xlsx` 操作的检索过程追溯报告；
- 完整主流程 workflow XML；
- XML 校验报告和生成追溯报告。

### 7.2 输入来源

| 输入 | 作用 | 处理方式 |
| --- | --- | --- |
| PDF / 扫描件 | 协议说明、ECU 参数表、流程图来源 | 通过 MinerU 输出 Markdown、`content_list.json`、图片区域和 bbox |
| `flow.xlsx` | 描述步骤顺序、并行节点和模板名称 | 横向行内按列串行执行，同一列跨行是并行操作，解析为 `FlowStepPlan` |
| 历史 XML 模板 | 提供真实标签、属性、ClassName、Args 风格 | 建立 `XmlTemplateRegistry`，不直接全文复制 |
| 人工规则表 | 补充 ECU 地址、DID、错误码、安全等级等稳定规则 | 入库为 `rule_chunk` / metadata |
| 历史生成结果 | 作为对齐样例和回归评测数据 | 入库为 `code_template` / `xml_template` |

### 7.3 关键中间对象

PDF 到 XML 不能只依赖文本 chunk，需要显式建模以下对象：

```text
FlowStepPlan
  - step_key
  - order
  - column
  - parallel_nodes: 同一 Excel 列中的操作，按 row 排序
  - node_name
  - template_name

EvidenceUnit
  - source_doc
  - page_idx
  - bbox
  - content
  - evidence_type: text/table/image/flowchart/template

GraphEntity
  - entity_type: ECU/DID/RID/Service/Session/SecurityLevel/FlowNode/XmlTemplate
  - name
  - aliases
  - metadata

GraphRelation
  - source
  - relation_type: requires/next/branch_to/uses_template/defines_arg/positive_response/error_code
  - target
  - evidence_ids

XmlGenerationPlan
  - flow_steps
  - selected_templates
  - arg_sets
  - graph_paths
  - validation_constraints

XmlArgSet
  - node_name
  - class_name
  - args[{name, value, source_evidence}]

WorkflowXmlPlan
  - phase_name
  - task_nodes
  - serial_nodes
  - parallel_nodes
  - refresh_strategy
```

`flow.xlsx` 的结构语义固定为：每一行从左到右是串行执行链；同一列中跨多行的非空流程单元格属于同一串行阶段的并行操作。每个单元格代表一个独立操作，必须生成一个独立的 `ScriptNode` XML；当同一列包含多个操作时，汇总 XML 中应渲染为一个 `ParallelNode`，其 `Tasks` 下挂这些 `ScriptNode`。

### 7.4 MinerU 解析与知识入库

第一步使用 MinerU 对 PDF 和扫描件做统一解析。Markdown 只作为人工查看材料，系统入库应优先使用结构化输出：

- `content_list.json`：保留阅读顺序、块类型、页码、bbox；
- `middle.json`：保留更细粒度的版面、图片、表格和中间结构；
- 图片目录：保留流程图、参数页截图和图表区域。

归一化后生成多类 `KnowledgeUnit`：

- `text_chunk`：协议说明、步骤解释；
- `table_chunk`：ECU 参数表、DID 表、错误码表；
- `image_region`：流程图、截图区域；
- `flow_step`：从流程图或流程表中抽取出的步骤节点；
- `xml_template`：历史 XML 模板和可复用 XML 片段；
- `rule_chunk`：人工整理的稳定规则。

### 7.5 图谱建模与 GraphRAG

旧流程里主要依赖关键词和模板名做匹配，遇到跨页引用、跳转、并行分支、异常分支时容易丢失关系。当前项目应把 GraphRAG 放在主链路中，用图谱显式表达诊断流程和 XML 编排关系。

图谱至少包含以下实体：

- `ECU`：如 BCM、ZCUD1D01、ECM1610；
- `DID/RID`：如 D134、DD0A、0208；
- `UDS Service`：如 `10`、`22`、`27`、`2F`、`31`；
- `Session`：默认会话、扩展会话、编程会话；
- `SecurityLevel`：Level 3、Level 5；
- `FlowNode`：流程图节点、判断节点、结束节点；
- `XmlTemplate`：TaskNode、ScriptNode、PhaseNode 模板；
- `ErrorCode`：IOErrorVCode、ReadErrorVCode、NRC、VC/VCode。

图谱至少包含以下关系：

- `requires_session`：服务或步骤需要某个会话；
- `requires_security`：服务或步骤需要某个安全等级；
- `uses_did` / `uses_rid`：步骤使用 DID/RID；
- `positive_response`：请求与正响应之间的关系；
- `next_step`：普通顺序跳转；
- `branch_to`：条件分支跳转；
- `retry_to`：失败重试跳转；
- `fallback_to`：失败兜底路径；
- `uses_template`：步骤绑定 XML 模板；
- `defines_arg`：证据文本定义某个 XML 参数。

GraphRAG 的职责不是替代向量检索，而是解决“结构关系”问题：

- 识别流程图里的跳转、分支、重试、结束路径；
- 将参数页中的 DID、RID、错误码和流程图节点对齐；
- 在跨页、跨文档引用时找到真实目标；
- 避免只因关键词相似而选错模板；
- 为 XML 生成提供可解释的路径证据。

### 7.6 检索与证据包构建

每个流程步骤都应构建一个 `StepEvidenceBundle`，而不是直接把散乱文本塞给模型。

```text
FlowStep
  -> Query Rewrite
  -> Metadata Filter
  -> Dense Retrieval
  -> Sparse Retrieval
  -> GraphRAG Path Search
  -> Rerank
  -> Evidence Bundle
```

证据包应包含：

- 当前步骤名称、模板名、ClassName；
- 参数文本证据；
- 参数表所在页码和 bbox；
- 流程图图片或流程图结构化结果；
- 相关 DID/RID/Service/Session/SecurityLevel；
- 图谱路径，例如 `step -> uses_did -> DID -> requires_service -> Service`；
- 候选 XML 模板及选择理由。

生成阶段需要额外落盘 `flow_evidence_trace.json`，按 `step_key / row / column / node_name` 记录每个操作的 top-k 检索候选、score、matched_terms、evidence_id、页码、bbox、内容摘要、GraphRAG 路径、最终采用的参数和单操作 XML 路径。同步落盘 flow-aware `diagnostic_graph.json`，用于回放 `FlowNode -> Evidence / DID / Service / Session / SecurityLevel / next_step` 等路径。`xml_plan.json` 只保留最终 plan 和 evidence id，不能替代完整检索过程记录。

参数选择不能只累加 evidence score。`EcuBOMName / DID / ServiceID / Session / SecurityLevel` 应按 `evidence_score + graph_score + flow_node_prior - conflict_penalty` 选择候选值，并在 `XmlArg` 中保留 `selection_score`、`score_breakdown`、`graph_paths` 和 `selection_notes`。这样后续审计可以解释“为什么选择这个参数值”，也能发现 flow 节点 ECU 与证据 ECU 不一致的情况。

### 7.7 XML 中间 DSL

模型或规则不直接输出最终 XML，而是输出 XML 中间 DSL。这样可以在渲染前做 schema 校验、图一致性校验和规则校验。

示例：

```json
{
  "node_name": "Car_Mode_Change_1",
  "class_name": "GEEA30_VMM_Change",
  "template_type": "io_control",
  "args": [
    {"name": "SourceAddress", "value": "0x0E80"},
    {"name": "ECUAddress", "value": "0x1D01"},
    {"name": "EcuBOMName", "value": "ZCUD1D01"},
    {"name": "DID", "value": "0xD134"},
    {"name": "RequestParameter", "value": "0x00"}
  ],
  "flow_constraints": {
    "requires_session": "extended",
    "requires_security": "level_3",
    "final_readback_required": true,
    "retry_policy": {"write_retry": 3, "readback_retry": 3}
  },
  "evidence_ids": ["uds_bcm_v1_0012", "flow_003_node_02"]
}
```

### 7.8 规则校验与图一致性校验

XML 渲染前必须先校验 DSL。校验分三层：

| 层级 | 校验内容 |
| --- | --- |
| Schema 校验 | 字段是否完整，类型是否正确，必填 Args 是否存在 |
| 诊断规则校验 | Session 顺序、Security Access 成对、响应长度保护、重试预算、最终读回 |
| 图一致性校验 | `next_step`、`branch_to`、`retry_to` 是否能形成可达流程，是否存在孤立节点或死分支 |

对流程图跳转相关场景，应重点检查：

- 判断节点是否至少有两个明确出口；
- 每条分支是否能到达成功、失败或重试终点；
- 重试节点是否有递减条件和退出条件；
- 错误分支是否绑定错误码；
- 并行块内部节点是否都能独立闭环；
- 跨页引用是否能在图谱中找到唯一目标。

### 7.9 XML 模板渲染

通过校验后再进入 XML renderer。渲染器应负责：

- 根据 `template_type` 选择 TaskNode / ScriptNode 模板；
- 按 `XmlArgSet` 注入 Args；
- 保留项目真实 XML 的标签、属性顺序、命名风格；
- 生成单步骤 XML；
- 按 `flow.xlsx` 列串行、同列跨行并行生成结构化 `SerialNode / ParallelNode` XML；
- 生成 ECU 参数片段 XML；
- 生成完整 workflow XML。

渲染时必须遵守：

- 不让 LLM 直接拼接完整 XML；
- 不保留 `{{...}}` 占位符；
- 不把真实并行/顺序结构压缩成简化节点；
- 不随意改写已有 `ScriptNode Name` 和 `ClassName`；
- 不生成缺少关键 Args 的节点。

### 7.10 主流程融合

最终 workflow XML 不应从零生成，而应基于真实主流程结构进行融合。

推荐流程：

```text
扫描历史 XML
  -> 选择最完整 PhaseNode 作为代表结构
  -> 根据 ScriptNode Name 对齐新生成参数
  -> 刷新 Args / ClassName
  -> 保留原有 SerialNode / ParallelNode / Expression / ScriptType
  -> 输出完整 workflow XML
```

主流程融合的目标是保留工程真实结构，同时更新当前 PDF 和参数中推导出的节点参数。

当前实现采用保守融合策略：

- 默认按 `ScriptNode Name` 匹配已有节点；
- 默认保留历史 XML 中非空 Args，不用检索结果覆盖人工确认值；
- 默认只填充已有但为空的 Args，不向脚本类追加未知参数；
- 只有显式启用覆盖或追加时，才刷新已有非空值或追加新 Arg；
- 保留 `SerialNode`、`ParallelNode`、`Expression`、`ScriptType` 等主流程结构。

### 7.11 最终 XML 校验

生成后必须输出校验报告。至少检查：

- XML 是否可解析；
- 是否残留模板占位符；
- `PhaseNode`、`SerialNode`、`ParallelNode`、`ScriptNode` 层级是否合法；
- `START`、`NORMAL`、`END` 是否完整；
- `ScriptNode` 数量是否异常减少；
- 关键 Args 是否为空；
- `Expression` 是否放在正确层级；
- 图谱中的流程跳转是否都能映射到 XML 节点；
- 所有生成节点是否能追溯到 PDF、表格、流程图或模板证据。

### 7.12 参数差异审计

workflow 融合完成后，需要生成参数审计报告，避免检索噪声静默污染历史 XML。审计报告以 `ScriptNode Name + ArgName` 为主键，对比三份数据：

- 原始主流程 XML；
- `XmlGenerationPlan` 中的计划参数和 evidence ids；
- 融合后的 workflow XML。

审计状态至少包括：

- `confirmed_by_plan`：历史值、计划值、融合值一致；
- `base_only_unchanged`：该参数只存在于历史 XML，融合保持不变；
- `filled_from_plan`：历史 XML 中已有空参数，融合用计划值补齐；
- `appended_from_plan`：显式允许追加时，计划参数被追加到 XML；
- `plan_conflict_kept_base`：计划值与历史值冲突，保守融合保留历史值；
- `plan_arg_missing_in_fused`：计划参数未进入融合结果，通常表示脚本类没有该 Arg；
- `workflow_changed_without_plan`：融合结果出现非计划内变化。

默认验收原则是：报告可以有 warning，但不能有 error；`plan_conflict_kept_base` 和 `plan_arg_missing_in_fused` 必须进入人工复核清单。

### 7.13 模板参数契约与审计解析

为了避免把诊断语义参数误写入 XML 脚本参数，需要从历史 XML 中推断 `ClassName -> ArgName` 参数契约。

契约规则：

- 同一个 `ClassName` 下出现过的 `ArgName` 视为该脚本类允许参数；
- 在该 `ClassName` 的所有样本中都出现的参数视为 required；
- 只在部分样本中出现的参数视为 optional；
- 没出现在契约中的参数默认不写入 workflow。

审计解析规则：

- `confirmed_by_plan`、`base_only_unchanged`、`filled_from_plan` 等状态直接通过；
- `plan_arg_missing_in_fused` 且参数不在脚本类契约中，自动标记为 `ignore_plan_arg`；
- `plan_arg_missing_in_fused` 且参数在契约中，保留为复核或后续补写候选；
- `plan_conflict_kept_base` 默认执行 `keep_base`，但保留复核项；
- 节点缺失、非计划变更、融合值与计划/历史均不一致时进入复核。

这一步的目标是把大批“LLM/检索抽出的语义字段”与“XML 脚本真实入参”分离，只把真正可能影响工程 XML 的冲突交给人工或 LLM resolver。

### 7.14 端到端 XML API

XML 生成链路可以逐步拆成以下 API：

| Method | Path | 职责 |
| --- | --- | --- |
| `POST` | `/api/v1/xml/flow/parse` | 解析 `flow.xlsx` 为 `FlowStepPlan` |
| `POST` | `/api/v1/xml/evidence/build` | 为每个流程步骤构建证据包 |
| `POST` | `/api/v1/xml/graph/build` | 从知识单元构建诊断流程图谱 |
| `POST` | `/api/v1/xml/plan/generate` | 生成 XML 中间 DSL |
| `POST` | `/api/v1/xml/plan/validate` | 校验 XML DSL 和图一致性 |
| `POST` | `/api/v1/xml/templates/contracts/build` | 推断模板参数契约 |
| `POST` | `/api/v1/xml/task/render` | 渲染单步骤 TaskNode / ScriptNode XML |
| `POST` | `/api/v1/xml/workflow/render` | 融合并渲染完整 workflow XML |
| `POST` | `/api/v1/xml/workflow/audit` | 输出参数差异和人工复核报告 |
| `POST` | `/api/v1/xml/workflow/resolve-audit` | 解析审计报告并生成复核决策 |
| `POST` | `/api/v1/xml/workflow/resolve-audit-llm` | 使用 vLLM 对仍需复核的冲突项做 JSON 决策 |
| `POST` | `/api/v1/xml/generate` | 端到端生成最终 XML |

### 7.15 pip 方式接入 vLLM

当前项目先采用 pip 安装 vLLM，而不是源码编译。原因是现阶段 LLM 只介入“结构化/仲裁/修复建议”这类低频节点，不在主链路中承担大规模并行生成，因此优先保证接入稳定和工程闭环。

已固化的运行方式：

```bash
scripts/install_vllm_pip.sh
scripts/start_vllm_qwen.sh
```

默认配置：

- 依赖版本固定为 `vllm==0.19.1`；
- 模型原始目录为 `/home/xdu/huggingface/Qwen3-VL-30B-A3B-Instruct-Q8_0-GGUF`；
- 启动脚本会在 `/tmp/vllm-qwen3-vl-30b-a3b-instruct-q8_0-gguf` 组装 GGUF 权重、`mmproj`、tokenizer 和 HF config 软链接；
- 服务名为 `qwen-audit-resolver`；
- OpenAI-compatible 地址为 `http://127.0.0.1:8008/v1`；
- 默认使用 `CUDA_VISIBLE_DEVICES=0,1` 和 `--tensor-parallel-size 2`；
- 默认限制 `--max-model-len 8192`，避免 Qwen3-VL 超长上下文配置导致 KV cache 预分配过大。

LLM 输出必须保持为可解析 JSON，不允许直接输出 workflow XML。当前 prompt 只允许以下动作：

- `keep_base`：保留历史 workflow 值；
- `accept_plan`：接受 XML plan 推导值；
- `needs_review`：证据不足，继续人工复核。

低置信度或非法 JSON 一律降级为 `needs_review`。这样可以让 vLLM 辅助减少人工复核量，但不会绕过模板参数契约、XML 校验和人工确认边界。

### 7.16 实现优先级

第一阶段先实现确定性骨架：

1. `MinerU -> KnowledgeUnit`；
2. `flow.xlsx -> FlowStepPlan`；
3. `KnowledgeUnit -> GraphEntity / GraphRelation`；
4. `FlowStepPlan + Hybrid Retrieval + GraphRAG -> StepEvidenceBundle`；
5. 已实现 `StepEvidenceBundle -> XmlGenerationPlan` 确定性骨架；
6. 已实现 `XmlGenerationPlan -> XML DSL Validator` 基础校验；
7. 已实现 `XmlGenerationPlan -> workflow XML conservative fusion`，默认保留历史主流程结构和脚本参数契约；
8. 已实现 `workflow arg audit report`，输出参数确认、冲突、缺失和人工复核清单；
9. 已实现 `template contract + audit resolver`，自动忽略非脚本参数并压缩真实复核项。

第二阶段接入 Qwen/vLLM：

1. 已实现 `generation/vllm_client.py`、`prompt_builder.py` 和 `resolution/llm_resolver.py`，通过 OpenAI-compatible chat 接口处理剩余审计冲突；
2. 对模板候选做 LLM rerank；
3. 对校验失败结果生成修复建议；
4. 保持最终 XML 仍由 renderer 生成。

第三阶段建立回归评测：

1. 已建立首个固定 workload：`treg_20260402`；
2. 已实现 `evaluation/regression.py` 和 `scripts/run_regression_eval.py`；
3. 已固化 `configs/evaluation/treg_20260402_regression.json`，检查 `xml_plan`、trace、GraphRAG、单操作 XML、汇总 XML、融合 workflow、审计摘要和关键参数；
4. 后续扩展为多 workload 对比，继续补节点覆盖率、参数准确率、图路径一致性和 GraphRAG ablation 指标。

当前运行方式：

```bash
python scripts/run_regression_eval.py --print-checks
```

默认输出：

```text
data/reports/regression/treg_20260402_regression_report.json
```

首版评测指标包括：

- artifact 完整性；
- `xml_plan.validation.valid`；
- flow step、planned node、operation XML 数量；
- 单操作 XML、汇总 `serial_node.xml`、融合 `fused_workflow.xml` XML 合法性；
- GraphRAG entity / relation / path 数量；
- retrieval 和 graph path 覆盖率；
- `XmlArg.selection_score` 覆盖率；
- 审计 `review_items` 和 resolution `review_required` 上限；
- 关键参数断言，例如 `FAH_CLEAR_DTC.EcuBOMName == FAH`。

### 7.17 一键端到端 Pipeline

整体工程流程以 workload manifest + pipeline runner 作为主入口，避免开发过程中手工串多个脚本导致产物不一致。

当前默认 workload：

```bash
python scripts/run_workload_pipeline.py --workload configs/workloads/treg_20260402.json --print-steps
```

workload manifest：

```text
configs/workloads/treg_20260402.json
```

默认报告：

```text
data/reports/pipeline/treg_20260402_pipeline_report.json
```

当前 pipeline 步骤：

1. `generate_xml_plan`：解析 `flow.xlsx`、构建 evidence bundle、GraphRAG path、XML plan、单操作 XML、trace；
2. `fuse_workflow`：把 XML plan 保守融合进历史 workflow；
3. `audit_workflow_args`：对比 base / plan / fused 参数；
4. `resolve_audit_report`：用模板参数契约自动解析审计项；
5. `resolve_audit_with_llm`：默认禁用，需要 vLLM 服务可用时通过 `--enable-step resolve_audit_with_llm` 显式打开；
6. `regression_eval`：执行固定 workload 回归评测。

设计约束：

- pipeline 配置只描述步骤顺序、命令、是否必需、是否启用和超时时间；
- 必需步骤失败时立即停止；
- 非必需步骤失败不会阻断后续回归评测；
- pipeline 报告必须保存每个步骤的 `return_code`、耗时、stdout/stderr 和可解析 JSON 摘要；
- 后续新增 workload 时，只新增 `configs/pipeline/*.json` 和 `configs/evaluation/*.json`，不复制脚本逻辑。
- 后续新增 workload 时，优先新增 `configs/workloads/*.json`；低层 `configs/pipeline/*.json` 只用于特殊调度实验。

### 7.18 系统 Benchmark

Benchmark 以场景配置驱动，默认覆盖端到端 pipeline 和回归评测两个确定性场景，先形成稳定基线，再把 vLLM 推理作为可选场景追加。默认不启用 vLLM 场景，避免本地服务未启动、模型未加载或 GPU 被占用时影响确定性回归。

默认运行：

```bash
python scripts/run_system_benchmark.py
```

默认配置：

```text
configs/benchmark/treg_20260402_system.json
```

测试矩阵：

```text
docs/benchmark_matrix.md
```

默认报告：

```text
data/reports/benchmark/treg_20260402_system_benchmark.json
```

当前场景：

1. `pipeline_no_llm`：执行 `scripts/run_workload_pipeline.py`，验证整条非 LLM 主流程耗时和成功率；
2. `regression_only`：执行 `scripts/run_workload_regression.py`，验证固定 workload 回归评测耗时和成功率；
3. `vllm_chat_stream`：调用 OpenAI-compatible vLLM `/chat/completions`，采集 TTFT、TPOT、tokens/s 和输出有效性，默认禁用。

启用 vLLM 推理 benchmark：

```bash
python scripts/run_system_benchmark.py --enable-scenario vllm_chat_stream --iterations 1
```

执行完整扩展矩阵：

```bash
python scripts/run_complete_benchmark.py
```

vLLM 场景前置条件：

- `scripts/start_vllm_qwen.sh` 已启动并监听 `http://127.0.0.1:8008/v1`；
- 模型名与服务启动参数一致，当前 benchmark 默认使用 `qwen-audit-resolver`；
- GPU 显存没有被 MinerU 或其他服务占满；
- prompt 输出必须保持可解析、短文本或 JSON，benchmark 不直接接受 workflow XML 作为模型输出。

首版 benchmark 指标：

- 每个场景的成功率、失败数、超时数；
- 命令级 `duration_min/mean/p50/p95/max`；
- vLLM 输出 JSON 中的 `ttft_seconds`、`tpot_seconds`、`tokens_per_second`、`completion_tokens` 聚合指标；
- 阈值检查结果，例如 `success_rate_min`、`duration_p95_seconds_max`、`stdout_ttft_seconds_p95_max`。

## 8. 首批开发顺序

1. 拆分 `api/routes_*.py`，避免 `app.py` 继续膨胀。
2. 实现 `ingestion/mineru_client.py`，用 CLI 或 Python API 调用 `/home/xdu/MinerU`。
3. 已实现 `indexing/local_builder.py`，先落本地 JSONL + 轻量诊断图谱，dense store 可后补 Faiss。
4. 已实现 `retrieval/local_sparse.py`，先提供无外部依赖的 BM25 风格 sparse 检索；后续再扩展 hybrid merge + score normalize。
5. 已实现 `generation/vllm_client.py` 和 `prompt_builder.py`，对接 OpenAI-compatible vLLM；下一步扩展到模板候选 rerank 和校验失败修复建议。
6. 扩展 `validation/rules.py`，加入 DID、NRC、timeout、retry、session/security 规则。
7. 已实现 `evaluation/regression.py` 和 `evaluation/benchmark.py`，固定 `treg_20260402` workload、回归指标、系统 benchmark 和 vLLM 推理场景。
8. 已实现 `orchestration/pipeline.py`、`orchestration/workload.py`、`scripts/run_pipeline.py` 和 `scripts/run_workload_pipeline.py`，形成 manifest 驱动的端到端一键流程；后续扩展多 workload manifest。

## 9. 最小可验收标准

| 能力 | 验收标准 |
| --- | --- |
| 文档解析 | 10 份 PDF 能通过 MinerU 生成 Markdown 和 `content_list.json` |
| 知识入库 | 每份文档能生成可追溯 `KnowledgeUnit` |
| 检索 | 给定 ECU/module/service 能返回相关 chunk |
| 生成 | 模型能稳定输出 `DiagnosticPlan` JSON |
| 校验 | 错误的 security access 顺序会被拦截 |
| 渲染 | 通过校验的 DSL 能渲染为 C 函数模板 |
| Regression | 固定 workload 能输出 XML 合法性、trace 覆盖率、GraphRAG 覆盖率、审计复核项和关键参数断言 |
| Benchmark | 能输出 pipeline / regression 的 P50/P95，以及 vLLM 场景的 TTFT、TPOT、tokens/s 和成功率 |
