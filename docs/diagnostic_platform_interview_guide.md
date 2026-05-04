# 车企诊断代码/XML 生成平台面试复盘文档

> 本文只基于当前仓库 `/home/xdu/LLM/vllm-ai_infra` 中已经实现的代码、配置和验收报告整理。  
> 主案例是 `treg_20260402_hybrid_llm` workload。  
> 当前核心产物是诊断 XML workflow 与可审计的中间计划；从工程定位上，它属于“证据驱动的诊断代码/XML 工件生成平台”，不是单纯的大模型部署项目。

## 1. 项目一句话介绍

### 30 秒版本

这个项目把车企诊断资料中的 PDF、流程表、历史 XML 模板和参数表，转成可检索、可追溯、可校验的知识索引，再通过 sparse、Milvus dense、Graph RAG 三路召回构造证据包，最后让本地 vLLM 只生成单节点 XML 计划，由系统负责校验、修正、组装、融合和回归验收。

### 90 秒版本

我做的是一个车企诊断代码/XML 生成平台。它不是让大模型直接自由生成完整 workflow，而是先把 MinerU 解析出来的文档、表格、流程图和历史 XML 模板规范化为 `KnowledgeUnit` 和 `EvidenceUnit`，再构建本地 JSONL sparse index、Milvus 向量库和诊断知识图谱。对 `flow.xlsx` 中的每个操作节点，系统会用 sparse、dense 和 graph 三路召回候选证据，打包成带来源、页码、bbox、evidence id 的上下文。LLM 只负责基于证据生成 operation-level 结构化 XML 节点计划，高风险参数再由 deterministic guardrail、template contract、base workflow 和 audit 校验接管。最终产出 `llm_xml_plan.json`、operation XML、`serial_node.xml`、`fused_workflow.xml`、trace、audit 和 regression report。

当前真实验收已经跑通：Milvus collection 有 `11140` 条向量，三组 scenario 全部 passed，LLM 生成 `11/11` 个节点有效，post-guardrail critical arg accuracy 达到 `1.0`，review items 降到 `2`。

### 3 分钟版本

这个项目的业务背景是，车企诊断开发资料非常分散：诊断步骤在 `flow.xlsx`，参数定义在 PDF/Markdown 的章节和表格，流程关系可能藏在流程图或 `refer to` 跳转里，历史可运行逻辑又在 XML workflow 模板中。传统做法靠人工查表、复制 XML、改参数，容易漏参数、错 ECU、错 DID，也难以证明某个参数来自哪里。

我实现的路线是“模型负责理解和生成，系统负责检索、约束、校验、修复和溯源”。上游用 MinerU 解析文档，把 text、table、image、flowchart 统一成 `KnowledgeUnit` 和 `EvidenceUnit`；检索层保留本地 JSONL sparse index，同时新增 Milvus 向量库，用本地 Qwen3-Embedding 做 dense retrieval；Graph RAG 再把 flow 节点、模板类、ECU、DID、ServiceID、表格行、章节和 refer-to 路径连起来。生成层不让 LLM 一次性生成完整 fused workflow，而是每个 flow operation 生成一个单节点结构化计划，再由系统渲染 XML、融合到 base workflow、跑 audit 和 regression。

为了避免模型幻觉，高风险字段如 `EcuBOMName`、`DID`、`RID`、`ServiceID`、`RequestParameter` 必须有 evidence、base workflow、deterministic plan 或明确 semantic default 支撑。trace 会同时记录 `raw_llm_args` 和 `post_guardrail_args`，并记录每次 guardrail correction 的来源和原因。最终验收里，raw LLM critical accuracy 只有 `0.2`，但经过 guardrail 后达到 `1.0`，这正好证明项目不是盲信模型，而是把模型放进可控闭环。

## 2. 业务背景与问题

### 为什么车企诊断资料难自动化

车企诊断流程不是一份干净的 API 文档，而是一组异构材料共同决定结果：

- PDF/扫描件：包含诊断说明、参数表、流程图、表格和注意事项。
- `flow.xlsx`：定义测试流程节点、串并行关系、operation 名称。
- 历史 XML workflow：包含已经可运行的 `ScriptNode`、`SerialNode`、`ParallelNode` 和大量参数。
- 模板库：同类节点的 `ClassName`、ArgName、默认值模式和必填参数。
- 车型/ECU 参数表：同一个动作在不同 ECU、DID、ServiceID 下参数不同。

难点不是“生成一段 XML 文本”，而是确认每个参数为什么这么填、是否能回到原始证据、是否和 base workflow 冲突、是否能通过回归。

### 人工做法的问题

- 查表成本高：一个 flow 节点可能对应多个 PDF、多个表格行和多个 refer-to 目标。
- 参数容易混淆：同类 DTC Read/Clear 节点只差 ECU 或 DID，人工复制很容易误用旧参数。
- 缺少证据链：XML 里只有最终值，不能反推它来自哪页、哪行、哪个表格。
- 难回归：改完 XML 以后，很难系统性比较 deterministic baseline、LLM 输出和 fused workflow 的差异。
- 难评估模型：如果只看模型输出是否“像 XML”，无法判断关键参数是否正确。

### 这个项目解决什么

这个项目把“人工查资料 + 改 XML + 复核”的过程拆成可自动化、可审计的工程链路：

```text
原始资料
  -> MinerU 解析
  -> KnowledgeUnit / EvidenceUnit
  -> JSONL sparse index + Milvus vector index + diagnostic graph
  -> flow.xlsx 节点级检索
  -> Graph RAG evidence bundle
  -> LLM 单节点 XML/结构化计划
  -> deterministic guardrail
  -> XML 渲染与 workflow fusion
  -> audit / resolution / regression
  -> acceptance report
```

## 3. 整体技术架构

### 3.1 主链路

```text
PDF / Markdown / flow.xlsx / base workflow / XML templates
        |
        v
MinerU 解析与规范化
        |
        v
KnowledgeUnit + EvidenceUnit
        |
        +----------------------+
        |                      |
        v                      v
JSONL sparse index        Milvus dense index
        |                      |
        +----------+-----------+
                   v
             hybrid retrieval
                   |
                   v
Graph RAG: flow node、ECU、DID、ServiceID、template、refer-to path
                   |
                   v
XmlGenerationContext / evidence bundle
                   |
                   v
LLM node XML plan + raw_llm_args
                   |
                   v
validator + deterministic guardrail + repair
                   |
                   v
post_guardrail_args + operation XML + serial_node.xml
                   |
                   v
fused_workflow.xml
                   |
                   v
workflow audit + resolver + regression + acceptance
```

### 3.2 三路召回分别解决什么

| 召回方式 | 解决的问题 | 例子 | 为什么保留 |
| --- | --- | --- | --- |
| sparse retrieval | 精确关键词、ECU、DID、ServiceID、flow name 命中 | `CMD1A1C`、`0x3101`、`DTC Read` | 对编号、十六进制参数、模板名更稳定 |
| Milvus dense retrieval | 表述不同但语义相近的证据召回 | `Request Open HV Battery` 与相关章节 | 能补充 sparse 漏召回 |
| Graph RAG | 结构关系、表格行、refer-to、模板路径 | flow 节点跳到目标流程图，再到参数表 | 解决“证据之间的关系”，不是只做文本相似度 |

当前 workload 中 hybrid 设置是 dense topK `8`、hybrid topK `8`、dense weight `0.6`、sparse weight `0.4`。dense 和 sparse 的结果会按 id 去重、归一化打分并合并，Graph RAG 再基于候选证据构建节点级 evidence bundle。

### 3.3 为什么 LLM 只生成单节点

不让 LLM 一次性生成完整 fused workflow，原因有三个：

1. 完整 workflow 上下文太长，包含数百个 `ScriptNode`，局部错误难定位。
2. 单个参数错误可能导致整条流程不可用，必须能追溯到 evidence 和 guardrail correction。
3. 系统已有 deterministic XML plan、template contract、base workflow、audit 能力，应该让模型补充理解和生成，而不是绕过这些校验。

因此当前路线是：

- LLM 输入：当前 flow 节点、候选证据、模板契约、base workflow 参考、Graph RAG path。
- LLM 输出：节点级结构化计划和 XML 片段。
- 系统接管：解析 JSON、校验 XML、校验 Arg、修正高风险字段、渲染 operation XML、组装 serial XML、融合 workflow、审计回归。

## 4. 核心数据对象

| 对象 | 通俗解释 | 为什么需要 | 面试怎么讲 |
| --- | --- | --- | --- |
| `KnowledgeUnit` | 面向检索的知识块 | 支撑 JSONL sparse index 和向量入库 | “它是文档内容的检索单元，保留 doc、ECU、DID、page、bbox 等 metadata。” |
| `EvidenceUnit` | 面向追溯的证据单元 | 每个参数要能回到原文、页码、bbox、表格行 | “最终不是只给 XML，而是能说清楚参数来自哪条证据。” |
| `RetrievedChunk` | 检索返回的一条候选结果 | 统一 dense/sparse/hybrid 结果格式 | “dense 返回也必须带原始 id 和 metadata，不能变成不可追溯的向量结果。” |
| `StepEvidenceBundle` | 某个 flow 步骤的证据包 | 给 Graph RAG 和 LLM 提供节点级上下文 | “它把候选证据、graph path、refer-to 结果和检索 trace 绑在一起。” |
| `XmlGenerationContext` | LLM 生成一个节点时的完整输入 | 控制 prompt 内容和 trace | “模型看到的是受预算控制的证据包，不是无限上下文。” |
| `LlmNodeXmlGeneration` | 一个节点的 LLM 输出与校验结果 | 保存 raw 输出、修复、guardrail、最终 XML | “它同时记录模型原始答案和系统修正后的可接受结果。” |
| `WorkflowAuditReport` | plan、base workflow、fused workflow 的差异报告 | 判断参数是否正确应用，是否冲突 | “这一步决定最终能不能接受，不由模型自己说了算。” |

## 5. 关键模块实现

### 5.1 文档解析与知识规范化

输入是 MinerU 的 `content_list`、Markdown、表格和图片信息。系统把它们转为统一 schema：

- `KnowledgeUnit`：用于检索和向量入库。
- `EvidenceUnit`：用于 evidence trace。
- 表格行会尽量保留列名和行级 metadata。
- source metadata 保留 `source_path`、`doc_id`、`page_idx`、`bbox`。

这样做的意义是：下游不再直接依赖 PDF 或 Markdown 的格式，而是依赖统一的数据对象。

### 5.2 JSONL sparse index

本地索引目录保存 `knowledge_units.jsonl`、`evidence_units.jsonl`、`summary.json` 等文件。sparse retrieval 不依赖外部服务，适合作为 baseline 和 fallback 评测对象。

sparse 的优势是精确：对 `0x3101`、`ADCU1301`、`ServiceID 0x19` 这类标识符，关键词匹配往往比纯语义向量更可控。

### 5.3 Milvus vector index

向量库使用外部 Milvus 服务，默认连接 `http://127.0.0.1:19530`。当前真实构建结果：

- collection：`diagnostic_knowledge_treg_20260402`
- embedding model：`/home/xdu/LLM/models/Qwen3-Embedding-0.6B`
- embedding dimension：`1024`
- metric：`COSINE`
- vector count：`11140`
- knowledge unit count：`4019`
- evidence unit count：`7121`
- manifest：`data/index/treg_20260402/vector_manifest.json`

manifest 会记录 collection、模型、维度、metric、写入数量和源 JSONL hash。这样可以确认“当前向量库到底由哪批源文件构建”，避免 collection 与本地索引不一致。

### 5.4 hybrid retrieval merge

dense 和 sparse 的结果会统一成 `RetrievedChunk`，再做：

- id 去重。
- score normalize。
- 按 `dense_weight` 和 `sparse_weight` 加权。
- 保留 `source="dense"`、`source="sparse"` 或 hybrid trace。
- 保留原始 `source_id`、`doc_id`、`source_path`、`page_idx`、`bbox`。

注意：dense retrieval 只增强召回，不替代 JSONL 和 raw source 作为事实源。

### 5.5 Graph RAG evidence bundle

Graph RAG 不只是文本相似度检索，而是把结构关系显式建模：

- flow 节点与 operation 名称。
- ECU、DID、RID、ServiceID 等参数实体。
- template class 和历史 XML 示例。
- Markdown section、table row、flowchart title。
- `refer to` 跳转路径。

最终每个 flow node 会得到一个 evidence bundle，包含候选证据、graph paths、检索来源和原始 metadata。这个 evidence bundle 是 LLM prompt 的主要上下文来源。

### 5.6 LLM XML/code generation

当前 LLM 接入方式是 vLLM OpenAI-compatible API：

- base URL：`http://127.0.0.1:8008/v1`
- model name：`qwen-audit-resolver`
- `max_model_len`：`8192`
- 双卡 TP2 可稳定运行。

LLM 只生成 operation-level 节点。生成过程包括：

- 构造 `XmlGenerationContext`。
- 预算控制 prompt，优先保留高价值证据。
- 调用 vLLM。
- 解析 JSON 与 XML。
- 校验 `ScriptNode`、`ClassName`、`Args`。
- 必要时进入 repair prompt。
- 多次失败后标记 `needs_review=true`，不静默兜底。

当前验收中：

- LLM generations：`11`
- valid generations：`11`
- repair attempts：`0`
- average generation latency：`17.770537s`
- prompt estimated tokens total：`31653`
- output estimated tokens total：`7056`

### 5.7 deterministic guardrail

高风险字段不能只信 LLM。当前重点字段包括：

- `EcuBOMName`
- `DID`
- `RID`
- `ServiceID`
- `RequestParameter`

系统会把模型原始输出记录为 `raw_llm_args`，再基于 evidence、base workflow、semantic default 或 deterministic plan 形成 `post_guardrail_args`。每次修正都写入 `guardrail_corrections`，包含：

- 原值。
- 修正后值。
- 修正字段。
- 修正来源。
- 修正原因。

当前验收中：

- raw LLM critical arg accuracy：`0.2`
- post-guardrail critical arg accuracy：`1.0`
- guardrail correction count：`12`
- correction by arg：`DID 5`、`EcuBOMName 5`、`RequestParameter 1`、`ServiceID 1`
- correction by source：`deterministic_plan 5`、`evidence_context 2`、`semantic_default 5`

这说明模型能生成结构，但关键参数必须经过系统约束后才能进入最终结果。

### 5.8 workflow fusion 与 audit

系统不会直接把 LLM XML 当最终 workflow。它会先渲染 operation XML 和 `serial_node.xml`，再融合到 base workflow，得到 `fused_workflow.xml`。

audit 会比较三者：

- plan 中想要的参数。
- base workflow 原有参数。
- fused workflow 最终参数。

当前 LLM scenario 的 fused 结果：

- fused root tag：`PhaseNode`
- fused script nodes：`214`
- fused serial nodes：`30`
- fused parallel nodes：`7`
- fused args：`1457`

audit 结果：

- planned nodes：`11`
- audited nodes：`11`
- review items：`2`
- status counts：`confirmed_by_plan 71`、`extra_plan_arg_ignored 8`、`filled_from_plan 7`、`plan_conflict_kept_base 2`
- resolution decisions：`88`
- auto resolved：`86`
- review required：`2`

这里的关键优化是把“LLM 多生成但 base workflow 不需要的可忽略 Arg”归为 `extra_plan_arg_ignored`，而不是和真实冲突混在一起。

### 5.9 regression 与 acceptance runner

acceptance runner 固定跑三组对照：

| scenario | 作用 |
| --- | --- |
| `sparse_deterministic` | 原始 baseline，验证旧链路不被破坏 |
| `hybrid_deterministic` | 验证 dense/hybrid 召回接入后，不破坏确定性生成 |
| `hybrid_llm_xml` | 验证 LLM 节点生成、guardrail、audit、regression 能闭环 |

当前 acceptance summary：

- workload：`treg_20260402_hybrid_llm`
- valid：`true`
- scenarios：`3`
- passed：`3`
- failed：`0`
- blocked：`0`
- duration：`281.476869s`

服务检查：

- Milvus：ok，TCP connection succeeded。
- vLLM：ok，`/v1/models` succeeded，模型暴露 `max_model_len=8192`。
- embedding model：ok，路径存在。
- python dependencies：ok，`pymilvus` 和 `sentence_transformers` 可导入。

报告路径：

- `data/reports/acceptance/treg_20260402_hybrid_llm_acceptance.json`
- `data/reports/acceptance/treg_20260402_hybrid_llm_acceptance.md`
- `data/reports/acceptance/treg_20260402_hybrid_llm/hybrid_llm_xml/regression_report.json`

## 6. 可校验、可回溯、可迭代设计

### 6.1 为什么不直接让模型生成最终 XML

直接让模型生成完整 workflow 会有几个工程风险：

- 长上下文风险：完整 workflow 很长，容易超上下文或遗漏局部节点。
- 局部错误难定位：一个 Arg 错了，很难知道是检索错、prompt 错还是模型幻觉。
- 无法稳定回归：模型输出不稳定，直接覆盖最终 workflow 会让 diff 和 audit 失控。
- 缺少证据边界：模型可能编造参数，尤其是 DID、RID、ServiceID 这类看起来像真的字段。

因此系统采用“LLM 生成单节点计划，系统负责最终接受”的架构。

### 6.2 raw LLM args 与 post-guardrail args

`raw_llm_args` 是模型原始建议，反映模型自己能从 prompt 中抽取到什么。  
`post_guardrail_args` 是系统校验、修正后的最终参数，才允许进入 XML 渲染和 workflow fusion。

这两个字段必须同时记录，原因是：

- 能量化模型本身能力，而不是只看最终结果。
- 能证明系统修正了哪些字段。
- 能定位错误来源。
- 能把 semantic default 与 evidence hit 区分开，避免伪造证据。

当前结果里 raw critical accuracy `0.2`，post critical accuracy `1.0`。这不是矛盾，而是说明 guardrail 是必要组件。

### 6.3 trace 里必须保留什么

一条可接受的生成结果至少要能回答：

- 当前节点用了哪些 prompt context。
- 候选证据来自 dense、sparse、hybrid 还是 graph path。
- 参数来自 evidence、base workflow、deterministic plan 还是 semantic default。
- LLM 原始输出是什么。
- XML 是否 well-formed。
- 是否 repair，repair 原因是什么。
- 是否有 audit conflict。
- 最终是自动接受、忽略、保留 base，还是进入人工 review。

### 6.4 baseline 与 regression 的意义

项目保留 deterministic baseline，不是因为不相信 LLM，而是为了有对照：

- sparse deterministic：旧链路是否稳定。
- hybrid deterministic：新增向量召回是否破坏旧生成。
- hybrid LLM XML：模型接入后是否提升，并且是否仍能通过 guardrail。

这让每次优化都可以回答：“提升来自哪里？有没有引入回归？”

## 7. 最终效果与指标分析

### 7.1 验收总览

| 指标 | 当前结果 | 说明 |
| --- | ---: | --- |
| acceptance valid | `true` | 三组场景全部通过 |
| scenarios passed | `3/3` | sparse deterministic、hybrid deterministic、hybrid LLM XML |
| vector count | `11140` | Milvus collection 写入总量 |
| dense recall@8 | `0.9` | 30 条 retrieval eval |
| hybrid recall@8 | `0.9` | 当前 hybrid 与 dense 指标一致 |
| MRR | `0.741667` | dense/hybrid 相同 |
| operation retrieval coverage | `1.0` | 11 个 operation 都有检索结果 |
| operation graph path coverage | `1.0` | 11 个 operation 都有 graph path |
| LLM valid generations | `11/11` | 节点级 XML 生成全部有效 |
| repair attempts | `0` | 当前无需 repair |
| raw critical accuracy | `0.2` | 模型原始关键参数准确率 |
| post-guardrail critical accuracy | `1.0` | 系统修正后关键参数准确率 |
| node golden checks | `39/39` | 11 个节点关键检查全过 |
| review items | `2` | audit 后仍需关注的真实冲突 |
| unit tests | `81 OK, 1 skipped` | 全量单测结果 |

### 7.2 如何解释 raw accuracy 低但最终 accuracy 高

面试时不要回避 raw LLM critical accuracy `0.2`。正确解释是：

- 这个指标故意只看高风险字段的模型原始输出，不看系统修正。
- 高风险字段通常是 ECU、DID、ServiceID 等强约束参数，不能依赖模型猜。
- 低 raw accuracy 说明模型不应该直接控制最终结果。
- post-guardrail `1.0` 说明系统约束有效，最终输出可接受。
- 项目的价值不是“模型一次生成完全正确”，而是“模型生成 + 系统校验 + 可追溯修正”的闭环。

### 7.3 review items 为什么能降到 2

初期 review items 较多，是因为 audit 把两类问题混在了一起：

- base workflow 不需要的额外 plan arg。
- base workflow 与 evidence/plan 真实冲突。

优化后新增 `extra_plan_arg_ignored` 分类，把可安全忽略的额外 Arg 从 review 中剥离，只保留真实冲突。当前 `88` 个 resolution decisions 中，`86` 个 auto resolved，只有 `2` 个 review required。

### 7.4 当前瓶颈

当前 acceptance 总耗时仍约 `281s`，说明真实闭环已经跑通，但还没达到低成本目标。主要优化方向：

- LLM 输出从 “JSON + 完整 XML” 收敛为 “结构化 args plan”，XML 由系统渲染。
- 普通节点使用短 prompt 和更小 max tokens。
- 高风险节点才使用长上下文。
- repair prompt 只传错误、相关 Arg 和相关 evidence。
- embedding query 和 vLLM GPU 占用需要更细的资源隔离。

## 8. 实现中遇到的问题与解决方案

### 问题 1：默认 `python` 缺少 vector 依赖

| 项 | 内容 |
| --- | --- |
| 问题 | 默认 `python` 环境没有 `pymilvus`、`sentence_transformers`，会导致 vector build/query 或 Milvus integration 失败。 |
| 原因 | 项目里真实向量能力依赖 `.venv`，但系统默认 `python` 指向另一个环境。 |
| 解决 | 固定使用 `.venv/bin/python` 运行 vector、acceptance 和 integration 命令。acceptance service check 中显式检查依赖可导入。 |
| 面试答法 | “我没有让脚本静默 fallback 到 sparse，而是把运行时依赖检查前置。这样缺依赖时能明确失败，避免误以为 dense/hybrid 已经生效。” |

### 问题 2：Milvus 和 vLLM 服务未监听导致验收 blocked

| 项 | 内容 |
| --- | --- |
| 问题 | acceptance runner 初期会发现 `19530` 和 `8008` 未监听，`hybrid_deterministic`、`hybrid_llm_xml` 被 blocked。 |
| 原因 | 真实验收依赖外部 Milvus 和 vLLM 服务，但代码不绑定 Docker 或启动方式。 |
| 解决 | acceptance runner 增加 service checks：Milvus TCP、vLLM `/v1/models`、embedding path、python dependencies。服务不可用时明确 blocked/fail，不静默降级。 |
| 面试答法 | “我把真实服务状态变成验收报告的一部分，避免模型服务没起来时误判算法效果。” |

### 问题 3：GPU0 被占用时只能单卡启动

| 项 | 内容 |
| --- | --- |
| 问题 | 初期 GPU0 显存被占用，vLLM 只能先用单卡低风险启动。 |
| 原因 | TP2 需要两张卡可用，否则容易启动失败或 OOM。 |
| 解决 | GPU0 释放后切换 `CUDA_VISIBLE_DEVICES=0,1`、`tensor_parallel_size=2`，并验证 `/v1/models` 暴露 `max_model_len=8192`。 |
| 面试答法 | “我没有一开始强上双卡，而是先保证服务可用，再在显存释放后切 TP2 做真实验收。” |

### 问题 4：Qwen3-Embedding 构建向量时 OOM

| 项 | 内容 |
| --- | --- |
| 问题 | Qwen3-Embedding 默认长序列，加上 vLLM 占 GPU，构建或查询 embedding 时可能 OOM。 |
| 原因 | embedding 模型和 vLLM 同时争 GPU，且默认 sequence length 偏大。 |
| 解决 | 在 embedding client 中增加 `EMBEDDING_MAX_SEQ_LENGTH=512` 和 `EMBEDDING_DEVICE` 控制；构建时降低 batch，验收查询时可使用 CPU embedding，避免抢占 vLLM 双卡。 |
| 面试答法 | “我把 embedding 的资源使用参数化，既保证真实模型可跑，又不影响 vLLM 服务稳定性。” |

### 问题 5：LLM 原始关键参数准确率低

| 项 | 内容 |
| --- | --- |
| 问题 | LLM 输出的 XML 结构合法，但 raw critical arg accuracy 只有 `0.2`。 |
| 原因 | ECU、DID、ServiceID 这类字段高度依赖模板、证据和业务默认值，不能只靠模型从 prompt 中猜。 |
| 解决 | 引入 deterministic guardrail，记录 `raw_llm_args`、`post_guardrail_args` 和 `guardrail_corrections`；最终只接受 post-guardrail 结果。 |
| 面试答法 | “这不是失败，而是我刻意把模型原始能力和系统修正能力分开量化。最终链路要求可审计，不要求模型裸输出全对。” |

### 问题 6：review items 把可忽略项和真实冲突混在一起

| 项 | 内容 |
| --- | --- |
| 问题 | 初期 audit review items 偏高，影响判断真实风险。 |
| 原因 | base workflow 中不存在但 LLM plan 多生成的非必填 Arg，被和真实冲突一起统计。 |
| 解决 | 新增 `extra_plan_arg_ignored` 状态，resolver 自动映射为 `ignore_plan_arg`；真实冲突仍保留 review。 |
| 面试答法 | “我把审计项分类细化，不是单纯压低 review 数，而是区分合理忽略和真实冲突。” |

### 问题 7：acceptance 总耗时仍偏高

| 项 | 内容 |
| --- | --- |
| 问题 | 当前 acceptance 耗时约 `281s`，已经可用但不够快。 |
| 原因 | LLM 输出仍包含较完整 JSON/XML，prompt budget 和 max tokens 较大，且真实服务端到端包含检索、生成、融合、审计、回归。 |
| 解决 | 已记录 per-node prompt/output token 和 latency；后续计划收敛为 args-only plan，普通节点短 prompt，高风险节点长 prompt。 |
| 面试答法 | “我先把耗时拆到 trace 里，确认瓶颈在 LLM 生成和输出长度，再做 token 和 prompt profile 优化。” |

### 问题 8：dense/hybrid 接入后必须保证 baseline 不被破坏

| 项 | 内容 |
| --- | --- |
| 问题 | 新增 dense/hybrid 后，如果直接替换 sparse，可能导致旧 deterministic 结果回归。 |
| 原因 | dense 对语义近似敏感，但对编号和十六进制参数未必比 sparse 更稳定。 |
| 解决 | dense 作为 opt-in 增量召回；acceptance 同时跑 sparse deterministic、hybrid deterministic、hybrid LLM XML 三组对照。 |
| 面试答法 | “我没有用新检索算法替代旧链路，而是让它先作为候选增强，再用三组场景证明没有破坏 baseline。” |

## 9. 面试高频问答

### Q1：这个项目到底是做什么的？

它是一个车企诊断代码/XML 工件生成平台。输入是 PDF/Markdown、`flow.xlsx`、历史 XML workflow 和模板契约；输出是可融合进主流程的 XML 节点、serial XML、fused workflow、trace、audit 和 regression report。核心不是“生成文本”，而是“证据驱动、可校验、可回溯地生成工程工件”。

### Q2：为什么用 Milvus？

Milvus 用来承载本地 embedding 的 dense retrieval。相比只用 JSONL sparse，它能召回表达方式不同但语义相关的章节和表格行。当前 Milvus collection 有 `11140` 条向量，维度 `1024`，metric 是 `COSINE`。但 Milvus 不是事实源，事实源仍是 JSONL 和 raw source。

### Q3：为什么 hybrid 而不是只用 dense？

诊断场景里有大量精确标识符，比如 `0x3101`、`ADCU1301`、`ServiceID 0x19`。这些内容 sparse 很强，dense 不一定稳定。hybrid 能同时利用 sparse 的精确匹配和 dense 的语义召回。当前 30 条 retrieval eval 中 dense/hybrid recall@8 都是 `0.9`，后续可以继续调权重或加入 reranker。

### Q4：Graph RAG 还有必要吗？

有必要。dense/sparse 解决“文本相似”，Graph RAG 解决“结构关系”。比如 flow 节点 refer to 某个流程图，流程图再指向参数章节，参数章节里某一行才是最终 DID/RID。只靠向量相似度不能稳定表达这种路径关系。

### Q5：为什么不让 LLM 直接生成完整 workflow？

完整 workflow 太长、结构复杂、局部错误难定位，而且模型可能编造关键参数。当前方案让 LLM 只生成单节点结构化计划，系统再做 XML 渲染、workflow fusion、audit 和 regression。这样模型有用，但最终接受权在系统。

### Q6：raw LLM critical accuracy 只有 0.2，是不是模型没用？

不是。这个指标只衡量模型原始输出的高风险字段是否完全正确。项目本来就不打算裸信模型。模型负责生成结构和候选参数，系统用 evidence、base workflow、deterministic plan 和 semantic default 做 guardrail。最终 post-guardrail critical accuracy 是 `1.0`，说明闭环有效。

### Q7：guardrail 会不会掩盖模型错误？

不会，因为 trace 同时保留 `raw_llm_args` 和 `post_guardrail_args`，并记录每次 correction 的来源、原因、原值和新值。模型错在哪里、系统怎么修正，都能在报告里看到。

### Q8：如何保证参数可追溯？

每条 evidence 都有 `evidence_id`、`doc_id`、`source_path`、`page_idx`、`bbox` 和 metadata。dense 检索返回也必须带原始 id。LLM prompt 使用的 context id、最终 arg evidence map、guardrail correction source 都会写 trace。

### Q9：TP2 双卡相比单卡解决了什么？

TP2 主要解决大模型服务的显存和上下文能力问题。当前 vLLM 服务使用 AWQ 模型，双卡 TP2 能稳定暴露 `max_model_len=8192`，支撑长 evidence context 的节点级生成。单卡适合先 smoke test，但真实验收更适合双卡。

### Q10：如果换车型或换流程，怎么泛化？

泛化依赖三个部分：重新构建 MinerU/JSONL/Milvus index，更新 template registry 和 contracts，新增 workload 与 golden checks。当前已经把 `treg_20260402` 做成 baseline；下一步要增加不同日期或不同车型 workload，验证 dense/hybrid/LLM guardrail 是否泛化。

### Q11：如何评估系统是不是比 deterministic 好？

不能只看最终 XML 是否生成，要看对照实验：

- sparse deterministic：旧 baseline。
- hybrid deterministic：检索增强是否破坏旧生成。
- hybrid LLM XML：模型接入后是否合法、是否减少 review、关键参数是否正确。

当前三组全部 passed，LLM 场景 review items 是 `2`，node golden checks `39/39`。

### Q12：后续怎么优化延迟和成本？

第一步把 LLM 输出从 “JSON + 完整 XML” 改成 “args plan”，XML 由系统渲染。第二步做 prompt profile：普通节点短上下文，高风险节点长上下文，repair 只传局部错误。第三步继续利用 vLLM prefix cache、chunked prefill 和更细的 token/latency trace。

## 10. 简历表达

### 3 条精炼项目描述

1. 设计并实现车企诊断代码/XML 生成平台，将 MinerU 解析结果、流程表、历史 XML 模板和参数表统一为可检索、可追溯的 `KnowledgeUnit/EvidenceUnit`，支撑节点级证据召回与工程工件生成。
2. 构建 sparse + Milvus dense + Graph RAG 的 hybrid retrieval 链路，基于 Qwen3-Embedding-0.6B 建立 `11140` 条向量 collection，并通过 30 条 golden retrieval case 验证 dense/hybrid recall@8 达到 `0.9`。
3. 接入本地 vLLM TP2 OpenAI-compatible 服务，让模型生成单节点 XML 计划，并通过 deterministic guardrail、template contract、workflow audit 和 regression 闭环将 post-guardrail critical arg accuracy 提升到 `1.0`，最终三组 acceptance scenario 全部通过。

### 技术栈关键词

- 文档解析：MinerU、Markdown/table normalization。
- 检索：JSONL sparse index、Milvus、Qwen3-Embedding、hybrid retrieval、Graph RAG。
- 生成：vLLM、OpenAI-compatible API、Qwen Coder AWQ、prompt budget、repair prompt。
- 工程约束：template contract、XML validator、deterministic guardrail、workflow fusion、audit resolver。
- 评测：retrieval golden set、node golden checks、regression、acceptance runner、latency/token trace。
- 服务：TP2 双卡推理、service check、manifest、benchmark。

### 可量化结果版本

可以在简历中写：

> 负责车企诊断代码/XML 生成平台核心链路，完成 MinerU 知识规范化、Milvus 向量索引、hybrid retrieval、Graph RAG evidence bundle、vLLM 单节点 XML 生成、guardrail、workflow fusion 与 acceptance regression；在 `treg_20260402` 真实 workload 上构建 `11140` 条向量，30 条 retrieval golden case recall@8 达到 `0.9`，LLM 节点生成 `11/11` 有效，post-guardrail critical arg accuracy 达到 `1.0`，node golden checks `39/39` 通过，三组 acceptance scenario 全部 passed。

## 11. 面试时的讲述顺序

推荐按以下顺序讲，不要一开始陷入代码文件：

1. 业务问题：车企诊断资料分散，人工生成 XML 容易错且不可追溯。
2. 总体路线：文档解析、知识规范化、三路召回、节点级生成、系统校验。
3. 关键设计：LLM 不直接生成完整 workflow，高风险参数由 guardrail 接管。
4. 工程闭环：trace、audit、regression、acceptance runner。
5. 真实结果：三组 scenario passed，critical accuracy、recall、review items、golden checks。
6. 问题复盘：环境依赖、服务启动、GPU/TP2、embedding OOM、LLM raw accuracy、audit 分类。
7. 下一步：扩大 workload、降低 token 和 latency、进一步增强泛化评测。

## 12. 可以主动强调的工程判断

- 不删除 deterministic baseline，因为 baseline 是判断新能力是否真的变好的参照。
- 不让 Milvus 替代 JSONL/raw source，因为向量库不是事实源。
- 不让 LLM 直接决定最终 workflow，因为工程交付需要可校验和可审计。
- 不把 semantic default 伪装成 evidence hit，因为这会破坏溯源可信度。
- 不在服务不可用时静默 fallback，因为真实验收必须暴露环境问题。
- 不只看最终 passed，还要看 raw vs post、guardrail correction、review 分类和 failed samples。

