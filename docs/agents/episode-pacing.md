# 剧集节奏与镜头语言

目标分三层：

1. **分集**：每集 ≈ 目标时长（默认 60s 正片预算，见下）。
2. **切分**：每镜 **3–8s**（默认目标 6s，再按所选视频模型的时长边界夹紧），
   镜头数由内容（台词/动作的切点）决定，**不再「越少越好」**。
3. **镜头语言**：每个 beat 定「景别 / 机位 / 运镜」，并保证**相邻两镜必须有差**
   （正打↔反打、全景↔特写）。

> 口径变迁：旧口径是「取视频模型单镜上限、镜头越少越好」（2.5→30s 一镜）+
> 相邻 beat **首尾帧逐帧咬合**。那等于「不允许切点」，成片像会动的 PPT。短剧靠切点
> 叙事，所以两处都反过来了：单镜压到 3–8s，咬合**默认关**。

## 三条独立的控制

1. **分集**（每集时长）——`novelvideo/pacing.py::build_episode_segments`
   + `SQLiteStore.build_episodes_by_duration`（`sqlite_store.py`）。
2. **镜头切分**（镜头数）——`novelvideo/pacing.py::compact_beats_for_shots` /
   `split_long_beats_for_shots`，在 `workflows/literal_script_writing.py` 生成
   `NarrationScript` 前执行。
3. **镜头语言**（每镜怎么拍）——`novelvideo/shot_language.py::plan_shot_language`，
   在 `task_backend/runners/video.py::_run_global_optimize_video_async`（`global_optimize_video`
   阶段）里算完整集，再逐 beat 注入提示词并落库。

分集与切分都用同一套「字数 ÷ TTS 语速」估算时长，刻度一致。

## 分集规则

输入是章节（`ChapterDetector`）。对每个章节按估算时长 `d`：

| `d` | 处理 |
| --- | --- |
| `d ≤ merge_floor`（45s） | 与相邻章节合并到 ≈正片预算 |
| `merge_floor < d ≤ split_threshold`（45–90s） | **压缩到正片预算，不拆集** |
| `d > split_threshold`（90s） | **按自然子边界拆成多个 ≈预算 的集** |

正片预算 = `episode_target_seconds − 片头3s − 片尾2s`（默认 60−5=55s）。
压缩保留开头若干句 + **最后一句**（结尾钩子）。

## 镜头切分规则

逐行剧本默认「一行一镜」。切分时：

- 只合并相邻、且 `audio_type` / `speaker_kind` / `time_of_day` / `scene` 一致的 beat；
  **对白允许跨说话人合并**（一镜多声线，见下），其它音轨仍按 `speaker` 区分，避免旁白与角色串音；
- 累积预估时长达到单镜目标（`shot_target_seconds`，默认 6s）就断开，**硬上限
  `shot_max_seconds`（默认 8s）**；
- 单条 beat 自身超过硬上限时，交给 `split_long_beats_for_shots` 按句拆
  （`max_seconds = shot_target_seconds`，`SHOT_SPLIT_SAFETY=0.85` 留出拼接静音余量）；
- 一条 beat 内不同说话人 ≤ **3**（`pacing.MAX_DIALOGUE_SPEAKERS_PER_SHOT`，
  与 Seedance2 单次请求最多 3 段参考音频的硬限制一致，超了就另起一镜）；
- 合并后旁白顺序拼接；跨说话人对白按 ``说话人：台词`` 逐行拼接，画面取该组第一镜的
  `visual_description`（长镜头锚点）。

## 镜头语言（分镜规划步）

`shot_language.py::plan_shot_language(beats)` 是**纯规则、零模型调用、可复现**的：
「相邻两镜必须有差」是跨镜头约束，逐 beat 调 LLM 看不到邻居，最容易给出「每镜都是中景」。

规则（从上到下，越靠前优先级越高）：

- **建立镜头**：全片第 1 镜、以及每换场景后的第 1 镜 → 全景平视（交代空间）；
- **空镜/过场**（`audio_type == "silence"`）→ 远景平视 + 摇；
- **对白** → 按场景内对白轮次 **正打（平视）/ 反打（过肩）交替**；台词很短
  （≤ `CLOSEUP_LINE_MAX_CHARS`）或带情绪标记、或每 3 轮 → 特写；
- **旁白** → 中景为主，情绪标记给近景 + 推，每 4 镜插一个近景；
- **收尾**：最后一镜推向近景/特写（**优先级低于**建立镜头与空镜，单镜集不会被压成特写）；
- **硬约束**：相邻两镜的（景别, 机位）不得完全一致，撞了就就近调一档景别。

产出落在 beat 的 `shot_size` / `shot_angle` / `shot_move` 三列
（`beats` 表 + `NovelVisualBeat`），下游三处消费：

- **草图构图**（决定成片景别的**唯一**入口）：草图是画面的构图源头，渲染阶段被明确
  禁止改机位，所以镜头语言必须写进草图提示词才真正落地。`plan_shot_language` 是确定性
  纯函数，因此在**草图之前**已经算过两次并盖到 beats 上：剧本阶段
  （`literal_script_writing` 落库）+ 草图任务下发
  （`generation.py::enqueue_sketch_grid_tasks` 调 `apply_shot_language_plan`，覆盖老数据）。
  SKETCH 模式下 `prompt_strategies._planned_shot_language_clause` 把
  「镜头语言：中近景、过肩反打、固定机位」追加到单格描述；没有规划时提示词一字不变。
- **视频提示词**：`global_video_optimizer.optimize_single_beat(shot_language=...)` 注入
  `## Shot Language (LOCKED by the storyboard planner)`，要求模型不得改回「中景平视」；
  同时 `seedance2_i2v/prompt.py::_beat_shot_language_fragment` 把它写进提示词草稿；
- **场景反向视角**：`shot_angle == "过肩"`（反打）时，`seedance2_i2v/assets.py`
  用场景的 `reverse_master.png` 当参考图（没有则退回正向 `master.png`）。正反打是两个
  相反的机位，而不是把同一张背景左右翻转。

## 首帧与参考资产（P2）

首帧是视频的第 0 帧，它的分辨率/质量会直接成为整条成片的清晰度上限：

- **尺寸**：出图网格用 2K/4K（`REGEN_MODE_CONFIGS`）；单格再生模式
  （`SINGLE_CELL_RENDER_MODE_KEY` / `SINGLE_CELL_RENDER_MODE_BY_ASPECT`）统一升到
  **2K**（约 1152×2048，高于 720p 成片），可用 `DRAMACLAW_SINGLE_CELL_IMAGE_SIZE`
  回退成 1K。单格再生要与网格出图的兄弟镜保持同一分辨率，否则重出的那一镜会明显发糊。
- **质量**：`OPENAI_IMAGE_QUALITY` 默认 **high**（首帧）；草图仍是
  `OPENAI_SKETCH_IMAGE_QUALITY=low`——草图只是给模型看构图，最终画面由 render 决定。
- **角色身份图**（4 面板 reference sheet：全脸 + 正面 + 45° 三分 + 背面）：
  `nanobanana_character.CHARACTER_SHEET_IMAGE_SIZE` 默认 **2K**（env
  `DRAMACLAW_CHARACTER_SHEET_IMAGE_SIZE`）。这张图是每镜身份锚点的来源，16:9 的 1K
  四面板均分下来每张脸只有 ~256px，锁不住脸。
- **参考资产**（`seedance2_i2v/assets.py`）：按「首/尾帧 → 身份图 → 服装 → 场景 →
  道具 → 用户图」顺序附加，并**硬裁到 9 张**（上游单请求 ≤9 张参考图，超了直接报错）；
  被裁掉的置为未发送并在资产面板的 note 里说明。服装图槽位是
  `assets/characters/<角色>/identities/<身份>_costume.png`。

## 一镜多声线

跨说话人合并出的 beat，文本按 ``说话人：台词`` 逐行标注，下游按行还原：

- 参考声线 / 字幕：`seedance2_i2v/spoken_dialogue.py` 的 `parse_seedance2_spoken_lines`
  / `unique_seedance2_dialogue_speakers`（每条说话人各要一段参考声线）；
- 逐行配音：`seedance2_i2v/voice_clone.py::generate_seedance2_dialogue_audio` 检测到
  多个说话人时，逐行用各自声线合成，再用 ffmpeg 拼成该 beat 的单条 `beat_NN.mp3`
  （`dialogue_lines` / `dialogue_voice_keys`；`_concat_audio_files`）。

> 前提：视频模型能在单镜内原生生成多说话人音轨（Seedance2.5 原生出声）。独立配音轨
> 仍用于计时/预览与时长决策。

## 首尾帧逐帧咬合（**默认关**）

相邻 beat 首尾帧严格咬合（Beat N 的尾帧 = Beat N+1 的首帧）能让镜头之间画面绝对连续，
但**等于不允许有切点**——每个新镜头都必须从上一镜的落点继续，正是「长镜头 PPT」的来源。
短剧靠正打/反打/特写来回切，每镜换机位、换景别，所以**默认关**。

需要长镜头连贯运动的特殊镜头仍可单独打开：

- 位置：`seedance2_i2v/pipeline.py::prepare_seedance2_generation_inputs`。
- 为什么不用厂商首尾帧参数：上游禁止把 `last_frame_image` 和参考媒体（参考声线也算）
  放进同一次请求（`conflicting_media_inputs`），所以「厂商首尾帧」与「原生多声线」在同一
  镜里只能二选一。
- 滚动做法：每个 beat 都置 `return_last_frame=true`，厂商返回的尾帧落到
  `videos/beats/epNNN/returned_last_frames/beat_NN.<ext>`；下一 beat 直接拿**上一 beat 的
  成片尾帧**当严格首帧（`assets.py` 的 `first_frame_override`），模式仍是 `first_frame`
  ——于是同一镜既逐帧接住画面，又能带原生参考声线。
- 尾帧来源：`return_last_frame=true` 只是「请厂商回尾帧」；`newapi_seedance-*` 走的
  seedance 网关**只回视频 URL、不回尾帧**，所以生成器在厂商没给尾帧时用 ffmpeg 从成片
  本地抽最后一帧落到同一路径（`VideoGeneratorBase._extract_local_last_frame`），让滚动
  咬合在任何后端都成立；只有本地抽帧也失败才退回渲染首帧。
- 开关：`FIRST_LAST_FRAME_CHAINING`（env）或 `project_config.json` 的
  `first_last_frame_chaining`（**缺省=关**）。
- 回退：首镜、上一镜尾帧尚未落盘、上一镜失败 / 没开尾帧 → 退回本项目渲染的首帧
  （`frames/.../beat_NN.png`）；Prompt 面板显式选了多参模式（`config.mode` 已不是
  `first_frame`）时不覆盖；`video_mode == "keyframe"` 仍走厂商首尾帧。
- 出镜顺序：`pipeline_auto.py::_dispatch_single_videos` 在咬合开启时，等上一 beat 出片
  （或上一 beat 已停止）才派发本 beat，避免抢跑接不上；一条坏 beat 不会把整条流水线卡死。

## 参数

全局默认（环境变量，见 `pacing.resolve_pacing_settings`）：

- `EPISODE_TARGET_SECONDS`（默认 60）
- `EPISODE_MERGE_FLOOR_SECONDS`（默认 45）
- `EPISODE_SPLIT_THRESHOLD_SECONDS`（默认 90）
- `SHOT_TARGET_SECONDS`（默认 **6**，单镜目标）
- `SHOT_MIN_SECONDS` / `SHOT_MAX_SECONDS`（默认 **3 / 8**，短剧镜头语言窗口；
  两者都会再与所选模型的时长边界取交集，例如 seedance-2.5 的 4–30s → 实际 4–8s）
- `FIRST_LAST_FRAME_CHAINING`（默认**关**）

项目覆盖（`project_config.json`，同名键）：`episode_planning_mode`（默认
`duration`）、`episode_target_seconds`、`episode_merge_floor_seconds`、
`episode_split_threshold_seconds`、`shot_target_seconds`、`shot_min_seconds`、
`shot_max_seconds`、`first_last_frame_chaining`。
通过 `PATCH /api/v1/projects/{project}`（`ProjectUpdate`）写入。

分集接口 `POST /api/v1/projects/{project}/episodes/plan`：
`planning_mode` 留空时用项目配置（默认 `duration`）；可传 `target_seconds` 覆盖目标。

## 覆盖 / 回退

- `episode_planning_mode="chapters"` → 回到旧的「一章一集」。
- `shot_target_seconds` 调大 → 镜头更少、单镜更长（上限受视频模型时长约束，
  如 seedance-2.0 为 4–15s，见 `config.NEWAPI_VIDEO_DURATION_BOUNDS`）。
- 直接构造 `LiteralScriptWritingWorkflow` 而不传 `pacing_settings` → 不做镜头切分
  （保持旧的「一行一镜」，仅单测/库调用如此；生产 runner 一定会传）。
- beat 的三个镜头语言字段为空（未规划）→ 提示词不注入、下游按原逻辑兜底。

## 文件

- `src/novelvideo/pacing.py` —— 分集 + 镜头切分的确定性逻辑（`tests/test_pacing.py`）。
- `src/novelvideo/shot_language.py` —— 镜头语言规则（`tests/test_shot_language.py`）。
- `src/novelvideo/sqlite_store.py::build_episodes_by_duration`
- `src/novelvideo/cognee/store.py::build_episodes_by_duration`（委托）
- `src/novelvideo/task_backend/runners/graph_build.py`（`planning_mode="duration"`）
- `src/novelvideo/pipeline_auto.py`（默认派发 duration）
- `src/novelvideo/workflows/literal_script_writing.py`（镜头切分）
- `src/novelvideo/task_backend/runners/script.py`（解析并传入节奏参数）
- `src/novelvideo/task_backend/runners/video.py`（`global_optimize_video` 里跑分镜规划）
- `src/novelvideo/agents/global_video_optimizer.py`（注入镜头语言到提示词）
