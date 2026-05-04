# 推理与 Benchmark 优化记录

本文档记录当前阶段已落地的优化项，目标是让 vLLM 接入、GraphRAG 证据使用和 benchmark 指标形成闭环：先可观测，再可比较，最后再做性能优化。

## 1. Benchmark 口径收紧

已将系统 benchmark 从单次 smoke 扩展为固定矩阵：

- 默认每个场景执行 `1` 次 warmup 和 `3` 次 measured iteration；
- 统计 `P50/P90/P95/P99`、`stdev`、`CV`，避免只看平均值；
- 输入长度、并发、拓扑矩阵使用 `cache-mode=cold`，在 prompt 开头加 salt，避免 prefix cache 把 prefill 测成 warm cache；
- 输出长度矩阵使用 `cache-mode=warm`，并用 `min_tokens` 强制生成接近目标输出长度，用于隔离 decode 性能；
- 报告保留原始 `samples`、threshold checks、quality warnings，方便追溯失败原因。

主要文件：

- `src/diagnostic_platform/evaluation/benchmark.py`
- `configs/benchmark/treg_20260402_system.json`
- `configs/benchmark/treg_20260402_vllm_expansion.json`
- `scripts/run_system_benchmark.py`
- `scripts/run_complete_benchmark.py`
- `docs/benchmark_matrix.md`

## 2. Serving Router

新增 profile-aware vLLM 路由层，用于把不同 LLM 请求分配到不同 serving 拓扑：

- `short_audit`、`rerank`：适合短 prompt、高并发，可走双实例 `2INST`；
- `long_context`、`repair`、`default`：适合长上下文或复杂修复，可走双卡 TP2；
- endpoint 选择使用确定性 round-robin，并按 `profile` 和 `max_prompt_tokens` 过滤。

主要文件：

- `src/diagnostic_platform/serving/profiles.py`
- `src/diagnostic_platform/serving/endpoint_pool.py`
- `src/diagnostic_platform/serving/router.py`
- `src/diagnostic_platform/generation/vllm_client.py`

当前接入点：

- `VllmClient` 支持传入 `VllmRouter`；
- `scripts/resolve_audit_with_llm.py --enable-router` 可启用路由；
- 2 实例场景可通过重复传入 `--short-base-url` 指定 `8008/8009`。

示例：

```bash
python scripts/resolve_audit_with_llm.py \
  --enable-router \
  --short-base-url http://127.0.0.1:8008/v1 \
  --short-base-url http://127.0.0.1:8009/v1 \
  --long-base-url http://127.0.0.1:8008/v1
```

## 3. Prompt Budgeter

新增 prompt 预算层，避免把过长证据、重复证据或低价值图路径直接塞进 LLM prompt：

- 对 `NodeEvidenceBundle` 执行 evidence 去重、score 排序、top-k 裁剪和 graph path 裁剪；
- 对任意文本 section 执行去重、预算裁剪和必要截断；
- 预算报告记录原始数量、保留数量、估算 token、丢弃原因；
- LLM 审计解析现在可从 `flow_evidence_trace.json` 加载证据上下文，并在 prompt 中加入预算后的 evidence context。

主要文件：

- `src/diagnostic_platform/retrieval/prompt_budget.py`
- `src/diagnostic_platform/generation/prompt_builder.py`
- `src/diagnostic_platform/resolution/llm_resolver.py`
- `scripts/resolve_audit_with_llm.py`

示例：

```bash
python scripts/resolve_audit_with_llm.py \
  --evidence-trace-path data/processed/xml_plan/treg_20260402/flow_evidence_trace.json \
  --prompt-budget-tokens 4096 \
  --prompt-reserved-output-tokens 512
```

当前边界：

- token 估算使用 `chars / 4`，不是 tokenizer 精确计数；
- 预算层优先保证稳定和可控，后续如要更精确，可接入 Qwen tokenizer；
- 证据上下文来自 trace 文件，因此需要先运行 `scripts/generate_xml_plan.py` 生成 `flow_evidence_trace.json`。

## 4. vLLM Metrics Scraper

新增 Prometheus `/metrics` 采集，用于把 vLLM 服务端指标写入 benchmark 报告：

- benchmark 场景支持配置 `metrics_urls`；
- 每个场景 measured iteration 前后抓取 metrics；
- 报告写入 `observability.metrics_before`、`observability.metrics_after`、`observability.metric_delta`；
- 常用 counter delta 会扁平化到 `metrics.vllm_delta_*`，便于 threshold 或后处理比较；
- 拓扑 benchmark 会自动根据 base URL 生成 `/metrics` URL。

主要文件：

- `src/diagnostic_platform/serving/metrics.py`
- `src/diagnostic_platform/evaluation/benchmark.py`
- `configs/benchmark/treg_20260402_vllm_expansion.json`
- `scripts/run_complete_benchmark.py`

## 5. 验证路径

轻量验证：

```bash
python -m py_compile \
  src/diagnostic_platform/generation/vllm_client.py \
  src/diagnostic_platform/generation/prompt_builder.py \
  src/diagnostic_platform/resolution/llm_resolver.py \
  src/diagnostic_platform/serving/profiles.py \
  src/diagnostic_platform/serving/endpoint_pool.py \
  src/diagnostic_platform/serving/router.py \
  src/diagnostic_platform/serving/metrics.py \
  src/diagnostic_platform/retrieval/prompt_budget.py \
  src/diagnostic_platform/evaluation/benchmark.py \
  scripts/resolve_audit_with_llm.py \
  scripts/run_complete_benchmark.py

python -m unittest \
  tests.test_serving_router \
  tests.test_prompt_budget \
  tests.test_serving_metrics \
  tests.test_vllm_llm_resolver \
  tests.test_system_benchmark
```

完整回归：

```bash
python -m unittest discover -s tests
```

完整 benchmark：

```bash
python scripts/run_complete_benchmark.py
```

完整 benchmark 会启动并停止 vLLM 服务，耗时和 GPU 占用明显高于单元测试。更新含 `/metrics` 的报告时再执行。

## 6. 本轮实测结果

2026-04-19 已完成一次带 vLLM `/metrics` 的完整 benchmark：

- `system_no_vllm`：通过，2 scenarios，6 measured samples；
- `vllm_tp2_expansion`：通过，11 scenarios，33 measured samples；
- `topology_tp2`：通过，3 measured samples；
- `topology_single_gpu`：通过，3 measured samples；
- `topology_dual_instance`：通过，3 measured samples；
- 所有 vLLM 场景均包含 `observability` 和 `vllm_delta_*` 服务端指标；
- benchmark 结束后 GPU 显存已释放到桌面基线。

报告路径：

```text
data/reports/benchmark/treg_20260402_complete_benchmark.json
data/reports/benchmark/treg_20260402_vllm_expansion_benchmark.json
data/reports/benchmark/treg_20260402_system_benchmark.json
```

本轮同时修复了完整 benchmark 的 vLLM 进程清理问题：

- `scripts/run_complete_benchmark.py` 的 `_wait_endpoint_down()` 现在会把服务关闭瞬间的 `ConnectionResetError` 视为 endpoint 已下线；
- `ManagedVllmService.stop()` 现在会二次确认 process group 退出，必要时执行 SIGKILL，避免 vLLM worker 残留占用显存。

## 7. 下一步优化顺序

优先级建议：

1. 跑一次完整 benchmark，确认新报告中 `observability` 和 `vllm_delta_*` 字段是否稳定；
2. 基于 `TTFT`、`TPOT`、`requests/s`、`vllm_delta_*` 定位当前瓶颈是 prefill、decode、排队还是拓扑；
3. 如果短请求占主导，优先比较 `2INST` 与 `TP2`，把 `short_audit/rerank` 默认切到双实例；
4. 如果长上下文占主导，优先优化 prompt budget 和 GraphRAG evidence top-k，而不是盲目调 serving 参数；
5. 最后再进入 vLLM 参数优化，如 `gpu_memory_utilization`、`max_num_seqs`、chunked prefill、prefix cache 策略。

## 8. 2026-04-22 新增 AI Infra 落地

本轮新增 4 个基础设施能力：

1. `P0 真实 workload 回放集`
   - 新增 `ReplayWorkloadDataset / ReplayWorkloadItem`
   - 新增 `src/diagnostic_platform/evaluation/replay.py`
   - 新增 `scripts/build_workload_replay.py`
   - 已基于 `flow_evidence_trace + audit_resolution_report + llm_resolution_report` 生成真实回放集：
     `data/processed/replay/treg_20260402_workload_replay.json`
   - 当前回放集包含 34 个真实请求：
     - `short_audit=6`
     - `rerank=11`
     - `long_context=11`
     - `repair=6`

2. `P1 router 健康检查 + 熔断 + route trace`
   - `src/diagnostic_platform/serving/router.py` 新增：
     - endpoint health state
     - failure threshold / cooldown circuit breaker
     - 周期性 probe
     - route trace 记录
   - `src/diagnostic_platform/generation/vllm_client.py` 已接入：
     - 请求成功回写 `mark_success`
     - 请求失败回写 `mark_failure`

3. `P2 vLLM tuning sweep`
   - `scripts/start_vllm_qwen.sh` 新增运行时参数入口：
     - `ENABLE_PREFIX_CACHING`
     - `ENABLE_CHUNKED_PREFILL`
     - `MAX_NUM_SEQS`
     - `MAX_NUM_BATCHED_TOKENS`
   - 新增 `scripts/benchmark_vllm_replay.py`
   - 新增 `scripts/run_vllm_tuning_sweep.py`
   - 新增 `configs/benchmark/treg_20260402_vllm_replay.json`

4. `P3 TP2 通信路径排查`
   - 新增 `scripts/diagnose_tp2_comm.py`
   - 当前诊断结论已输出到：
     `data/reports/infra/treg_20260402_tp2_comm_diagnosis.json`
   - 现有证据链：
     - GPU0<->GPU1 拓扑关系为 `NODE`
     - TP2 启动日志存在 `Custom allreduce is disabled`
     - TP2 启动日志存在 `lacks GPU P2P capability`

## 9. 2026-04-22 上下文分层路由与严谨窗口 Benchmark

本轮继续补齐两项面向 AI infra 的能力，目标是把“上下文长度”从单纯的模型参数，提升为可路由、可比较、可回归的 serving 维度。

### 9.1 上下文分层路由

`src/diagnostic_platform/serving/router.py` 已新增 `ContextTierPolicy`：

- `short <= 4k`
- `standard <= 32k`
- `extended <= 64k`
- `extreme <= 128k`
- 超出 `128k` 记为 `overflow`

对应改动：

- `RouteTrace` 新增 `context_tier`
- `VllmRoute` 新增 `context_tier`
- `build_default_router()` 支持：
  - `extended_base_url`
  - `extreme_base_url`
  - `context_tier_policy`
- `VllmEndpoint` 新增 `min_prompt_tokens`，可把不同端口绑定到不同上下文区间

这意味着当前 router 不再只按 `profile` 分流，还能按 prompt token 长度把请求送到：

- 标准长上下文实例
- 扩展上下文实例
- 极长上下文实例

当前 CLI 已接入：

- `scripts/resolve_audit_with_llm.py --extended-base-url ...`
- `scripts/resolve_audit_with_llm.py --extreme-base-url ...`

### 9.2 严谨版窗口 Benchmark

新增 `scripts/run_context_window_benchmark.py`，和之前的 `run_context_extreme_trials.py` 做职责分离：

- `run_context_extreme_trials.py`
  - 用于回答“某个窗口能不能跑通”
  - 偏容量验证
- `run_context_window_benchmark.py`
  - 用于回答“这个窗口在严谨口径下延迟如何”
  - 偏回归评测

严谨版窗口 benchmark 的约束：

- 每个档位固定 `1 warmup + 3 measured`
- 使用 `cache-mode=cold`
- 同一 `MAX_MODEL_LEN` 分组运行，降低多次起停噪声
- 统一输出：
  - `TTFT P95`
  - `TPOT P95`
  - `prefill_tokens_per_second`
  - `success_rate`
  - `quality_warnings`

默认覆盖窗口：

- `16k`
- `24k`
- `32k`
- `48k`
- `64k`
- `96k`
- `128k`

输出路径：

- `data/reports/context/context_window_benchmark.json`
- `data/reports/context/context_window_benchmark.md`
- `data/reports/context/context_window_benchmark.svg`

### 9.3 当前判断

窗口极限试跑已经说明当前 TP2 可以跑到 `128k`，但是否适合作为线上默认窗口，需要看严谨 benchmark 的 `TTFT` 与 `prefill_tokens_per_second`，而不是只看单次成功。

因此当前的工程结论是：

- `run_context_extreme_trials.py` 负责能力上限
- `run_context_window_benchmark.py` 负责可用上限
- `router context tier` 负责把两类结论落到线上调度策略
