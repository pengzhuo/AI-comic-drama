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
| `asset`（`_compile_scenes` 块级：补齐环境描述 + 派生场景分析） | `stage_int("asset", "ASSET_SCENE_BLOCK_CONCURRENCY", "scene_block_concurrency", 4)` = 在飞的**场景块数** | `map_bounded` 默认 | `asset.json` → `scene_block_concurrency` | — |
| `video_optimizer`（逐 beat 视频提示词审核） | `map_bounded(stage="video_optimizer")` | `map_bounded` 默认 | `video_optimizer.json` | — |
| `global_optimize_video`（逐 beat 首帧提示词优化） | `stage_int("global_optimize_video", "GLOBAL_OPTIMIZE_CONCURRENCY", …, 8)` + `wave_size`（默认 40） | `map_bounded` 默认 | `global_optimize_video.json` | `global_optimize_video_call` |
| `first_frames`（`selected_regen` 首帧渲染） | `stage_int("first_frames", "FIRST_FRAMES_CONCURRENCY", "concurrency", 3)` = 在飞的**块数** | —（图片请求不经 LLM 闸门） | `first_frames.json` | `selected_regen`（任务级 `task_span`） |
| `system_voice_setup`（角色参考声线合成） | `stage_int("system_voice_setup", "SYSTEM_VOICE_TTS_CONCURRENCY", "concurrency", 4)` = 在飞的**角色数** | —（音频请求不经 LLM 闸门） | `system_voice_setup.json` | `system_voice_setup`（任务级 `task_span`） |

`system_voice_setup` 只把**合成**并发：声线的选择（含同项目内 `claimed` 去重）
仍按角色顺序串行，`prepared` / 声线分配与逐角色串行逐字一致（`concurrency=1` 即回退旧行为）。
合成池只有个位数条预置音色，池耗尽后多个角色会共用同一条音色 → 按**主声线加锁**，
后到的角色直接命中 `assets/system_voices/<provider>__<voice>.mp3` 缓存，
不重复打网关（串行版靠文件缓存天然去重，并发下不加锁会放大成 N 次重复请求）。
任一角色彻底合成失败仍整集失败（与串行同口径，不静默降级）。
实测（`护花铃_2` episode 1）：基线 **549s / 10 次网关调用串行**，
其中一次单请求 **5m26s**、一次 71s——瓶颈是单次网关时延而不是派发。

**群杂兜底**（同一份热调参文件）：dialogue beat 的 `speaker` 对不上任何角色时
（「路人」「伙计」「众弟子」这类脚本里的临时说话人），默认**硬失败**并列出
「未找到说话身份对应角色：X」让用户补角色；把 `unmatched_speaker_voice`
（env `SYSTEM_VOICE_UNMATCHED_VOICE`）设成一条网关 preset 音色 id 后，
这些说话人统一用那条「群杂音色」配音，不再阻塞整集。群杂音色交给同一条合成通道
（同一并发池、同一 `assets/system_voices` 缓存、同主声线加锁），但它不是一个角色，
不会写进 `prepared`；解析口径落在 `resolve_dialogue_reference_audio`，
所以音频计划、配音生成、`system_voice_setup` 前置检查三处一致。
实测（`护花铃_2` episode 1）：1123 条对白 beat 里 51 条（35 个说话身份）对不上角色。

**这个开关有项目级口径**：`project_config.json` 的同名键
`unmatched_speaker_voice` 优先于部署级（env / 热调参文件），三态语义是
「缺键 = 跟随部署级」「显式 `""`/`false`/`null` = 该项目关掉兜底（回到硬失败）」
「非空字符串 = 该项目的群杂音色」。因此想「只让某个项目用群杂」的做法是：
部署级留空（缺省），只在该项目的 `project_config.json` 里写音色 id；
反向（全局开、个别项目关）就直接在该项目里写 `""`。读取路径统一走
`store.state_dir/project_config.json`（`unmatched_speaker_voice_for_store`），
下游解析同样按项目口径取同一个音色 id，避免「准备阶段用项目音色、配音阶段用部署音色」
这种半生效。

另有一个**项目级开关**：`script_process_digest`（布尔）
控制「剧本处理理解账」是否注入逐行剧本 prompt，部署级口径是
`SCRIPT_PROCESS_DIGEST`（env）> `runtime/script_writer.json` 的 `process_digest`
> 默认**开**。理解账来自 `script_process` 阶段的 `script_process/result.json`，
注入时会在系统提示里追加理解账 / 人物 / 未决项，并明确「仍只标注当前这一行」。
注入改变了逐行 prompt，因此 `SCRIPT_CHECKPOINT_VERSION` 随之上调
（旧断点自然失效）。

`first_frames` 这一层是**任务级（块级）并发**，与阶段内并发叠加：每块 9 个 beat、
块内 `NANOBANANA_REGEN_CONCURRENCY`（默认 3）路图片请求，块数由本表这一行控制，
所以真实在飞图片请求 ≈ `块数 × 块内并发`（默认 3 × 3 = 9）。块按 beat 号
**固定切开**（`AUTO_FIRST_FRAME_CHUNK=9`）而不是「缺帧的前 9 个」滑动窗口——固定块
才允许块间并行（滑动窗口一旦并发就会把同一批 beat 渲染两遍）。签名带缺集
（`dispatch:selected_regen:{ep}:{首}-{尾}:{缺哪几个}`），所以「整块缺帧」只派一次，
跑完留下缺口才允许再派一次补缺。单镜块会带 `beat_num`，让被上游审核挑掉的 beat
能走「改写提示词 → 降级成草图顶替」的既有闭环，而不是把整条 run 停成等人确认。
整段基线实测 **25,321s / 136 块 ≈ 12.7h（≈191s/块、≈20.7s/beat，1656 beats）**；
`concurrency=1` 即退回旧单飞语义。

实测（`护花铃_2`，生产 run 中途热补）：
- 基线单飞 **2.95 帧/分**（≈20.7s/beat）；
- 块并发 3 → 稳定 3 块在飞、**≈6 帧/分（2.0x）**；
- 块并发 5 → 稳定 5 块在飞、**≈13.5 帧/分（4.6x）**，单块耗时仍 ≈140s（未被上游拉长，
  说明瓶颈一直是我们自己的串行派发），内存 764MiB → 1.16GiB，无 429/5xx
  （唯一的 Grid 失败是被审核挑掉的 beat 与重启中断）。
- 生产热调参文件取 `{"concurrency": 5}`：CE 的 default lane 全局 6 槽，留 1 个给别的阶段。

已知折中：块内的图是**逐张落盘**的，所以一块还在跑时它的缺口会缩小，若此时签名被
清（失败重试会清全部 `dispatch:*`）就会对那几个 beat 再派一次——实测浪费 ≈0.5%
的图片调用，换来 4.6x，暂不值得为它加「在飞块覆盖区间」的簿记。

`global_optimize_video` 是**波前（wave）并发**：唯一的跨 beat 依赖是
`prev_prompt`（上一 beat 已优化提示词的前 150 字），所以按波切分，波内并行、
波间串行，波内每个 beat 以「上一波最后一个成功提示词」为锚点。两个独立旋钮：
`concurrency` = 在飞上限（吞吐），`wave_size` = 波宽（锚点粒度 + 栅栏次数），
`wave_size` 取 `max(concurrency, 配置值)`；`prev_prompt=none` 可整段不发锚点。
实测（80 beats，`护花铃_2`）：1 路 1065.7s → 8 路/波宽 8 217.7s（4.9x）→
**8 路/波宽 40 128.8s（8.27x）**，盲评质量与串行持平；12 路无额外收益。
`concurrency=1` 时逐字节等于旧串行。

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
