# Benchmark 测试矩阵

本文档定义当前项目的系统 benchmark 矩阵。原则是先固定 workload 和报告格式，再逐步扩展输入长度、输出长度、并发数和部署拓扑，保证每次优化都有可对比基线。

## 1. 当前可执行矩阵

| ID | 场景 | 层级 | Workload / 输入 | vLLM | 拓扑 | 并发 | 迭代 | 核心指标 | 默认阈值 | 命令 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BM-E2E-001 | `pipeline_no_llm` | 端到端 | `treg_20260402` pipeline | 否 | 本地进程 | 1 | 1 warmup + 3 measured | success rate、duration P50/P90/P95/P99、stdev、CV、pipeline 内部耗时 | measured iterations >= 3，success rate >= 1.0，duration P95 <= 10s | `python scripts/run_system_benchmark.py` |
| BM-REG-001 | `regression_only` | 回归评测 | `treg_20260402` regression | 否 | 本地进程 | 1 | 1 warmup + 3 measured | success rate、checks、failed checks、duration P50/P90/P95/P99、stdev、CV | measured iterations >= 3，success rate >= 1.0，duration P95 <= 5s | `python scripts/run_system_benchmark.py` |
| BM-LLM-001 | `vllm_chat_stream` | 推理侧 | 短 JSON 输出 prompt，`max_tokens=128` | 是 | 双卡 TP=2，AWQ | 1 | 1 warmup + 3 measured | TTFT、TPOT、tokens/s、completion tokens、stream chunks、finish reason | measured iterations >= 3，success rate >= 1.0，TTFT P95 <= 10s，TPOT P95 <= 1s，tokens/s P50 >= 1 | `python scripts/run_system_benchmark.py --enable-scenario vllm_chat_stream` |

默认配置文件：

```text
configs/benchmark/treg_20260402_system.json
```

默认无 vLLM 报告：

```text
data/reports/benchmark/treg_20260402_system_benchmark.json
```

带 vLLM 全量报告：

```text
data/reports/benchmark/treg_20260402_system_benchmark_vllm.json
```

## 2. vLLM 启动与执行

当前可用启动脚本：

```bash
scripts/start_vllm_qwen_coder_awq.sh
```

该脚本默认使用：

| 参数 | 当前值 |
| --- | --- |
| 模型目录 | `/home/xdu/huggingface/cyankiwi-Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit` |
| 服务名 | `qwen-audit-resolver` |
| API 地址 | `http://127.0.0.1:8008/v1` |
| GPU | `CUDA_VISIBLE_DEVICES=0,1` |
| 张量并行 | `TENSOR_PARALLEL_SIZE=2` |
| 最大上下文 | `MAX_MODEL_LEN=4096` |
| 显存比例 | `GPU_MEMORY_UTILIZATION=0.80` |
| 执行模式 | `ENFORCE_EAGER=1` |

启动后先确认模型接口：

```bash
curl -sS http://127.0.0.1:8008/v1/models
```

运行带 vLLM 的全量 benchmark：

```bash
python scripts/run_system_benchmark.py \
  --enable-scenario vllm_chat_stream \
  --output-path data/reports/benchmark/treg_20260402_system_benchmark_vllm.json
```

## 3. 推理扩展矩阵

推理扩展矩阵用于后续优化 vLLM serving，不放入默认 smoke benchmark，但已经实现为可执行脚本和配置。完整矩阵入口：

```bash
python scripts/run_complete_benchmark.py
```

该入口会自动执行：

1. 无 vLLM 的 pipeline / regression baseline；
2. 双卡 TP=2、`MAX_MODEL_LEN=16384` 下的输入长度、输出长度和并发矩阵；
3. 单卡单实例拓扑 smoke benchmark；
4. 双卡双实例拓扑 benchmark。

TP2 扩展矩阵单独配置：

```text
configs/benchmark/treg_20260402_vllm_expansion.json
```

### 3.1 输入长度矩阵

| ID | 输入长度 | 当前状态 | 说明 |
| --- | --- | --- | --- |
| BM-LLM-IN-1K | 1k tokens | 已实现 | `vllm_input_1k`，适合短审计冲突和模板选择 |
| BM-LLM-IN-4K | 4k tokens | 已实现 | `vllm_input_4k`，覆盖当前常规上下文上限 |
| BM-LLM-IN-8K | 8k tokens | 已实现 | `vllm_input_8k`，由完整 benchmark 以 `MAX_MODEL_LEN=16384` 启动 TP2 后执行 |
| BM-LLM-IN-16K | 16k tokens | 已实现 | `vllm_input_16k`，使用 16k bucket 合成 prompt，保留输出余量 |

### 3.2 输出长度矩阵

| ID | `max_tokens` | 用途 | 主要观察指标 |
| --- | --- | --- | --- |
| BM-LLM-OUT-128 | 128 | JSON 决策、短修复建议 | 已实现为 `vllm_output_128`，观察 TTFT、TPOT、成功率 |
| BM-LLM-OUT-256 | 256 | 结构化解释、候选 rerank 理由 | 已实现为 `vllm_output_256`，观察 TPOT、tokens/s |
| BM-LLM-OUT-512 | 512 | 长修复建议或多节点摘要 | 已实现为 `vllm_output_512`，观察 TPOT、显存、尾延迟 |

### 3.3 并发矩阵

| ID | 并发数 | 当前状态 | 目的 |
| --- | --- | --- | --- |
| BM-LLM-CONC-1 | 1 | 已实现 | `vllm_concurrency_1`，单请求基线，排除排队影响 |
| BM-LLM-CONC-4 | 4 | 已实现 | `vllm_concurrency_4`，验证连续批处理收益 |
| BM-LLM-CONC-8 | 8 | 已实现 | `vllm_concurrency_8`，观察 P95、queue wait 和吞吐拐点 |
| BM-LLM-CONC-16 | 16 | 已实现 | `vllm_concurrency_16`，压测容量上限 |

### 3.4 部署拓扑矩阵

| ID | 拓扑 | 当前状态 | 适用问题 |
| --- | --- | --- | --- |
| BM-TOP-1GPU | 单卡单实例 | 已实现 | `scripts/run_complete_benchmark.py` 自动用 GPU0、TP=1、`MAX_MODEL_LEN=2048` 启动并测试 |
| BM-TOP-TP2 | 双卡 TP=2 | 已实现 | 完整 benchmark 自动用 GPU0,1、TP=2、`MAX_MODEL_LEN=16384` 启动并测试 |
| BM-TOP-2INST | 双卡双实例 | 已实现 | 完整 benchmark 自动启动 GPU0:8008 和 GPU1:8009 两个单卡实例并测试 round-robin 并发 |

### 3.5 上下文窗口严谨评测

上下文窗口极限试跑已经证明当前 TP2 服务可运行到 `128k`，但极限试跑更偏向容量验证，不适合作为延迟回归口径。为此新增严谨版窗口 benchmark：

```bash
python scripts/run_context_window_benchmark.py
```

设计原则：

- 每个窗口档位固定执行 `1 warmup + 3 measured`；
- 使用 `cache-mode=cold`，避免 prefix cache 把长上下文 prefill 测成 warm cache；
- 同一 `MAX_MODEL_LEN` 下按组启动 vLLM，减少多次起停的噪声；
- 统一记录 `TTFT P95`、`TPOT P95`、`prefill_tokens_per_second`、`success_rate`；
- 产出 JSON、Markdown、SVG 三份报告，便于回归比较。

默认窗口组：

| 组别 | `MAX_MODEL_LEN` | 目标输入长度 |
| --- | ---: | ---: |
| CW-32K | 32768 | 16k、24k、32k |
| CW-48K | 49152 | 48k |
| CW-64K | 65536 | 64k |
| CW-96K | 98304 | 96k |
| CW-128K | 131072 | 128k |

默认报告路径：

```text
data/reports/context/context_window_benchmark.json
data/reports/context/context_window_benchmark.md
data/reports/context/context_window_benchmark.svg
```

## 4. 结果记录字段

每次 benchmark 报告至少保留：

- `workload`、`scenario.name`、`command`、`iterations`；
- `success_rate`、`passed`、`failed`、`timeout`；
- `started_at`、`finished_at`、`duration_seconds`；
- `duration_min_seconds`、`duration_mean_seconds`、`duration_p50_seconds`、`duration_p90_seconds`、`duration_p95_seconds`、`duration_p99_seconds`、`duration_max_seconds`；
- `duration_stdev_seconds` 和 `duration_cv_seconds`，用于观察同一场景稳定性；
- vLLM 场景的 `stdout_ttft_seconds_p95/p99`、`stdout_tpot_seconds_p95/p99`、`stdout_tokens_per_second_p50`、`stdout_completion_tokens_p50/p95`；
- 配置 `metrics_urls` 后，会额外记录 `observability.metrics_before`、`observability.metrics_after`、`observability.metric_delta`，并把 delta 扁平化为 `vllm_delta_*` 指标；
- 每条 threshold check 的 `actual`、`expected` 和 `passed`；
- `quality_warnings`，用于提示 measured iterations 过少、缺少 warmup、失败或超时；
- 原始 `samples` 中的 stdout/stderr，便于定位服务不可用、沙箱网络限制或模型输出异常。

严格口径：

- 固定 benchmark 至少执行 1 次 warmup 和 3 次 measured iteration；
- P95 / P99 使用 nearest-rank 口径，样本数较少时更偏向保守尾延迟；
- 输入长度矩阵使用 `cache-mode=cold`，在 prompt 开头加入每次请求不同的 salt，避免 prefix cache 把长上下文 prefill 测成 warm cache；
- 输出长度矩阵使用 `cache-mode=warm`，并用 `min_tokens` 强制生成接近 128 / 256 / 512 token，用于隔离 decode 性能；
- 并发和拓扑矩阵使用 `cache-mode=cold`，更接近多请求不共享完全相同 prompt 的服务场景。
- 窗口 benchmark 单独统计 `prefill_tokens_per_second = prompt_tokens / TTFT`，显式区分 prefill 与 decode；
- 窗口极限试跑与严谨 benchmark 分离维护：前者回答“能否跑通”，后者回答“该窗口下延迟是否还能接受”。

## 5. 当前本地基线

最近一次完整 benchmark：

| Section | 结果 | 覆盖范围 |
| --- | --- | --- |
| `system_no_vllm` | 通过 | 2 scenarios，pipeline + regression |
| `vllm_tp2_expansion` | 通过 | 11 scenarios，输入长度 + 输出长度 + 并发 |
| `topology_tp2` | 通过 | 双卡 TP=2 拓扑 |
| `topology_single_gpu` | 通过 | 单卡单实例拓扑 |
| `topology_dual_instance` | 通过 | 双卡双实例拓扑 |

报告路径：

```text
data/reports/benchmark/treg_20260402_complete_benchmark.json
data/reports/benchmark/treg_20260402_vllm_expansion_benchmark.json
data/reports/benchmark/treg_20260402_system_benchmark.json
```

本次报告已包含 vLLM `/metrics` 观测字段。所有 vLLM 场景均写入 `observability`，并生成 `vllm_delta_*` 服务端指标。

系统 baseline：

| 场景 | 结果 | 关键指标 |
| --- | --- | --- |
| `pipeline_no_llm` | 通过 | success rate 1.0，duration P95 1.299020s，P99 1.299020s，CV 0.003908 |
| `regression_only` | 通过 | success rate 1.0，33 checks，0 failed，duration P95 0.165848s，P99 0.165848s，CV 0.024810 |

TP2 输入长度 baseline：

| 场景 | prompt tokens P95 | TTFT P95 | TPOT P95 | tokens/s P50 |
| --- | ---: | ---: | ---: | ---: |
| `vllm_input_1k` | 1094 | 0.234403s | 0.018521s | 26.371990 |
| `vllm_input_4k` | 3910 | 0.758722s | 0.019603s | 17.827540 |
| `vllm_input_8k` | 7750 | 1.576052s | 0.019231s | 9.472786 |
| `vllm_input_16k` | 15432 | 3.629854s | 0.020049s | 13.224870 |

TP2 输出长度 baseline：

| 场景 | prompt tokens P95 | TTFT P95 | TPOT P95 | tokens/s P50 |
| --- | ---: | ---: | ---: | ---: |
| `vllm_output_128` | 1088 | 0.043033s | 0.020621s | 48.269657 |
| `vllm_output_256` | 1088 | 0.044397s | 0.020814s | 47.877438 |
| `vllm_output_512` | 1088 | 0.046651s | 0.020678s | 48.698220 |

TP2 并发 baseline：

| 场景 | requests/s P50 | total tokens/s P50 |
| --- | ---: | ---: |
| `vllm_concurrency_1` | 0.418243 | 44.751977 |
| `vllm_concurrency_4` | 1.235932 | 91.112004 |
| `vllm_concurrency_8` | 1.901357 | 158.525620 |
| `vllm_concurrency_16` | 2.782221 | 191.238663 |

拓扑 baseline：

| 拓扑 | requests/s P50 | total tokens/s P50 |
| --- | ---: | ---: |
| TP2 | 2.758024 | 119.805333 |
| 1GPU | 2.039206 | 164.585960 |
| 2INST | 3.901887 | 277.079085 |

备注：如果在默认沙箱内运行本机 HTTP benchmark，可能出现 `Operation not permitted`。带 vLLM 的 benchmark 需要允许访问 `127.0.0.1:8008`。

## 6. 当前上下文分层建议

基于已完成的拓扑 benchmark、tuning sweep 和窗口极限试跑，当前 serving 建议为：

- `short_audit`、`rerank`：优先走 `2INST`，重点追求 requests/s 和尾延迟；
- `long_context`、`repair`：走 TP2；
- `<=4k`：`short` tier；
- `4k-32k`：`standard` tier，作为在线长上下文默认档；
- `32k-64k`：`extended` tier，只给重任务路径；
- `64k-128k`：`extreme` tier，更适合离线分析或低频任务。

当前 `router` 已支持按 prompt token 长度做 tier 分类和 endpoint 过滤，后续可以把不同 tier 绑定到不同端口或不同启动参数的 vLLM 实例。
