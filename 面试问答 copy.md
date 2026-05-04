# 一、项目一：车企诊断知识检索、代码生成与推理基础设施平台

> 口径说明：以下答案只基于当前仓库里的代码、配置和文档，不补不存在的实现。能确认的写成“已实现”，不能确认的明确写成“当前版本未实现/只做了设计预留”。

## 1. 项目背景与整体架构

1. 这个项目解决的核心业务问题是什么？  
答：把车企诊断资料解析成可检索、可追溯、可生成 XML 工作流的工程闭环，减少人工从 PDF、流程表和历史 XML 中手工找证据、填参数、拼 workflow 的成本。

2. 为什么车企诊断场景需要知识检索、代码生成和推理基础设施？  
答：因为知识分散在 PDF、流程表、历史 XML 模板里，而且很多字段是 DID、ServiceID、Session、SecurityLevel 这类强结构化参数，既要查得准，也要生成得稳；另外剩余冲突才适合交给 vLLM 做保守决策，所以需要独立的 serving 和 benchmark 基础设施。

3. 诊断知识主要来自哪些数据源？  
答：当前仓库里主要有 4 类：MinerU 解析后的 PDF/扫描件输出、`flow.xlsx`、历史 workflow/XML 模板、由模板反推出来的参数契约与审计报告。

4. PDF、流程图、历史 XML 模板分别在业务中承担什么作用？  
答：PDF 提供原始诊断知识证据；流程图在当前实现里主要来自 `flow.xlsx`，决定步骤顺序和并行关系；历史 XML 模板提供 `ScriptNode/ClassName/ArgName` 结构、融合基线和参数契约。

5. 最终用户是谁？研发、售后、测试还是诊断工程师？  
答：从当前仓库形态看，主要面向诊断开发、测试和流程工程师，以及负责 vLLM serving/benchmark 的 AI infra 工程师，不是面向 C 端问答用户的产品。

6. 平台的输入和输出分别是什么？  
答：输入包括 MinerU 输出目录、`flow.xlsx`、历史 workflow XML、模板目录和 workload manifest；输出包括 `xml_plan.json`、`serial_node.xml`、每个 operation 的独立 XML、融合后的 workflow、审计/修复报告、回归报告和 benchmark 报告。

7. 这个系统是一个 RAG 系统、代码生成系统，还是推理服务平台？  
答：当前实现更像“诊断 XML 生成与审核平台 + vLLM 推理基础设施”。它有 RAG 风格的证据检索，也有 XML/代码生成能力，还有独立的 router、benchmark、metrics 工具链。

8. 整体架构可以怎么画？  
答：可以画成两条主线：  
离线生成线：`MinerU -> normalize/evidence -> local index/graph -> flow.xlsx parse -> evidence bundle -> xml plan -> validate -> render/fuse -> audit -> contract resolve -> optional llm resolve`。  
推理基础设施线：`vLLM serve -> profile-aware/context-tier router -> prompt budget -> benchmark/replay/metrics`。

9. 线上请求从进入服务到最终返回结果，完整链路是什么？  
答：如果说当前唯一真正在线的 LLM 路径，就是 `/api/v1/xml/workflow/resolve-audit-llm`：请求进入 FastAPI -> 载入 resolution report 和 evidence context -> 可选 router 选 endpoint -> prompt budget 裁剪上下文 -> `VllmClient` 调 OpenAI-compatible `/chat/completions` -> 解析 JSON -> 回写保守决策。

10. 你在项目中负责哪几块？哪些是你独立完成的？  
答：仓库本身看不出个人分工。按当前实现口径，能说自己负责了 MinerU 接入、索引与检索、轻量图谱、XML 计划生成/融合/审计、vLLM router、benchmark 和 metrics 这一整条工程闭环。

11. 项目中最难的技术点是什么？  
答：最难的是把流程节点和真实证据稳定对齐，再把证据转成 XML 参数，同时还要保留证据链并和历史 workflow 做差异审计，而不是只让模型自由生成。

12. 项目中最有价值的优化是什么？  
答：当前最有价值的是两块：一块是“图谱感知的参数选择 + 审计闭环”，保证生成可靠；另一块是“profile-aware router + workload benchmark”，让 vLLM 的部署选择有数据依据。

13. 和普通问答系统相比，车企诊断知识检索有什么特殊性？  
答：这里对精确符号特别敏感，像 DID、ServiceID、Session、SecurityLevel、ECU 名称都要精确命中；另外还要保留页码、bbox、节点名、历史模板关系，不能只做语义相似。

14. 这个平台如何保证生成结果的可靠性？  
答：当前实现主要靠规则链路：证据检索、图谱路径、确定性参数打分、XML plan 校验、workflow 审计、模板契约解析、剩余冲突才交给 LLM，而且 LLM 只允许输出有限动作。

15. 为什么要做证据追踪链路？  
答：因为每个参数都需要知道“从哪条证据来的”。当前实现里 `XmlArg` 保留 `evidence_ids/selection_score/graph_paths`，`flow_evidence_trace.json` 还能追到原始证据摘录、页码和命中词。

16. 为什么不能只让大模型直接回答？  
答：因为当前场景更像工程生成，不是开放问答。直接让模型输出 XML 或参数，风险是幻觉字段、覆盖已有稳定值、无法审核；所以当前仓库把 LLM 限制在 unresolved audit conflict 这类小范围任务里。

17. 系统中哪些部分是规则驱动，哪些部分是模型驱动？  
答：规则驱动的是解析、归一化、检索、图谱构建、XML plan 生成、校验、workflow 融合和合同解析；模型驱动的是 `resolve-audit-llm` 这一步，未来文档里也预留了更广义的 DSL repair。

18. 如果让你重新设计这个系统，你会怎么改？  
答：我会补上真正的 dense retrieval 和独立 reranker，把 prompt budget 从 `chars/4` 升级到真实 tokenizer 计数，再把 router 从 round-robin 升级到 queue-length/least-load，并补充增量索引和版本管理。

---

## 2. 异构知识解析与生成链路工程化

1. PDF 解析用了什么方案？  
答：当前已实现的是 MinerU CLI，同步调用 `mineru -p ... -o ...`，然后读取 `*_content_list.json`；如果没有结构化输出，可以退化到 markdown fallback。

2. PDF 中表格、流程图、标题层级如何处理？  
答：表格保留 `caption/body/footnote`；PDF 里的图片会根据 caption/footnote 关键词被标成 `flowchart` 或普通 `image`；标题层级没有做完整文档树，只保留了 `text_level` 元数据。

3. PDF 解析后的结构化 schema 是什么？  
答：先是 `MinerUContentBlock`，再转成两套统一结构：检索用 `KnowledgeUnit`，追溯用 `EvidenceUnit`，两者都保留 `page_idx/bbox/source_path` 和抽取出的 DID/ServiceID/Session/SecurityLevel。

4. 流程图是图片、Visio、PDF 还是其他格式？  
答：当前真正用于流程建模的是 `flow.xlsx`；PDF 里的流程图只作为 `flowchart evidence` 使用，没有 Visio 解析链路。

5. 流程图如何转换成可检索知识？  
答：有两条路径：`flow.xlsx` 会被解析成 `FlowStepPlan`；PDF 中的流程图图片则通过 MinerU caption/footnote 转成 `EvidenceUnit`，参与检索和图谱。

6. 历史 XML 模板的结构是什么？  
答：当前主要关心 `ScriptNode`、`SerialNode` 和 `Arg`。模板扫描时提取的是 `node_name/class_name/root_tag/arg_names/attrs/source_path`。

7. XML 模板中哪些字段对代码生成最重要？  
答：最重要的是 `ClassName`、`ArgName` 集合、已有 `ScriptNode` 属性，以及节点名称和来源文件。后面的 workflow 融合和 contract 推断都依赖这些字段。

8. 不同来源的数据如何统一成一种知识表示？  
答：PDF/图片/表格统一成 `KnowledgeUnit` 和 `EvidenceUnit`；流程图单元统一成 `FlowNode/FlowStepPlan`；历史 XML 统一成 `XmlTemplateEntry` 和 `XmlTemplateClassContract`。

9. 入库前做了哪些清洗？  
答：做了 HTML unescape、去标签、压缩空白、bbox 转 int、相对图片路径转绝对路径，以及文本里的 DID/ServiceID/Session/SecurityLevel 抽取。

10. 如何处理重复知识？  
答：当前没有做离线语义去重；但在 prompt budget、图谱构建和 evidence bundle 阶段会做按 `evidence_id`、relation key 或 section 内容的去重。

11. 如何处理冲突知识？  
答：当前不会在入库阶段强行合并，而是保留为多条证据，后续在参数选择、workflow audit 和 resolve 阶段做保守决策。

12. 如何处理过期版本的诊断文档？  
答：当前只保留了 `version/source_path/doc_id` 这类 metadata，没有做自动版本仲裁和过期清理策略。

13. 如何区分车型、年款、ECU、DTC、诊断服务？  
答：当前已显式建模的是 `protocol/module/ecu/service_ids/dids/sessions/security_levels`；车型和年款还没有在 schema 里做完整标准化。

14. 文档分块策略是什么？  
答：当前是一块 MinerU block 对应一条 `KnowledgeUnit/EvidenceUnit`，没有再做 token 级二次切分。

15. chunk size 怎么选？  
答：当前不按固定 token size，直接沿用 MinerU block 粒度，所以 size 由原始文档结构决定。

16. chunk overlap 怎么选？  
答：当前没有 overlap。

17. 有没有保留标题层级、页码、章节号等 metadata？  
答：保留了 `page_idx/bbox/source_path/text_level`；没有把章节号和完整标题树单独建模。

18. 为什么要建立文本搜索索引？  
答：因为当前场景里大量是精确编码和术语，像 `D134/2F/27/03` 这种更适合做 exact-match boost 和稀疏检索。

19. 用的是 BM25、Elasticsearch、向量库，还是混合检索？  
答：当前已实现的是本地 JSONL + 依赖极少的 BM25 风格 sparse 检索；图谱是辅助信息；dense/vector 还在文档规划里。

20. 为什么这里提到“文本搜索索引”而不是向量索引？  
答：因为当前项目真正落地的是 `local_sparse.py`，没有接任何向量库。这个阶段更强调“先闭环、先可复现”。

21. 诊断知识是否适合向量检索？  
答：部分适合，尤其是解释性文本；但对 ECU/DID/ServiceID 这种强编码字段，当前项目判断 sparse 更可靠，所以 vector 还没正式接入。

22. 关键词检索和向量检索分别有什么优缺点？  
答：关键词检索对编码、缩写、精确字段更稳，但语义泛化弱；向量检索反过来语义泛化强，但容易把相近概念混掉。当前仓库优先选择了前者。

23. 如何解决专业术语、缩写、DTC 编码的检索问题？  
答：当前 token 规则专门支持 `0x...`、字母数字串和十六进制片段，并对 DID、ServiceID、Session、SecurityLevel 做 exact-match boost。

24. 如何解决同义词问题？  
答：当前没有独立同义词词典，只做了节点名、模板名、动作短语的多视角匹配，所以同义词处理能力还比较弱。

25. 如何解决跨文档证据聚合问题？  
答：每个 flow node 会在全部 `evidence_units` 上打分取 top-k，最后通过 `evidence_ids` 和 `graph_paths` 把跨文档证据聚到同一个节点下。

26. 入库流程是离线批处理还是在线更新？  
答：当前是离线批处理，入口是 `scripts/build_mineru_index.py` 和 `/api/v1/index/build`。

27. 知识库更新后如何保证索引一致性？  
答：当前做法是一次 build 同时产出 `documents/knowledge_units/evidence_units/graph/summary`，属于整批重建，不是增量事务更新。

28. 入库失败如何重试？  
答：当前没有任务队列和自动 retry，失败后直接重跑脚本或 API。

29. 如何做数据版本管理？  
答：当前主要靠 workload 名、输出目录和文件路径约定，没有完整的版本库和回滚机制。

30. 如何验证解析结果是正确的？  
答：靠单元测试、固定 workload regression，以及 `flow_evidence_trace.json` 这样的可审计中间产物人工 spot check。

---

## 3. 轻量诊断图谱与证据追踪链路

1. 轻量诊断图谱是什么？  
答：就是一个本地可序列化的轻量图结构，不依赖图数据库，节点和边都由规则从 flow、evidence 和模板信息确定性生成。

2. 图谱中有哪些节点？  
答：当前有 `FlowNode`、`XmlTemplate`、`Evidence`、`ECU`、`DID`、`Service`、`Session`、`SecurityLevel`。

3. 图谱中有哪些边？  
答：当前有 `next_step`、`uses_template`、`targets_ecu`、`supported_by`、`uses_did`、`uses_service`、`requires_session`、`requires_security`、以及各类 `mentions_*` 边。

4. DTC、故障现象、ECU、诊断步骤、XML 模板之间如何建模？  
答：当前明确建模了 ECU、诊断步骤、XML 模板以及 DID/Service/Session/SecurityLevel；DTC 和故障现象还没有独立实体类型，所以这一版更接近“流程参数图”而不是全量诊断知识图谱。

5. 为什么不用完整知识图谱，而是“轻量诊断图谱”？  
答：因为当前目标是先把 XML 生成闭环做稳，所以采用了规则清晰、可落盘、可回归的轻量方案，避免一开始就上复杂图数据库和模型抽取。

6. 图谱构建是规则生成还是模型抽取？  
答：纯规则生成。

7. 图谱如何参与检索？  
答：它不单独返回检索结果，而是给每个 node bundle 补可读的 graph path，辅助后续参数选择和审计解释。

8. 图谱如何参与生成？  
答：`xml_plan.py` 在选 ECU/DID/ServiceID/Session/SecurityLevel 时，会把 graph path 匹配额外加分，形成 `graph_score`。

9. 图谱如何帮助减少幻觉？  
答：因为参数不是让模型编，而是必须在 evidence metadata、content 或 graph path 里出现；没有证据时会变成 `missing_fields` 或 `needs_review`。

10. 证据追踪链路如何设计？  
答：核心产物是 `flow_evidence_trace.json`。它按 step 和 operation 展开，保存 flow cell、命中的 evidence、graph path、选中的 args、缺失字段和 operation XML 路径。

11. 每个生成结果如何关联到原始文档证据？  
答：`XmlArg` 里有 `evidence_ids`，trace 里还能继续追到 `doc_id/page_idx/bbox/source_path/content_excerpt`。

12. 如果模型生成的内容找不到证据，如何处理？  
答：当前不会让模型直接生成 XML 参数；如果 plan 里某个 Arg 没证据，可以在 `XmlPlanValidationRequest` 里要求 `require_arg_evidence=True`，否则保留 warning 并进入 review。

13. 证据链如何展示给用户？  
答：当前没有前端展示，主要通过 JSON trace/report 展示。

14. 证据链如何用于审核？  
答：workflow audit 会把 `plan/base/fused` 三者差异列出来，resolver 再结合 contract 和 evidence context 做自动或 LLM 辅助判断。

15. 多条证据冲突时如何排序？  
答：当前按 retrieval score 累加，再叠加 graph score、ECU 先验和冲突惩罚，最后选分数最高的值。

16. 证据置信度怎么定义？  
答：当前没有做概率校准。工程上用的是 `EvidenceMatch.score`、`XmlArg.selection_score` 和 resolver 的 `confidence` 字段。

17. 图谱和文本索引如何结合？  
答：先用文本/evidence ranking 找候选，再用 graph path 丰富上下文，最后生成阶段同时参考两者。

18. 图谱有没有做路径搜索？  
答：有，`search_graph_paths` 用 BFS，支持 relation filter、max depth 和 max paths。

19. 是否做过 DTC 到诊断步骤的路径推理？  
答：当前没有，因为 DTC 还没做成独立实体；已经做的是 DID/Service/Session/SecurityLevel/ECU 到步骤的路径推理。

20. 如何衡量图谱构建质量？  
答：当前主要看 regression 里的 `graph_entities/graph_relations/graph_paths/operation_graph_path_coverage` 等覆盖指标。

---

## 4. Prompt Budget、上下文管理与证据裁剪

1. Prompt Budget 是什么？  
答：是把 evidence、graph path、operation context 裁到可控 prompt 长度的一层策略，当前实现集中在 `retrieval/prompt_budget.py`。

2. 为什么需要 Prompt Budget？  
答：因为 LLM 只用于 unresolved audit/repair 这类任务，输入太长会把 TTFT 拉高，也容易把无关证据塞进去。

3. 你们如何计算 token budget？  
答：当前用的是粗估公式 `len(text) / 4`，也就是 `chars_per_token=4`。

4. system prompt、用户问题、检索证据、生成格式分别分配多少预算？  
答：当前没有细到每一段单独配额，只配置 `max_prompt_tokens` 和 `reserved_output_tokens`，可用给证据的预算是两者之差。

5. 4k、32k、64k、128k 上下文分别适合什么请求？  
答：当前建议是：`<=4k` 给 `short_audit/rerank`，`4k-32k` 是标准长上下文，`32k-64k` 给重任务，`64k-128k` 更适合低频离线或极长审计。

6. 长上下文是否一定比短上下文效果好？  
答：不是。当前文档明确把长上下文当作“容量能力”，不是默认最好；上下文越长，TTFT、噪声和 OOM 风险都更高。

7. 上下文太长会带来哪些问题？  
答：会带来 TTFT 上升、显存压力变大、短请求被阻塞、无关证据干扰和 routing 更复杂。

8. 如何选择哪些证据放入 prompt？  
答：先按 score 排序，再按 `evidence_id` 去重、按 `max_evidence_items` 截断、按 token budget 裁剪。

9. 证据去重怎么做？  
答：对 evidence 用 `evidence_id` 去重；对文本 section 用 `label` 和内容归一化后的 key 去重。

10. 证据裁剪怎么做？  
答：超过 item 数或 token budget 的直接丢；如果是首个高分 section 过大，会截断并打上 `...[truncated by prompt budget]` 标记。

11. 裁剪时如何避免把关键字段裁掉？  
答：当前策略是先在结构化阶段把关键值提取成 `XmlArg`，prompt 里放的是围绕这些值的证据摘录；但它还不是 tokenizer-aware 的精细裁剪。

12. XML 模板很长时如何压缩？  
答：当前并不把整份历史模板塞进 prompt，而是转成 `selected_args`、contract 和 operation context，所以本质上是结构化替代，不是全文压缩。

13. 历史代码很长时如何压缩？  
答：当前没有专门的历史代码压缩器。

14. 有没有做摘要？  
答：没有做独立摘要模型；当前只有预算裁剪和结构化 section 组织。

15. 有没有做结构化压缩？  
答：有，operation context 会按 `node/template/selected_args/graph_paths/retrieval_matches` 这类字段组织，再 budget。

16. 有没有按章节、字段、规则优先级来裁剪？  
答：当前主要按 `score` 和 section label 优先级裁剪，没有做到按 PDF 章节级别裁剪。

17. rerank 在链路中起什么作用？  
答：当前 repo 里没有独立 reranker 模块；“rerank”更多是 router/profile/replay 里的请求类型命名，实际候选排序仍由本地 sparse score 完成。

18. rerank 使用什么模型？  
答：当前未实现独立 rerank 模型。

19. rerank 是在线做还是离线做？  
答：当前没有单独 rerank 服务，所以不存在这一层的在线/离线划分。

20. rerank 的延迟如何控制？  
答：当前不涉及独立 rerank 延迟；现有排序是本地规则打分。

21. 如何避免模型被无关证据干扰？  
答：依赖 filter、top-k、prompt budget、graph path 限量和 evidence section 去重。

22. 如果证据不足，模型应该怎么回答？  
答：当前 prompt 明确要求在证据不足时选 `needs_review`，而不是编值。

23. 如何判断检索结果不可靠？  
答：比如没有命中、命中很多但字段冲突、`missing_fields` 多、没有 `evidence_ids`、graph path 也不支持，这时当前链路会转 review。

24. Prompt 模板如何版本管理？  
答：当前主要通过 Git 管理 Python prompt builder 和 `configs/prompt` 目录；还没有单独的 prompt registry。

25. Prompt 改动后如何评测效果？  
答：通过 replay benchmark、system benchmark 和固定 workload regression 做对比。

---

## 5. XML / 代码生成、审核与修复闭环

1. 生成的诊断 XML 是什么格式？  
答：当前先生成 `XmlGenerationPlan` 这种中间 DSL，再渲染成 `ScriptNode`、`SerialNode` XML；还可以融合进历史主流程 workflow XML。

2. XML 需要满足哪些业务规则？  
答：至少要满足 ScriptNode 必填属性齐全、必填 Arg 不缺失、没有占位符、节点数量合理、ClassName 合法，并尽量带证据引用。

3. XML schema 是固定的吗？  
答：中间 DSL 是固定的，但历史 workflow 本身不完全固定，所以项目里用模板扫描和 contract 推断来适配不同 `ClassName`。

4. 如何保证 XML 语法合法？  
答：渲染用 `xml.etree.ElementTree`，校验用 `validate_xml` 再 parse 一次。

5. 如何保证 XML 语义合法？  
答：靠 `validate_xml_plan`、`workflow_audit`、`template_contract` 和 optional `llm_resolver` 共同保证，不只是看 XML 能不能 parse。

6. 结构化输出是怎么实现的？  
答：生成阶段是 Pydantic 模型；渲染阶段是确定性 XML renderer；LLM 阶段只允许返回 JSON 决策，不允许直接吐 XML。

7. 用 JSON schema、XML schema、正则，还是解析器校验？  
答：当前主要用 Pydantic schema、ElementTree 解析、以及占位符/十六进制这类正则检查，没有接外部 XSD。

8. 如果模型输出格式错误，如何修复？  
答：当前 LLM 只输出一个很小的 JSON 对象，`extract_json_object` 会从 markdown/code fence 里抽第一个 JSON；如果还是失败，就保留原 decision 并报 warning。

9. repair loop 的流程是什么？  
答：当前的 repair 更准确地说是“审计决策修复”：`workflow_audit -> resolve_audit_report -> resolve_audit_with_llm`。

10. repair 请求和普通生成请求有什么区别？  
答：普通生成是规则把 evidence 转成 XML plan；repair 请求只处理少量 unresolved 的 workflow 参数冲突。

11. repair prompt 里会放哪些内容？  
答：会放 `node_name/class_name/arg_name/audit_status/base_value/plan_value/fused_value/contract 信息/evidence_ids`，以及预算后的 evidence context。

12. 如何避免 repair 越修越错？  
答：当前限制很严：LLM 只能选 `keep_base/accept_plan/needs_review`，而且默认偏保守，证据不足时直接 `needs_review`。

13. repair 最多迭代几轮？  
答：当前只有一轮，没有多轮 self-repair。

14. 如果多轮修复仍然失败，如何处理？  
答：当前不存在多轮；一轮失败就保留 review 状态。

15. 如何做规则校验？  
答：有三层：诊断 DSL 规则校验、XML 中间 DSL 校验、最终 XML 结构校验。

16. 规则是硬编码还是配置化？  
答：校验规则大多硬编码在 Python；工作负载期望值是 manifest/config 配置化。

17. 规则校验失败后如何定位到具体字段？  
答：`ValidationIssue` 会带 `code/message/step_index`；workflow audit 还能精确到 `node_name.arg_name`。

18. 审核环节是人工审核还是自动审核？  
答：当前是“自动优先，人工兜底”。先自动出 audit 和 resolve，剩下 review item 再人工或 LLM 辅助。

19. 代码生成是否存在安全风险？  
答：如果让模型直接写 XML/C 当然有风险；当前仓库通过中间 DSL、固定 renderer 和有限动作，把风险压到比较低。

20. 如何防止生成危险代码？  
答：当前 LLM 不生成可执行代码，只做小范围决策；真正 XML/C 内容来自规则化 renderer。

21. 生成代码是否需要运行单元测试？  
答：当前不会去执行 XML 对 ECU 的真实逻辑，但仓库有大量单元测试和 regression 来验证生成链路稳定性。

22. 如何做生成结果回归测试？  
答：`run_workload_regression.py` 会检查产物是否存在、XML 是否有效、图谱覆盖、operation 数量、critical args 是否正确。

23. 历史 XML 模板如何作为 few-shot 示例使用？  
答：当前不是 few-shot 用法，而是把历史 XML 扫描成模板注册表和参数契约。

24. 模型生成结果如何和历史模板对齐？  
答：通过 `template_name -> class_name`、contract 推断、workflow 融合和 audit 对齐到历史流程。

25. 如何评估生成质量？  
答：看 `validation.valid`、audit review 数、resolution review 数、critical arg 命中、XML valid 和 graph/evidence 覆盖率。

26. 有没有构建标准答案集？  
答：当前没有自然语言标准答案集，但有固定 workload 的 `regression_expectations` 和 `critical_args`，可以视作结构化金标。

27. 生成结果如何上线？  
答：当前仓库只做到产物生成、审计和 benchmark，没有业务上线/发布流水线实现。

28. 生成链路中最常见的失败 case 是什么？  
答：常见是证据不足、节点在 base/fused workflow 中缺失、Arg 没被应用、参数冲突、占位符残留。

29. 如何解决模型幻觉字段？  
答：当前做法是不让模型直接造字段，字段必须来自 evidence 提取或模板契约，否则走 `ignore_plan_arg` 或 `needs_review`。

30. 如何保证不同车型配置下生成结果正确？  
答：当前主要依赖 ECU 级证据、模板契约和 workload 回归；车型/年款的显式配置矩阵还没有做全。

---

## 6. vLLM 推理服务核心原理

1. 为什么选择 vLLM？  
答：因为它天然适合高吞吐 serving，有 OpenAI-compatible API，便于和当前仓库的 HTTP client、router、benchmark 融合。

2. vLLM 相比 HuggingFace Transformers 直接推理有什么优势？  
答：当前项目最看重的是 serving 形态更成熟、吞吐更高、支持 continuous batching、方便做 TP 和 OpenAI-compatible 接口。

3. vLLM 的核心设计理念是什么？  
答：核心就是把 KV Cache 管理、调度和在线服务做成高吞吐系统，而不是只做单请求推理。

4. PagedAttention 是什么？  
答：它可以理解成把 KV Cache 按页管理，而不是给每个请求预留一整段连续大块显存。

5. PagedAttention 解决了什么问题？  
答：主要解决 KV Cache 浪费和显存碎片问题。

6. KV Cache 为什么会浪费显存？  
答：因为不同请求长度不同，如果按连续大块预留，很多空间其实用不到。

7. PagedAttention 如何管理 KV Cache？  
答：逻辑上仍然是一条序列，物理上拆成多个 page/block，通过映射来访问。

8. continuous batching 是什么？  
答：就是请求不需要等整批凑齐，可以在 decode 循环里持续插入新请求。

9. continuous batching 和传统 batch 有什么区别？  
答：传统 batch 更像固定一批一起跑；continuous batching 是动态混合 prefill 和 decode，资源利用率更高。

10. vLLM 的调度器是怎么工作的？  
答：底层细节由 vLLM 负责，当前仓库把它当黑盒；我们主要通过 benchmark 和参数如 `max_num_seqs/max_num_batched_tokens` 来观察调度效果。

11. prefill 和 decode 阶段有什么区别？  
答：prefill 处理整个输入上下文，决定 TTFT；decode 是一个 token 一个 token 地往后生成，决定 TPOT。

12. 为什么 decode 阶段通常更容易成为瓶颈？  
答：因为它有强串行依赖，每生成一个 token 都要再走一步，GPU 利用率未必像 prefill 那样高。

13. TTFT 和 TPOT 分别受哪些因素影响？  
答：TTFT 主要受 prompt 长度、排队、TP 通信和 prefill 速度影响；TPOT 主要受 decode 性能、量化、batch 和模型规模影响。

14. prefix cache 是什么？  
答：就是当前缀 prompt 重复时，复用之前的前缀计算结果。

15. prefix cache 在你们场景中有用吗？  
答：对 warm 模式、模板化短请求、固定前缀 prompt 是有帮助的；对真实长上下文冷请求帮助有限。

16. 什么时候 prefix cache 命中率高？  
答：同类审计 prompt、重复 workload replay、warm cache benchmark 这种场景命中率更高。

17. 什么时候 prefix cache 反而帮助不大？  
答：`cache-mode=cold`、每次 evidence 都不一样、长上下文差异很大的请求里帮助就不大。

18. vLLM 支持哪些量化方式？  
答：从当前仓库口径讲，真正验证跑通的是 AWQ 4bit 的 Qwen3-Coder-30B；另外保留了 GGUF 启动脚本，但 README 里明确写了当前不能完整加载 Qwen3-VL GGUF。

19. 量化对吞吐、显存、精度有什么影响？  
答：一般是显存占用更低、部署更容易，吞吐通常更好，但可能有精度损失；当前仓库就是靠 AWQ 才把 30B 模型稳定放到双卡 TP2 上跑 benchmark。

20. vLLM 如何支持 OpenAI-compatible API？  
答：当前就是直接用 `vllm serve ...`，对外暴露 `/v1/models` 和 `/v1/chat/completions`。

21. vLLM streaming 输出底层怎么实现？  
答：在当前项目里，表现为 SSE 风格的 `data:` 事件流；`benchmark_vllm_chat.py` 会逐行读取并在首个 token 到达时记录 TTFT。

22. vLLM 服务如何限制最大上下文长度？  
答：启动参数 `--max-model-len`；当前 router 还会按 endpoint 的 `min/max_prompt_tokens` 做二次约束。

23. vLLM 如何处理超长请求？  
答：当前项目里主要是“预防式处理”：router 会把请求按 tier 分流，超过 endpoint 上限的会直接没有可路由目标，而不是在线自动截断。

24. vLLM 的 `max_num_batched_tokens` 怎么调？  
答：当前通过环境变量和 `run_vllm_tuning_sweep.py` 做 sweep，比对 replay benchmark 结果，不在代码里写死最佳值。

25. vLLM 的 `max_num_seqs` 怎么调？  
答：同样是通过 tuning sweep 结合真实 replay 数据去调。

26. `gpu_memory_utilization` 设置过高有什么风险？  
答：最直接的风险就是 OOM 和服务不稳定。当前配置普遍用 `0.80~0.86`，不是把显存打满。

27. vLLM 的 swap space 有什么作用？  
答：这是 vLLM 层面的显存缓冲能力，但当前仓库没有显式配置和调优它，所以不是当前实现重点。

28. vLLM 中 tensor parallel size 如何设置？  
答：当前通过环境变量 `TENSOR_PARALLEL_SIZE` 设置；长上下文/30B 路径默认是 `TP=2`。

29. vLLM 多卡推理怎么通信？  
答：底层依赖 NCCL/allreduce 一类通信。当前项目还专门做了 `diagnose_tp2_comm.py`，发现现网机器上存在 `Custom allreduce is disabled` 和 GPU P2P 能力不足的证据。

30. vLLM 推理中最容易遇到的 OOM 场景有哪些？  
答：长上下文、大并发、高 `max_tokens`、高 `gpu_memory_utilization`、以及过激进的 `max_num_batched_tokens` 都容易触发 OOM。

---

## 7. TP / PP / DP / EP 与推理部署

1. TP、PP、DP、EP 分别是什么？  
答：TP 是张量并行，PP 是流水并行，DP 是多副本并行，EP 是专家并行。当前项目实际重点只有 TP 和“多实例副本”。

2. TP 在推理中怎么用？  
答：把同一个模型拆到多张卡上服务同一个请求。当前项目就是双卡 `TP=2` 跑 30B 模型和长上下文。

3. PP 在推理中怎么用？  
答：理论上是把模型层切到不同阶段，但当前项目没有落地 PP。

4. DP 在推理中怎么用？  
答：在当前项目语境里，最接近的是双实例部署，即两个独立 serving 副本各自接请求。

5. EP 在推理中怎么用？  
答：主要面向 MoE 模型做 expert shard，当前项目没有用到。

6. 为什么大模型推理常用 TP？  
答：因为单卡经常放不下模型和长上下文的 KV Cache，而 TP 能把权重和计算摊到多卡。

7. TP2 和双实例部署有什么区别？  
答：TP2 是两张卡共同服务一个模型实例；双实例是两张卡各跑一个独立实例，各自处理不同请求。

8. 1GPU、TP2、双实例分别适合什么场景？  
答：1GPU 适合小模型或短上下文 smoke test；TP2 适合长上下文和大模型；双实例更适合大量短请求和高并发。

9. TP2 会提升吞吐还是降低延迟？  
答：它更核心的价值是“把模型和上下文放下并跑稳”。对长请求可能更有价值，但当前 benchmark 显示短请求吞吐通常不如双实例。

10. TP2 的通信开销来自哪里？  
答：主要来自多卡间的 allreduce/P2P/NCCL 通信。

11. 双实例 round-robin 和 TP2 的本质区别是什么？  
答：前者是请求级负载均衡，后者是模型级分布式执行。

12. 双实例部署为什么可能比 TP2 吞吐更高？  
答：因为它没有每个 token 都跨卡通信的成本，多个短请求可以真正并行落到两个独立 worker 上。

13. 单请求长上下文时，TP2 是否更有优势？  
答：是的，尤其在单卡显存放不下时，TP2 是更现实的方案。

14. 多短请求并发时，双实例是否更有优势？  
答：是的，当前项目的 topology benchmark 就体现了这一点。

15. DP 在 serving 中是不是就是多副本？  
答：在当前工程表达里，可以这么理解。

16. EP 主要用于什么模型？  
答：主要用于 MoE。

17. MoE 模型推理中 EP 解决什么问题？  
答：解决 expert 很多、单卡放不下或专家路由需要分摊的问题。

18. PP 为什么在在线低延迟推理中不一定常用？  
答：因为它会引入 stage 间流水和气泡，在线低延迟场景收益不一定高。

19. 如何判断某个模型需要 TP？  
答：如果单卡放不下模型+KV，或者长上下文下单卡性能/稳定性太差，就需要 TP。当前 30B 长上下文路径就是这个判断。

20. 如何根据显存估算能否单卡部署？  
答：原则上看“模型权重 + KV Cache + runtime buffer”总和；但当前项目更偏工程实证，直接用单卡/TP2/双实例 benchmark 去验证。

---

## 8. FastAPI 与 OpenAI-compatible 在线服务

1. 为什么用 FastAPI？  
答：因为它足够轻、类型友好，适合把当前这条工程链路先做成 API。

2. 服务接口如何设计？  
答：当前按功能拆成文档解析、知识归一化、索引、检索、XML 计划生成、校验、融合、审计、修复和 workload 运行接口。

3. OpenAI-compatible 接口需要兼容哪些字段？  
答：当前实际兼容了 `model/messages/temperature/max_tokens/stream/min_tokens/stream_options.include_usage` 这类核心字段。

4. `/v1/chat/completions` 如何实现？  
答：当前不是 FastAPI 自己实现，而是由外部 `vllm serve` 提供；项目代码只是调用它。

5. streaming 和 non-streaming 接口有什么区别？  
答：streaming 按 SSE 增量返回 token，可采 TTFT；non-streaming 一次性返回完整 JSON。

6. SSE 是什么？  
答：Server-Sent Events，服务端持续推送文本事件流。

7. 流式输出中如何处理客户端断开？  
答：当前仓库没有在 FastAPI 层自己实现 streaming proxy，所以没有自定义断开处理逻辑，主要依赖 vLLM 本身。

8. 如何处理请求取消？  
答：当前没有显式 cancel API。

9. 如何处理超时？  
答：客户端和脚本层都有 `timeout_seconds`，子进程跑 benchmark/pipeline 也有超时控制。

10. 如何做限流？  
答：当前未实现。

11. 如何做鉴权？  
答：当前 vLLM 调用层保留了 Bearer API key 字段，但默认是 `EMPTY`；FastAPI 业务接口没有鉴权中间件。

12. 如何做请求日志？  
答：当前主要保留脚本 stdout/stderr、benchmark samples 和 vLLM 启动日志，没有统一的 FastAPI request logging middleware。

13. 如何做 trace id？  
答：当前只在 router 层做了 `route trace id`，还没有做到全链路 HTTP trace middleware。

14. 如何把 route trace 返回给调用方？  
答：当前没有通过 API 回传，只能从 router 的 `recent_traces()` 里读。

15. FastAPI 服务如何和 vLLM engine 通信？  
答：通过 HTTP 调 OpenAI-compatible endpoint。

16. 是直接调用 vLLM Python API，还是通过 HTTP 转发？  
答：当前是 HTTP。

17. 多实例下如何做负载均衡？  
答：当前通过 `VllmRouter + VllmEndpointPool` 做 endpoint 级 round-robin。

18. 如何保证接口幂等？  
答：当前没有实现幂等 key；多数 API 是确定性纯函数或文件产物生成，但不是严格幂等设计。

19. 如何处理大请求体？  
答：当前没有 FastAPI 侧的特殊大包处理，更多依赖 `max_model_len`、router tier 和 prompt budget。

20. 如何处理高并发下的连接数问题？  
答：当前只在 benchmark 工具里模拟并发，没有在 FastAPI 层做连接池、worker 数或 keep-alive 优化。

---

## 9. Profile-aware + Context-tier 路由治理

1. profile-aware routing 是什么？  
答：就是按请求类型路由。当前 profile 有 `short_audit`、`rerank`、`long_context`、`repair`、`default`。

2. context-tier routing 是什么？  
答：就是按 prompt token 长度分层。当前分成 `short/standard/extended/extreme/overflow`。

3. 为什么要按 4k、32k、64k、128k 分层？  
答：因为当前 benchmark 和文档已经把这几个窗口做成了稳定口径，且不同档位对应不同部署策略。

4. short_audit、rerank、long_context、repair 分别代表什么请求类型？  
答：`short_audit` 是短 JSON 审计冲突判断；`rerank` 是证据重排类请求；`long_context` 是带 graph path 和多条 evidence 的长上下文校核；`repair` 是 unresolved conflict 的修复建议。

5. 如何识别请求类型？  
答：当前主要由调用方显式传 `request_profile`，不是模型自动识别。

6. 请求类型是用户显式传入，还是服务自动判断？  
答：profile 是显式传入；context tier 是根据 prompt token 长度自动判断。

7. 路由规则是硬编码还是配置化？  
答：核心逻辑写在 Python 里，但 endpoint URL、tier 边界和 short/long 端口可以通过 CLI 参数和构造函数注入。

8. 路由策略如何动态调整？  
答：可以在运行时调整 endpoint 配置、health state 和 probe 结果；但没有单独的策略中心。

9. 为什么需要双实例 round-robin？  
答：因为当前 benchmark 已经证明短请求并发下双实例吞吐更高，适合 `short_audit/rerank`。

10. round-robin 有什么缺点？  
答：它不感知实时负载，可能把请求分到一个刚恢复、但其实更慢的实例上。

11. 有没有做 least-load 或 queue-length-based routing？  
答：当前没有。

12. 如何判断某个实例健康？  
答：实例必须 enabled，且 circuit 没打开；如果开启 probe，还需要 `/v1/models` 探活成功。

13. health check 检查哪些指标？  
答：当前检查的是 HTTP 探活结果、最后一次延迟、连续失败次数和最后错误信息，不是 GPU 指标级 health check。

14. 熔断机制怎么设计？  
答：连续失败超过阈值就打开 circuit，进入 cooldown；到期后 probe 成功再恢复。

15. 什么情况下触发熔断？  
答：请求失败被 `mark_failure` 记录，或主动 probe 失败，且累计到 `failure_threshold`。

16. 熔断后请求路由到哪里？  
答：路由到同 profile、同 prompt 范围内其他健康 endpoint；如果没有就直接报错。

17. 半开状态怎么恢复？  
答：当前没有单独 half-open 状态枚举，而是 cooldown 到期后重 probe，probe 成功就清零失败计数并恢复。

18. route trace 记录哪些信息？  
答：记录 `trace_id/profile/prompt_tokens/context_tier/selected_endpoint/candidate_endpoints/healthy_endpoints/degraded/fallback_reason/created_at/completed_at/success/latency/error`。

19. route trace 对排障有什么帮助？  
答：能看到某次请求为什么没走首选实例、走的是哪个 tier、是否 degraded、错误是什么，特别适合排查尾延迟和异常路由。

20. 如果长上下文请求把实例打满，如何保护短请求延迟？  
答：当前做法就是 profile + tier 隔离，把短请求优先导到双实例，把长请求放到 TP2 或 extended/extreme 实例。

21. 如何避免 head-of-line blocking？  
答：靠不同拓扑和 tier 的 endpoint 隔离；当前没有实现同一实例内的优先级队列。

22. 如何做优先级队列？  
答：当前未实现。

23. 如何做请求隔离？  
答：当前按 request profile 和 prompt token 区间做实例隔离。

24. 如何防止 repair 请求占用过多资源？  
答：当前一方面把 `repair` 归到 long profile，另一方面 `resolve-audit-llm` 还有 `max_items` 限制，避免一次处理太多 review item。

25. 如果 128k 请求 OOM，系统如何降级？  
答：当前实现更偏向“提前分层和失败显式化”，没有自动把 128k 请求压缩成低窗口继续跑；需要把这类请求路由到 extreme 实例，否则直接失败。

---

## 10. Benchmark 与可观测性

1. workload-driven benchmark 是什么？  
答：就是 benchmark 不跑抽象空 prompt，而是绑定固定 workload、固定命令和固定输出报告格式，保证可复现。

2. 为什么要用真实请求 replay？  
答：因为光跑合成 prompt 看不出真实业务 mix，replay 才能覆盖 `short_audit/rerank/long_context/repair` 的真实比例。

3. replay 如何脱敏？  
答：当前仓库没有专门脱敏流水线，只是把真实 trace 转成结构化 prompt；如果要上生产，这块还需要补。

4. replay 如何保证可重复？  
答：通过固定的 replay dataset JSON、固定 cache mode、固定 concurrency 和固定 benchmark 脚本保证重复执行。

5. benchmark 的请求集如何构造？  
答：一类来自 `configs/benchmark/*.json` 的系统场景，另一类来自 `flow_evidence_trace + resolution_report + llm_resolution_report` 生成的 replay dataset。

6. 为什么测试 1k、4k、8k、16k 输入长度？  
答：它们正好覆盖短上下文、常规上下文和当前 TP2 16k 上限附近的 prefill 区间，便于看 TTFT 随输入增长的变化。

7. 为什么测试 1、4、8、16 并发？  
答：分别对应单请求基线、连续批处理起效点、中高并发拐点和容量极限压测。

8. TTFT 是什么？  
答：首个 token 返回时间。

9. TPOT 是什么？  
答：第一个 token 之后，平均每个输出 token 的时间。

10. P50、P95、P99 分别代表什么？  
答：分别代表中位数、95 分位和 99 分位，越往后越能体现尾延迟。

11. requests/s 怎么计算？  
答：并发 benchmark 里是 `请求数 / 墙钟时间`。

12. total tokens/s 怎么计算？  
答：是 `总 completion tokens / 墙钟时间`。

13. input tokens/s 和 output tokens/s 是否分开统计？  
答：当前 output tokens/s 是显式统计的；input 侧通过 `prompt_tokens` 和 `/metrics` 中的 prompt 相关 counter 间接统计，二者是分开的。

14. 为什么要同时看延迟和吞吐？  
答：因为只看吞吐可能把短请求延迟打爆，只看延迟又可能浪费大量 GPU 资源。

15. 你们的 P50 requests/s = 3.90 说明什么？  
答：这说明在当前 `2INST` 拓扑 benchmark 条件下，中位数吞吐大约是每秒 3.9 个请求。

16. total tokens/s P50 = 277 说明什么？  
答：说明同样在 `2INST` 拓扑下，中位数总输出吞吐大约是每秒 277 个生成 token。

17. 这组指标是在什么模型、什么 GPU、什么上下文长度下测的？  
答：按当前文档口径，这组拓扑 baseline 来自 Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit，在双 3090 环境下测得；topology 场景默认用 512 输入 token、`max_tokens=96` 的并发 benchmark。

18. 如果面试官问这组指标偏低，你怎么解释？  
答：可以解释为当前测的是 30B 模型、保守的 `enforce-eager`、真实 HTTP serving 路径，而且 TP2 通信环境还有 P2P 限制，所以不是极限单卡跑分。

19. 如果面试官问这组指标偏高，你怎么证明？  
答：证明方式就是看完整 benchmark 口径：固定 `1 warmup + 3 measured`、原始 samples 保留、P95/P99 可复查、还能对照 `/metrics` delta。

20. benchmark 是否包含 warmup？  
答：默认包含，系统 benchmark 和扩展矩阵默认都是 `1 warmup + 3 measured`。

21. benchmark 是否考虑冷启动？  
答：模型冷启动不放在默认场景里统计，但拓扑 benchmark 会显式拉起 vLLM 再等待 ready；另外 prompt 冷热通过 `cache-mode` 区分。

22. benchmark 是否考虑首 token 抖动？  
答：考虑，通过多次样本的 TTFT 分布和 P95/P99 体现。

23. benchmark 是否考虑长尾请求？  
答：考虑，因为报告里固定保留 P95/P99 和 quality warnings。

24. tuning sweep 调了哪些参数？  
答：当前 sweep 了 `ENABLE_PREFIX_CACHING`、`ENABLE_CHUNKED_PREFILL`、`MAX_NUM_SEQS`、`MAX_NUM_BATCHED_TOKENS`。

25. Prometheus `/metrics` 暴露了哪些指标？  
答：当前项目不限定具体名字，而是把 vLLM `/metrics` 里可聚合的数值型指标全部抓下来，并按 metric name 聚合。

26. 如何采集 TTFT？  
答：在 streaming 模式下读取到第一个 token 的时间戳减去请求开始时间。

27. 如何采集 TPOT？  
答：用 `(duration - TTFT) / (completion_tokens - 1)` 计算。

28. 如何采集队列等待时间？  
答：当前没有直接独立采集 queue wait，只能通过 TTFT、并发场景和 route trace 间接判断。

29. 如何采集 GPU 显存和利用率？  
答：当前更偏向通过 vLLM `/metrics` 间接采，必要时再结合 `nvidia-smi`；仓库里没有完整 GPU profiler。

30. 如何定位吞吐瓶颈？  
答：看输入长度矩阵、输出长度矩阵、并发矩阵和 topology matrix，再结合 `/metrics` 和 TP2 诊断判断是 prefill、decode 还是通信问题。

31. 如何定位 P95 延迟抖动？  
答：先看 route trace 和 raw samples，再对比 cold/warm、TP2/2INST 和不同 concurrency。

32. 如何判断瓶颈在 CPU、GPU、网络还是调度器？  
答：当前主要靠实验对比法：切 topology、切 cache mode、切 TP 参数、看 TTFT/TPOT 分离指标，再结合 TP2 通信诊断。

33. Grafana 面板会展示哪些图？  
答：当前没实现 Grafana，但按这个项目我会重点看 TTFT、TPOT、requests/s、tokens/s、running requests、prompt/completion tokens 和 GPU 显存相关曲线。

34. 如何做报警？  
答：当前未实现；但 benchmark 里的 threshold 已经给了很好的报警基线。

35. benchmark 结果如何指导路由策略？  
答：当前文档已经直接给出结论：`short_audit/rerank` 优先走 `2INST`，`long_context/repair` 优先走 TP2，超长上下文再分 `extended/extreme`。

---

## 11. 性能优化与稳定性

1. 推理服务最主要的性能瓶颈是什么？  
答：当前项目里最明显的是长上下文 prefill 和 TP2 跨卡通信。

2. 长上下文请求为什么慢？  
答：因为 prefill 要处理整段 prompt，KV Cache 也更大，TTFT 会随上下文长度明显增长。

3. KV Cache 显存如何估算？  
答：原则上和层数、hidden size、token 数、batch 数都相关；当前仓库没有写精确估算器，更多是通过 benchmark 和 `max_model_len` 实测。

4. batch size 越大越好吗？  
答：不是，batch 大了吞吐可能更高，但尾延迟、显存占用和 OOM 风险也会升高。

5. 并发越高越好吗？  
答：也不是。当前 benchmark 就是为了找并发拐点，不是越大越优。

6. 如何平衡吞吐和延迟？  
答：靠 profile-aware routing、topology 分离和真实 workload benchmark，不是盲调单个参数。

7. 如何保护短请求？  
答：把短请求导到双实例，不和长上下文 TP2 共用一条主路。

8. 如何做限流和排队？  
答：当前没有正式的限流器和优先级队列，只做了实例隔离和请求分类。

9. 如何防止 OOM？  
答：控制 `max_model_len/gpu_memory_utilization/max_tokens`，给长请求专门实例，prompt budget 也尽量收缩输入。

10. 如何处理 CUDA OOM 后服务不稳定？  
答：当前 benchmark 管理器会在 stop 时杀整个 process group，必要时 SIGKILL，避免残留 worker 持续占显存。

11. 如何做模型热启动？  
答：当前做法是启动 vLLM 后先 wait ready，再进入 benchmark/在线调用。

12. 如何做优雅重启？  
答：当前实现有 `ManagedVllmService.start/stop()` 和端口 ready/down 等待逻辑，但还不是完整生产级滚动重启系统。

13. 如何做多实例滚动升级？  
答：当前未实现。

14. 如何做版本回滚？  
答：当前没有部署平台级回滚，只能切回旧脚本/旧配置/旧模型路径。

15. 如何处理模型输出中断？  
答：如果 HTTP/stream 中断，benchmark 会标成失败；路由侧会记录失败并可能触发熔断。

16. 如何处理客户端超时但模型还在生成？  
答：当前没有显式取消机制，所以这是现阶段的缺口。

17. 如何清理无效请求占用的 KV Cache？  
答：当前没有自己管理 KV Cache，依赖 vLLM 内部处理。

18. 如何设计服务日志？  
答：当前重点是保留可回放的 benchmark samples、vLLM 启动日志、TP2 诊断报告和 route trace，而不是只打一行访问日志。

19. 如何定位线上偶发错误？  
答：先看 route trace、health snapshot、benchmark raw sample、vLLM 日志，再判断是 endpoint 异常还是 prompt/模型问题。

20. 如何压测到服务极限？  
答：当前已经有并发 16、输入 16k、上下文极限到 128k、以及 1GPU/TP2/2INST 的完整矩阵。

21. 如何做容量规划？  
答：用 replay benchmark 和 topology benchmark 拿到各 profile 的 `requests/s`、`tokens/s`、TTFT/TPOT，再按业务比例估算所需副本数。

22. 如果请求量翻倍，你如何扩容？  
答：短请求优先横向加实例，长请求优先加 TP 或专门长上下文实例，再把 router 的 profile/tier 重新绑定。

23. 如果模型从 7B 升级到 32B，架构怎么变？  
答：通常就不能再默认单卡，要引入 TP、多拓扑评估、更严格的 prompt budget 和更高规格的 benchmark。

24. 如果上下文从 32k 扩到 128k，架构怎么变？  
答：需要把 router 的 `extended/extreme` tier 真正落到不同实例/端口上，同时更强调预填充吞吐和 OOM 防护。

25. 如果要支持多模型，路由如何设计？  
答：当前 endpoint 已经有 `model` 字段，所以扩展路径很清楚：在 profile/tier 之外再加 model 维度，把不同模型挂到不同 endpoint pool。
