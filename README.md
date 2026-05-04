# Vehicle Diagnostic XML Generation Platform

这是一个面向车载诊断资料的 XML / 代码生成平台。项目目标不是做通用聊天问答，也不是单纯部署 vLLM，而是把 PDF、流程表、历史 XML 模板、流程图说明等资料组织成可检索、可追溯、可校验的证据链，然后让大模型在受控上下文中生成单个诊断节点的 XML 代码，最后经过系统渲染、融合、审计和回归测试。

当前主线可以概括为：

```text
MinerU 多产物
  -> Markdown semantic chunk + content_list provenance
  -> KnowledgeUnit / EvidenceUnit
  -> sparse retrieval + Milvus dense retrieval + hybrid merge
  -> Graph RAG / reference expansion / evidence filter
  -> evidence chain report
  -> TemplateFamilyResolver
  -> LLM 生成单节点 ClassName / TaskNode XML
  -> XML validator / guardrail / workflow fusion / audit / regression
```

## 项目总览

### 解决的问题

车企诊断自动化资料通常分散在多类文件中：

- ECU 参数 PDF：包含 DID、ServiceID、DTC 表格、请求参数、NRC allowed、Refer 信息等。
- EOL Process PDF：包含公共流程图、TypeN 流程模板、请求响应顺序、重试逻辑等。
- `flow.xlsx`：定义主流程中的 FlowNode 和 TemplateClass，例如 `ADCU1301_DTC_Clear(ADCU1301_DTC_Clear_Type7)`。
- 历史 XML：提供已有 ScriptNode / TaskNode / ClassName 的结构样例。
- 总流程 XML：例如 `P177__FHC.xml`，是多个 ScriptNode 汇总后的完整工作流。

人工写 XML 时，需要先定位 ECU 文档，再根据 `Refer to ...` 跳到公共 EOL 流程章节，结合表格、流程图和历史 XML 模板，最后生成可执行的节点 XML。这个过程很容易出现参数遗漏、ECU 混淆、流程图漏读、模板结构不一致等问题。

本项目把这个过程拆成几个可工程化的环节：

- 检索层负责找到候选证据。
- Graph RAG 负责把候选证据组织成节点级 evidence bundle。
- Evidence chain report 负责让人能检查每个节点最终用了哪些证据。
- TemplateFamilyResolver 负责把 FlowNode 对齐到历史 XML 模板族。
- LLM 只生成单节点 XML，不直接生成完整总流程。
- Validator、guardrail、audit、regression 负责兜底校验和效果度量。

### 当前已实现能力

- MinerU 输出解析与本地 JSONL 索引构建。
- Markdown 语义 chunk 和 `content_list.json` 版面溯源结合的 KnowledgeUnit / EvidenceUnit。
- sparse 检索、Milvus dense 检索、hybrid merge。
- FlowNode 级 QueryPlan、reference expansion、多跳 refer-to 解析。
- EvidenceFilter：过滤明显 ECU 冲突、目录噪声、弱 reference 匹配等。
- Graph RAG：基于 FlowNode、TemplateClass、文档 metadata、section/table 层级、refer-to 和模板族关系组织 evidence bundle。
- Evidence chain JSON / Markdown 报告。
- LLM XML 生成链路：prompt 保存、raw response 保存、post-guardrail 结果保存。
- TemplateFamilyResolver：从历史 XML 和流程图说明中构建模板族，并为节点选择匹配模板。
- TaskNode / ClassName XML 生成与校验。
- workflow fusion、参数 audit、audit resolution、workload regression。
- vLLM OpenAI-compatible 调用、Milvus 真库接入、pipeline runner 和 benchmark 脚本。

### 当前验证结果

最近一次 `treg_20260402_semantic_hybrid_llm` workload 跑通结果：

- flow steps：8
- script nodes：11
- LLM valid generations：11/11
- needs_review：2
- operation retrieval coverage：1.0
- operation dense retrieval coverage：1.0
- operation hybrid retrieval coverage：1.0
- operation graph path coverage：1.0
- regression checks：58，failed：0
- raw LLM critical arg accuracy：0.2
- post-guardrail critical arg accuracy：1.0
- guardrail correction count：12

这个结果说明：模型原始输出仍然不适合直接信任，必须通过 evidence、deterministic plan、semantic default、validator 和 audit 做可追溯修正。

## 本地准备

### Python 环境

推荐使用项目内 `.venv`：

```bash
python -m venv .venv
source .venv/bin/activate
.venv/bin/pip install -e .
```

向量检索需要额外依赖：

```bash
.venv/bin/pip install -r requirements/vector.txt
```

vLLM 服务需要额外依赖：

```bash
.venv/bin/pip install -r requirements/vllm.txt
```

运行单元测试：

```bash
.venv/bin/python -m unittest discover -s tests
```

当前全量单测结果为：

```text
Ran 97 tests
OK (skipped=1)
```

### Milvus 服务

默认 Milvus URI：

```text
http://127.0.0.1:19530
```

如果本机已经有 Milvus docker 容器，可以直接启动：

```bash
docker start milvus-etcd
docker start milvus-minio
docker start milvus-standalone
```

确认服务可用后再构建向量索引。真实集成测试不会静默降级到 sparse：

```bash
RUN_MILVUS_INTEGRATION=1 MILVUS_URI=http://127.0.0.1:19530 \
  .venv/bin/python -m unittest tests.test_milvus_integration
```

### vLLM 服务

当前 workload 默认调用 OpenAI-compatible 服务：

```text
base_url: http://127.0.0.1:8000/v1
model: /media/xdu/新加卷/LLM_model/Qwen3.6-27B-FP8
```

本地启动示例：

```bash
ln -sfn "/media/xdu/新加卷/LLM_model/Qwen3.6-27B-FP8" /tmp/Qwen3_5_27B_FP8

.venv/bin/vllm serve /tmp/Qwen3_5_27B_FP8 \
  --served-model-name "/media/xdu/新加卷/LLM_model/Qwen3.6-27B-FP8" \
  --hf-config-path /tmp/Qwen3_5_27B_FP8 \
  --model-impl vllm \
  --host 0.0.0.0 \
  --port 8000 \
  --tensor-parallel-size 2 \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.90 \
  --max-num-seqs 1 \
  --max-num-batched-tokens 8192 \
  --dtype float16 \
  --reasoning-parser qwen3 \
  --language-model-only \
  --trust-remote-code \
  --enforce-eager \
  --enable-prefix-caching
```

服务启动后检查：

```bash
curl -sS http://127.0.0.1:8000/v1/models
```

## 核心链路

### 1. MinerU 多产物解析

MinerU 不是只生成一个 Markdown。项目使用的输入通常包括：

- `.md`：更接近语义结构，适合解析标题、章节、表格文本和流程说明。
- `content_list.json`：版面级 block，保留 `page_idx`、`bbox`、`img_path`、block type 等溯源信息。
- `middle.json`：更底层的版面调试信息。
- `images/`：表格、流程图或页面区域截图。

当前技术路线是：

```text
Markdown 做语义主干
content_list.json 做版面溯源
middle.json / images 做调试和原始区域证据
```

这样可以避免直接把 `content_list.json` 的 text line 当成最终 RAG chunk，也避免只依赖 Markdown 导致 page/bbox/source image 丢失。

### 2. KnowledgeUnit / EvidenceUnit

`KnowledgeUnit` 和 `EvidenceUnit` 都来自 MinerU 文档规范化，但职责不同：

- `KnowledgeUnit`：偏检索索引，服务 sparse/dense 初召回。
- `EvidenceUnit`：偏证据表达，服务 Graph RAG、LLM context、trace、audit。

重要 metadata 包括：

- `doc_id`
- `module`
- `ecu`
- `source_path`
- `page_idx`
- `bbox`
- `section_title`
- `unit_type`
- `evidence_type`
- `service_ids`
- `dids`
- `source_block_ids`
- `parent_evidence_id`

对表格类证据，系统会尽量保留 parent table / parent section。命中单行 `table_row` 时，最终给 LLM 的上下文不只是一行，而是会把父表格或父章节一起带上，避免 DTC Clear 这类节点只看到第一行表格。

### 3. sparse / dense / hybrid retrieval

检索分三路：

- sparse：适合 DID、ServiceID、ECU 名称、TemplateClass、TypeN 等精确 token。
- dense：通过 embedding + Milvus 做语义相似召回，适合 `DTC Read` 和 `Read DTC` 这种词序不同但语义接近的表达。
- hybrid：对 sparse 和 dense 结果按 evidence id 去重、score normalize、加权合并。

当前默认 Milvus collection：

```text
diagnostic_knowledge_treg_20260402_semantic_hybrid
```

默认 embedding 模型：

```text
/home/xdu/LLM/models/Qwen3-Embedding-0.6B
```

为了避免和 vLLM 抢显存，建议向量索引构建时单独使用 GPU，运行生成链路时 query embedding 改为 CPU：

```bash
EMBEDDING_DEVICE=cuda:0 EMBEDDING_MAX_SEQ_LENGTH=512 \
.venv/bin/python scripts/build_vector_index.py \
  --index-dir data/index/treg_20260402_semantic_hybrid \
  --collection-name diagnostic_knowledge_treg_20260402_semantic_hybrid \
  --milvus-uri http://127.0.0.1:19530 \
  --embedding-model /home/xdu/LLM/models/Qwen3-Embedding-0.6B \
  --metric-type COSINE \
  --batch-size 1024 \
  --milvus-timeout-seconds 10 \
  --drop-existing
```

### 4. Graph RAG 和 evidence bundle

hybrid retrieval 只负责“召回候选”，Graph RAG 负责“组织上下文”。

对每个 FlowNode，系统先解析 `NodeIntent`：

- node name，例如 `ADCU1301_DTC_Clear`
- template class，例如 `ADCU1301_DTC_Clear_Type7`
- ECU-like token，例如 `ADCU1301`
- operation words，例如 `DTC Clear`
- TypeN，例如 `Type7`

然后生成多类 query：

- anchor query：`node_name + template_name + raw`
- ecu doc query：`target_ecu + operation words + TREG Parameter`
- public process query：`EOL process + operation/type words`
- template query：`template_name + XML + ScriptNode Args`
- reference query：从候选证据里的 `Refer to ...` 动态生成

Graph RAG 会基于以下关系把候选组织成 evidence bundle：

- FlowNode 和 TemplateClass 的显式匹配。
- ECU parameter 文档和当前节点 ECU 的一致性。
- EOL Process 公共流程文档和 `Refer to ...` 的跳转关系。
- Markdown section / table / table row 的父子层级。
- content_list 的 page/bbox/source image 溯源。
- TemplateFamilyResolver 得到的模板族关系。
- graph path 中的 entity/relation/provenance。

EvidenceFilter 不做每个节点的强规则，而是做通用过滤：

- 明确属于其他 ECU 的 parameter 文档降权或剔除。
- 目录、revision log、页眉页脚、空内容等噪声剔除。
- 公共 EOL Process 文档不要求 ECU 匹配。
- reference 标题弱匹配时降权。

### 5. Evidence chain report

每个节点最终会生成可读证据链报告，格式类似：

```text
第 N 步
FlowNode(TemplateClass)
参数来源 Markdown / 行号 / PDF / 页码 / 参数页图片
Refer 信息
最终流程标题 / 流程图状态 / 流程图图片
参数文字
参数页截图
最终流程图
```

这个报告不替代 JSON trace，而是方便人工检查每个 FlowNode 最终给 LLM 的证据是否正确。

### 6. TemplateFamilyResolver

`TemplateFamilyResolver` 用于把当前 FlowNode 对齐到可复用的 XML 模板族。

它会读取历史 XML 模板，抽取：

- `ClassName`
- root tag
- TaskNode / ScriptNode 结构
- ArgName 列表
- 必填/可空参数
- 重复结构模式
- 示例 XML 片段

同时可以读取流程图说明 Markdown，将 `DTC Clear Type7`、`DTC Clear Type2`、`DTC Read Type1` 等流程说明和 XML 模板族关联起来。

这一步的目标不是只支持 DTC Clear，而是为不同类型节点建立统一的模板族抽象。后续只要补充更多 XML 模板和流程图说明，就可以扩展更多 FlowNode 类型。

### 7. LLM XML 生成与校验

LLM 不直接生成 `P177__FHC.xml` 这种完整总流程，而是生成单个节点的 ClassName / TaskNode XML。

例如：

- `ASDM1401_DTC_Clear_Type7.xml`
- `ADCU1301_DTC_Clear_Type7.xml`
- `GEEA30_VMM_Change.xml`
- `GEEA30_Request_Open_HV_Battery.xml`

系统再把这些节点 XML 汇总到 serial XML，必要时和 base workflow 融合。

LLM 输入主要包含：

- 当前 FlowNode / TemplateClass。
- 检索和 Graph RAG 组织后的 evidence bundle。
- 完整表格或父章节上下文。
- reference expansion 得到的流程图说明。
- TemplateFamilyResolver 选中的 XML 模板族。
- XML 输出格式约束。

LLM 输出会进入：

- XML 格式校验。
- Template family 校验。
- critical arg guardrail。
- base workflow conflict audit。
- regression checks。

## 运行命令

### 1. 构建本地语义索引

```bash
.venv/bin/python scripts/build_mineru_index.py \
  --mineru-output-dir data/processed/mineru/treg_20260402_api/extracted \
  --index-dir data/index/treg_20260402_semantic_hybrid \
  --collection-name treg_20260402_semantic_hybrid \
  --protocol UDS \
  --doc-type pdf_protocol \
  --source mineru \
  --chunking-mode semantic
```

### 2. 构建 Milvus 向量索引

```bash
EMBEDDING_DEVICE=cuda:0 EMBEDDING_MAX_SEQ_LENGTH=512 \
.venv/bin/python scripts/build_vector_index.py \
  --index-dir data/index/treg_20260402_semantic_hybrid \
  --collection-name diagnostic_knowledge_treg_20260402_semantic_hybrid \
  --milvus-uri http://127.0.0.1:19530 \
  --embedding-model /home/xdu/LLM/models/Qwen3-Embedding-0.6B \
  --metric-type COSINE \
  --batch-size 1024 \
  --milvus-timeout-seconds 10 \
  --drop-existing
```

向量检索 smoke query：

```bash
EMBEDDING_DEVICE=cpu \
.venv/bin/python scripts/query_vector_index.py \
  --index-dir data/index/treg_20260402_semantic_hybrid \
  --collection-name diagnostic_knowledge_treg_20260402_semantic_hybrid \
  --milvus-uri http://127.0.0.1:19530 \
  --embedding-model /home/xdu/LLM/models/Qwen3-Embedding-0.6B \
  --query "ADCU1301 DTC Clear Type7"
```

### 3. 构建模板族 registry

```bash
.venv/bin/python scripts/build_template_family_registry.py \
  --xml-template-dir data/xml_template \
  --flowchart-markdown 流程图.md \
  --output-path data/processed/template_family_registry/template_families.json \
  --max-example-chars 6000
```

### 4. 只生成 evidence plan 和证据链报告

```bash
EMBEDDING_DEVICE=cpu \
.venv/bin/python scripts/generate_xml_plan.py \
  --flow-path pdf-loop/20260402_new_v2/0402/0402/flow.xlsx \
  --index-dir data/index/treg_20260402_semantic_hybrid \
  --output-path data/processed/xml_plan/treg_20260402_semantic_hybrid_llm/baseline/xml_plan.json \
  --xml-output-path data/processed/xml_plan/treg_20260402_semantic_hybrid_llm/baseline/serial_node.xml \
  --operation-xml-dir data/processed/xml_plan/treg_20260402_semantic_hybrid_llm/baseline/operations \
  --trace-output-path data/processed/xml_plan/treg_20260402_semantic_hybrid_llm/baseline/flow_evidence_trace.json \
  --evidence-chain-output-path data/processed/xml_plan/treg_20260402_semantic_hybrid_llm/baseline/evidence_chain_report.json \
  --evidence-chain-markdown-output-path data/processed/xml_plan/treg_20260402_semantic_hybrid_llm/baseline/evidence_chain_report.md \
  --graph-output-path data/processed/xml_plan/treg_20260402_semantic_hybrid_llm/baseline/diagnostic_graph.json \
  --top-k-per-node 5 \
  --graph-max-paths-per-node 12 \
  --graph-max-depth 2 \
  --serial-name MAC_ALL \
  --enable-dense \
  --vector-config configs/retrieval/milvus_vector.json \
  --dense-top-k 8 \
  --hybrid-top-k 8 \
  --dense-weight 0.6 \
  --sparse-weight 0.4 \
  --milvus-uri http://127.0.0.1:19530 \
  --vector-collection diagnostic_knowledge_treg_20260402_semantic_hybrid \
  --embedding-model /home/xdu/LLM/models/Qwen3-Embedding-0.6B
```

### 5. 运行完整 workload pipeline

如果向量索引已经构建完成：

```bash
EMBEDDING_DEVICE=cpu \
.venv/bin/python scripts/run_workload_pipeline.py \
  --workload configs/workloads/treg_20260402_semantic_hybrid_llm.json \
  --enable-llm \
  --print-steps \
  --progress \
  --skip-step build_vector_index \
  --skip-step resolve_audit_with_llm \
  --enable-dense \
  --vector-config configs/retrieval/milvus_vector.json \
  --vector-collection diagnostic_knowledge_treg_20260402_semantic_hybrid \
  --milvus-uri http://127.0.0.1:19530 \
  --embedding-model /home/xdu/LLM/models/Qwen3-Embedding-0.6B
```

如果 evidence plan 已经生成，只想重跑 LLM XML、融合、审计和回归：

```bash
EMBEDDING_DEVICE=cpu \
.venv/bin/python scripts/run_workload_pipeline.py \
  --workload configs/workloads/treg_20260402_semantic_hybrid_llm.json \
  --enable-llm \
  --print-steps \
  --progress \
  --skip-step build_vector_index \
  --skip-step generate_xml_plan \
  --skip-step resolve_audit_with_llm \
  --enable-dense \
  --vector-config configs/retrieval/milvus_vector.json \
  --vector-collection diagnostic_knowledge_treg_20260402_semantic_hybrid \
  --milvus-uri http://127.0.0.1:19530 \
  --embedding-model /home/xdu/LLM/models/Qwen3-Embedding-0.6B
```

### 6. 单独生成 ClassName / TaskNode XML

只保存 prompt，不调用 LLM：

```bash
.venv/bin/python scripts/generate_class_xml_with_llm.py \
  --flow-path pdf-loop/20260402_new_v2/0402/0402/flow.xlsx \
  --index-dir data/index/treg_20260402_semantic_hybrid \
  --template-family-registry data/processed/template_family_registry/template_families.json \
  --output-dir data/processed/class_xml/treg_20260402 \
  --node-name ASDM_DTC_Clear \
  --node-limit 1
```

调用 LLM 生成节点 XML：

```bash
EMBEDDING_DEVICE=cpu \
.venv/bin/python scripts/generate_class_xml_with_llm.py \
  --flow-path pdf-loop/20260402_new_v2/0402/0402/flow.xlsx \
  --index-dir data/index/treg_20260402_semantic_hybrid \
  --template-family-registry data/processed/template_family_registry/template_families.json \
  --output-dir data/processed/class_xml/treg_20260402 \
  --node-name ASDM_DTC_Clear \
  --node-limit 1 \
  --enable-dense \
  --vector-collection diagnostic_knowledge_treg_20260402_semantic_hybrid \
  --milvus-uri http://127.0.0.1:19530 \
  --embedding-model /home/xdu/LLM/models/Qwen3-Embedding-0.6B \
  --call-llm \
  --vllm-base-url http://127.0.0.1:8000/v1 \
  --vllm-model "/media/xdu/新加卷/LLM_model/Qwen3.6-27B-FP8" \
  --vllm-api-key EMPTY \
  --vllm-timeout-seconds 300 \
  --vllm-temperature 0.0 \
  --vllm-max-tokens 8192 \
  --disable-thinking
```

### 7. 回归测试

```bash
.venv/bin/python scripts/run_workload_regression.py \
  --workload configs/workloads/treg_20260402_semantic_hybrid_llm.json \
  --output-path data/reports/regression/treg_20260402_semantic_hybrid_llm_regression_report.json \
  --use-llm-xml
```

## 输出物说明

### 本地索引输出

构建索引后主要输出：

```text
data/index/treg_20260402_semantic_hybrid/
  documents.jsonl
  knowledge_units.jsonl
  evidence_units.jsonl
  diagnostic_graph.json
  kg_nodes.jsonl
  kg_edges.jsonl
  kg_provenance.jsonl
  kg_manifest.json
  summary.json
```

这些文件用于 sparse retrieval、Graph RAG、evidence bundle 和回归统计。

### 向量索引输出

Milvus 中保存 dense 向量 collection：

```text
diagnostic_knowledge_treg_20260402_semantic_hybrid
```

本地 manifest：

```text
data/index/treg_20260402_semantic_hybrid/vector_manifest.json
```

manifest 记录 collection、embedding model、dimension、metric、源 JSONL hash、写入数量和构建时间。

### Evidence plan 输出

deterministic baseline 和证据链输出：

```text
data/processed/xml_plan/treg_20260402_semantic_hybrid_llm/baseline/
  xml_plan.json
  serial_node.xml
  operations/
  flow_evidence_trace.json
  evidence_chain_report.json
  evidence_chain_report.md
  diagnostic_graph.json
```

其中 `evidence_chain_report.md` 是人工检查每个 FlowNode 证据是否正确的主入口。

### LLM XML 输出

LLM 节点生成输出：

```text
data/processed/llm_xml/treg_20260402_semantic_hybrid_llm/
  llm_xml_plan.json
  serial_node.xml
  operations/
  llm_generation_trace.json
  raw_sources/
    prompts/
```

`raw_sources/prompts/*.json` 保存每个节点实际发送给 LLM 的 prompt。`llm_generation_trace.json` 保存 raw LLM args、post-guardrail args、guardrail corrections、generation latency、prompt token estimate、output token estimate 等信息。

### ClassName XML 输出

单节点 ClassName / TaskNode XML 生成输出：

```text
data/processed/class_xml/treg_20260402/
  prompts/
  templates/
  class_xml_generation_trace.json
```

`templates/*.xml` 是 LLM 生成并通过 TaskNode / template family 校验的节点 XML。

### Workflow fusion 和 audit 输出

完整工作流融合与审计输出：

```text
data/processed/workflow/treg_20260402_semantic_hybrid_llm/
  fused_workflow.xml
  arg_audit_report.json
  audit_resolution_report.json
  llm_resolution_report.json
```

`fused_workflow.xml` 是把生成的节点计划和 base workflow 对齐后的结果。`arg_audit_report.json` 用于检查 base workflow 与 plan 的参数冲突、额外 Arg、缺失 Arg 等。

### Regression 输出

```text
data/reports/regression/treg_20260402_semantic_hybrid_llm_regression_report.json
data/reports/pipeline/treg_20260402_semantic_hybrid_llm_pipeline_report.json
```

重点关注指标：

- `valid`
- `failed_checks`
- `llm_valid_generations`
- `llm_needs_review`
- `operation_retrieval_coverage`
- `operation_dense_retrieval_coverage`
- `operation_hybrid_retrieval_coverage`
- `operation_graph_path_coverage`
- `raw_llm_critical_arg_accuracy`
- `post_guardrail_critical_arg_accuracy`
- `guardrail_correction_count`
- `evidence_chain_report_coverage`
