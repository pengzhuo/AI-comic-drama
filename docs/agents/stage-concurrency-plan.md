# 全流水线 LLM 阶段并发/提速计划

把 `script_process` 上验证有效的一套（独立并发 + 保序归并 + 热调参 + 全局闸门 +
逐调用观测）铺到相邻阶段时的**度量口径、约束与待办**。实测数据见
[`script-process-tuning.md`](script-process-tuning.md)，共享基建见
[`stage-runtime.md`](stage-runtime.md)。

## 度量口径

- **阶段墙钟 ≈ 总工作量 ÷ 有效并发**。判断「还有没有空间」先看
  `summarize_spans(output_dir)` 的每阶段 `span_seconds`，再看该阶段的
  `duration_sum / span`（= 平均在飞）。接近并发上限 = 该阶段已吃满，继续加并发才有收益。
- 每个阶段任务在唯一派发点（`task_backend/run_core.py`）自动落一条 `task_span`，
  无需各阶段自己接线；`script_process` 另有逐调用 `script_process_call`。

## 三层并发约束（自上而下）

| 层 | 默认 | 位置 | 作用 |
| --- | --- | --- | --- |
| 全局 LLM 在飞闸门 | 12 | `shared/global_llm_gate.py`（`LLM_GLOBAL_CONCURRENCY`） | 限制**全进程**同时在飞的 LLM 调用，是所有阶段的总预算 |
| 任务 lane 并发 | default=6 / 每项目=3 / video=3 | `task_backend/limits.py` | CE inline 线程池槽位；**同一项目同一时间最多 3 个阶段任务** |
| 阶段内并发 | 各阶段热文件 | `runtime/<stage>.json` | 单次任务内部的并发（如 `script_process`=12） |

含义：
- 把某阶段任务内并发提到 12 **不增加**网关总负载（仍受全局闸门 12 封顶）；
  再往上必须同步抬高全局闸门，会波及所有阶段。
- 「任务级按集并发」受**每项目 lane=3** 限制，最多 3 路；跨项目才可能吃满 6。
  任务级并发上线前先确认内存（lane 是进程内，视频类曾因 video=4 把内存冲到 9.8G）。

## 现状与待办

| 阶段 | 串行热点 | 现状 | 风险 | 计划 |
| --- | --- | --- | --- | --- |
| `script_process` | 逐段理解 + 归并 | ✅ 并发 12（`map_bounded` 等价），热调参齐全 | — | 已到位；仅剩「模型/prompt 降单次时延」 |
| `structured` 抽取 | 按 chunk map | ✅ `map_bounded(stage="structured")` | — | 已接入 |
| `cognee` 事件提取 | 逐章提取 | ✅ `map_bounded(stage="cognee")` | — | 已接入（`ai_events` 模式） |
| `cognee` 归一化/富化 | 按场景 map + 批内逐场景回退 | ✅ 批并发已接入；批内回退仍串行（fallback 路径） | 低 | 有需要再把回退并入外层批 |
| 解说改写 `content_rewriter` | 按 chunk map_reduce | ✅ `stage="rewriter"` | — | 已接入热调参 |
| 素材/道具 `asset_compiler` | 按 chunk map_reduce；`_compile_scenes` 逐场景块 | ✅ map_reduce 已接入；逐场景块**未并发**（共享 `pending_scene_map`/`scene_menu` 顺序状态 + store 写入） | 中 | 收集结果后按序合并方可并发 |
| 视频提示词 `global_video_optimizer` | 逐 beat 审核 | ✅ `review_and_fix` 改 `map_bounded(stage="video_optimizer")`；`optimize` 链式（`prev_prompt`）不可并发 | — | 已到位 |
| 身份 `identity_planner` | 逐 requirement 解析/生成形象；逐集规划 | ❌ 串行；共享 store 写入 + 顺序（`resolved_ids`） | 中高 | 需按序合并 + 保护 store 写 |
| 逐行剧本 `literal_script_writing` | **逐行主循环（并发=1）** | ❌ 串行；`self._agent`/`self._valid_*`/`_*_section` 是实例级可变状态，Agent 的 `validation_context` 直接引用这些集合 | 高 | 见下「专项」 |

## 专项：`literal_script_writing` 并发（最大热点）

三条路线，按风险从低到高：

1. **任务级按集并发**（较低风险）：`script_writer` runner 本就**按集一个任务**、
   每集独立新建 `CogneeStore` + workflow → 集间天然隔离。障碍在编排层：
   `pipeline_auto.py` 每次只推进一个 `current_episode`（`compute_pipeline_status`
   返回单个 `next_step`）。要改成「一次派发多集」需重写该步的 `next_step` 判定
   与 `attempted` 去重口径；且受**每项目 lane=3** 限制，最多 3×。
2. **逐行块并发**（中等风险，需质量 A/B）：把逐行主循环按 K 行成块，块内**丢开**
   `prev_beat_anchor`/`safe_prompt_history` 的块内依赖（只用块前上下文），K 路并发。
   说得通的提速，但块内相邻 beat 的连贯性下降 → 必须做成**默认关闭**的热旋钮并 A/B。
3. **按 run 隔离状态重构**（高风险）：把 `self._agent`/`self._valid_*`/
   `_identity_section` 等全部改为 run 局部、Agent 的 `validation_context` 改为传入
   可变引用容器；之后 `run_all_episodes` 才能安全 `map_bounded`。集内仍严格串行 →
   质量不变，但重构面大（约 1600 行、多处 helper 读实例状态），需完整回归。

推荐顺序：1（先拿到 3× 且质量不变）→ 2（A/B 换更大提速）→ 3（彻底解耦）。

## 稳健性：分段断点续跑 / 结果缓存

`script_process` 逐段落盘、失败可续。障碍是 `force` 语义：用户流程总是
`force=true`（A/B 要重算），而续跑要复用已完成段。可行口径：
**仅当上一次同 key（source sha + 分块 + 注入 + 版本常量）的运行未完成时复用**，
成功完成后清缓存；`force` 只表示「不要整段短路」，不表示「丢弃段缓存」。
需一个 `_SEGMENT_CACHE_VERSION` 常量在改动分段 prompt/模型时手动 +1，否则会复用旧 prompt 结果。
