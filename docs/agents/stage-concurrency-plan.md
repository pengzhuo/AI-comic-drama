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
| 素材/道具 `asset_compiler` | 按 chunk map_reduce；`_compile_scenes` 逐场景块 | ✅ map_reduce 已接入；`_compile_scenes` 改**三遍式**（解析 → `map_bounded` 块级并发 → 按块顺序合并），块级并发 `ASSET_SCENE_BLOCK_CONCURRENCY` / `asset.json` 的 `scene_block_concurrency`（默认 4，设 1 = 逐块串行）。每块最多 2 次调用（补齐环境描述 + 派生场景分析），只为 LLM 并发、写库与建菜单仍在合并遍按块顺序 | 中：合并遍保序，故菜单/派生场景顺序不变；同一基础场景重复出现只补齐一次 | 已到位；`props` 侧（`_analyze_block_props`）仍受「上一块已选道具」顺序依赖，未并 |
| 视频提示词 `global_video_optimizer` | 逐 beat **审核** | ✅ `review_and_fix` 改 `map_bounded(stage="video_optimizer")` | — | 已到位 |
| 视频提示词 `global_optimize_video` | 逐 beat **优化（并发=1）** | ✅ 波前并发（默认 `concurrency=8` + `wave_size=40`，可热调参回退到 1）。基线 **22,981.9s / 1656 beats ≈ 6h23m（13.9s/beat）**，是全流水线最大热点；A/B 实测 8.27x → 投影 **≈46 分钟** | 中：波内相邻 beat 拿不到「上一 beat 的优化提示词」，锚点退化为上一波末尾（盲评与串行持平） | 已到位；`prev_prompt=none` 为实测逃生舱 |
| 系统声线 `system_voice_setup` | 逐角色**网关 TTS**合成参考声线 | ✅ 合成分流为 `map_bounded` 有界并发；旋钮 `SYSTEM_VOICE_TTS_CONCURRENCY` / `system_voice_setup.json` 的 `concurrency`（默认 4，设 1 = 逐角色串行）。基线实测 **549s / 10 次网关调用串行**（其中一次单请求 **5m26s**、一次 71s） | 低：只并发生成，声线选择仍串行 → `prepared`/分配与串行逐字一致（回归用例锁死）；同主声线加锁去重，任一角色彻底失败仍整集失败 | 已到位；剩余瓶颈是**单次网关时延**，与并发无关 |
| 首帧 `first_frames`（`selected_regen`） | 逐块**单飞**（1 块 9 张） | ✅ 固定块 + 多块并发（`FIRST_FRAMES_CONCURRENCY` 默认 3；块内 `NANOBANANA_REGEN_CONCURRENCY` 仍是 3）。基线实测 **25,321s / 136 块 ≈ 12.7h（≈191s/块，1656 beats）**——比 `global_optimize_video` 还大，而只有 3 路图真的在飞 | 低：块按 beat 号固定切开，块间不重叠；旧的滑动窗口一旦并发就会重复渲染同一批 beat | 已到位；并发=1 即退回旧单飞 |
| 身份 `identity_planner` | 逐 requirement 解析/生成形象；逐集规划 | ❌ 串行；共享 store 写入 + 顺序（`resolved_ids`） | 中高 | 需按序合并 + 保护 store 写 |
| 逐行剧本 `literal_script_writing` | **逐行主循环（并发=1）** | 逐行链路仍串行；`self._agent`/`self._valid_*`/`_*_section` 是实例级可变状态，Agent 的 `validation_context` 直接引用这些集合。✅ 路线 1（任务级按集并发）已上线；✅ 逐行主循环已加**分段断点续跑**（见「稳健性」） | 高 | 见下「专项」 |

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

**逐行剧本已落地**：`literal_script_writing` 逐行循环每 `SCRIPT_CHECKPOINT_EVERY`（默认 5）
行把「已生成 beats + 循环状态（sticky 身份、`safe_prompt_history`、当前场次块、降级行号）」
原子写到 `<项目输出目录>/runtime/script_writer/ep{N}.json`（目录解析：workflow 构造参数
`output_dir` → `store.output_dir` → `store.project_dir`；生产 `CogneeStore` 只有 `project_dir`）
（`tmp` + `os.replace`，写失败只记日志）。下一次 `run()` 在同一集上启动时：指纹一致才复用，
指纹不同/文件损坏/进度不自洽一律从零重算。

指纹（`_script_checkpoint_fingerprint`）覆盖所有会改变逐行输出的输入：`SCRIPT_CHECKPOINT_VERSION`
+ 原文 sha256 + 行数 + `episode.title` + 输出语言 + `audio_type_mode` + `visual_style`
+ `identity_default_map` + 场景菜单（`_valid_scene_ids`）+ 每个场次块的 header/地点/时间/人物/行数/
候选身份道具/菜单文本。改动逐行 prompt 或循环语义时必须把 `SCRIPT_CHECKPOINT_VERSION` +1。

复用门控只有「断点文件还在」这一条 —— 成功 `persist_narration_script` 后立即删档，所以用户主动
重生成（发生在上次成功之后）天然从零，不需要往 payload 里加 `force`。断点不落 DB，不参与任何
阶段间共享写入。失败/取消时最坏重算 `SCRIPT_CHECKPOINT_EVERY - 1` 行（因为落点是每 5 行一次，
不是异常路径兜底）。
