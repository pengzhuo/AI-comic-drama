# 阶段运行期共享基建（stage runtime）

全流水线有多处长稿 LLM 热点：`script_process` 逐段理解 + 归并、`structured_v1`
抽取、`cognee` 归一化、逐行脚本标注、逐 beat 视频优化……它们此前各写一套并发/
重试/观测，导致「有的串行、有的各建 semaphore、有的没有全局上限」。本页记录抽出来
的公共件与约定，后续每个阶段提速都复用这套语义，而不是各自发明。

逐阶段热点清单、三层并发约束与推进计划见
[`stage-concurrency-plan.md`](stage-concurrency-plan.md)。

代码：

| 模块 | 职责 |
| --- | --- |
| `src/novelvideo/shared/stage_runtime.py` | 热调参文件读写、值收正、`StageLimiter`、`run_bounded`、`record_call` |
| `src/novelvideo/shared/global_llm_gate.py` | 进程级 LLM 在飞调用闸门（`LLM_GLOBAL_CONCURRENCY`） |
| `src/novelvideo/utils/bounded_concurrency.py` | `map_bounded`（已默认过全局闸门） |
| `src/novelvideo/pipeline_timing.py` | `record_event` 追加上限轮转 + `summarize_stage` |

## 热调参文件（每阶段一份）

```
${NOVELVIDEO_RUNTIME_DIR:-<DATA_ROOT>/runtime}/<stage>.json
```

**优先级：阶段专用环境变量（若设置）> 该文件 > 代码常量。**
文件缺失/损坏一律当空字典，绝不抛错；改文件无需重启（下一次构造阶段即生效）。
`script_process` 另可用 `SCRIPT_PROCESS_TUNING_FILE` 指定绝对路径，其余阶段沿用
`stage_int` / `stage_bool` / `stage_choice` 的 `<stage>.json` 约定。

| 函数 | 用途 |
| --- | --- |
| `runtime_tuning_path(stage, explicit_env=...)` | 解析文件路径 |
| `load_runtime_tuning(stage, explicit_env=...)` | 读字典（容错） |
| `stage_int(stage, env_name, key, default)` | 取正整数：env > 文件 > 默认 |
| `stage_bool(stage, env_name, key, default)` | 取布尔 |
| `stage_choice(stage, env_name, key, options, default)` | 取枚举 |

## 并发闸门

- :class:`StageLimiter` —— **阶段级**闸门：正常按 `limit` 并发放行；连续 `streak`
  次瞬时失败（429/5xx/连接失败）后进入 `cooldown_seconds` 秒冷却，冷却期间有效并发
  降到 `cooldown_limit`（默认 `1`，与旧 `script_process` 行为一致；设成
  `max(1, limit//2)` 即「过载减半」的温和降级）。`acquire()` 返回本次是否持有冷却
  闸门，`release(flag)` 同步释放（取消路径上不再 await）。
- :func:`run_bounded` —— 保序的有界映射；`cancel_siblings=True` 时首个致命错误会取消
  其余在飞调用再上抛（适合「一段坏了整条白跑」的阶段）。默认还会过全局闸门。
- :data:`global_llm_gate.global_llm_slot` —— **进程级**闸门：同一事件循环共享一个
  `asyncio.Semaphore`（跨循环用 `WeakKeyDictionary` 分桶）。上限
  `LLM_GLOBAL_CONCURRENCY` 默认 12，`0` 关闭。**必须**在拿到阶段级闸门之后、真正发
  请求之前进入，否则会占着全局名额等阶段名额造成饥饿。

`map_bounded` 已默认 `gate=True`，所以结构化抽取 / cognee 无需改动就吃到全局上限；
`script_process` 在分段/归并的 `one()` 内显式 `async with global_llm_slot()`。

## 已接入阶段

| 阶段 | 并发来源 | 全局闸门 | 热调参文件 | 逐调用观测 |
| --- | --- | --- | --- | --- |
| `script_process` | `SCRIPT_PROCESS_CONCURRENCY`（默认 12） | `_map_segments` / `_map_digest_batches` | `script_process.json` | `script_process_call` |
| `structured`（structured_v1 抽取/形象设定） | `map_bounded(stage="structured")` | `map_bounded` 默认 | `structured.json` | — |
| `cognee`（场景归一化/富化、逐章事件提取） | `map_bounded(stage="cognee")` | `map_bounded` 默认 | `cognee.json` | — |
| `rewriter`（解说改写 map_reduce） | `map_bounded(stage="rewriter")` | `map_bounded` 默认 | `rewriter.json` | — |
| `asset`（素材/道具对账 map_reduce） | `map_bounded(stage="asset")` | `map_bounded` 默认 | `asset.json` | — |
| `video_optimizer`（逐 beat 视频提示词审核） | `map_bounded(stage="video_optimizer")` | `map_bounded` 默认 | `video_optimizer.json` | — |

`map_bounded(..., stage="x")` 仅在 `limit is None` 时读 `runtime/x.json` 的
`concurrency` 键；显式传 `limit` 一律优先。env 复用 `STRUCTURED_LLM_CONCURRENCY`。

## 观测

- 每次调用写 `runtime/pipeline_timing.jsonl` 的 `<stage>_call` 事件，字段含
  `phase / ok / duration_seconds` + 阶段自有字段；`record_call(output_dir, stage,
  phase, started=..., ok=..., **fields)` 是统一入口。
- **每个阶段任务**在唯一派发点（`task_backend/run_core.py`）写一条 `task_span`
  事件（`stage` + `started` + `duration_seconds` + `ok`），无需各阶段自己接线即可
  看到全链路墙钟。`summarize_spans(output_dir, since=...)` 按阶段汇总
  调用数 / 总耗时 / `span_seconds`（该阶段把流水线堵住的时长）/ 失败数——**先看
  `span_seconds` 排序，下一刀切最大的那个**。
- 文件按 `PIPELINE_TIMING_MAX_BYTES`（默认 2 MB，0=关闭）**轮转**：超过后只保留最新
  一半，避免跨多次重跑无界增长。
- `summarize_stage(output_dir, stage, since=...)` 汇总某阶段的调用数 / 失败数 / 各
  phase 计数 / span / 耗时中位数，`since` 可只看某一跑的 epoch 秒。

## 约定（新增阶段提速时照做）

1. 不要把 `for` 循环里的模型调用留给串行——接 `run_bounded` 或 `map_bounded`。
2. 阶段级并发别只加环境变量：同时提供 `<stage>.json` 热调参键，优先级 env > 文件 > 常量。
3. 归并/合并类输入先**瘦身**（只带下游需要的事实，别把逐段完整 JSON 灌进去）。
4. 每阶段都发 `<stage>_call` 事件，跑完用 `summarize_stage` 出数，再决定下一步。
5. 保持串行语义：并发=1 时必须与旧路径逐字节一致（旧归并的「子批插队」深度优先
   逻辑在 `script_process` 里仍有单独分支与测试锁定）。
