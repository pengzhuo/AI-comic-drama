# 剧集节奏：每集≈60 秒 + 镜头最少

目标：**任何剧本的每一集都控制在约 1 分钟，且镜头数尽量少。** 默认全局生效，
每个项目可覆盖。

## 两条独立的控制

1. **分集**（每集时长）——`novelvideo/pacing.py::build_episode_segments`
   + `SQLiteStore.build_episodes_by_duration`（`sqlite_store.py`）。
2. **镜头合并**（镜头数）——`novelvideo/pacing.py::compact_beats_for_shots`，
   在 `workflows/literal_script_writing.py` 生成 `NarrationScript` 前执行。

两处都用同一套「字数 ÷ TTS 语速」估算时长，刻度一致。

## 分集规则

输入是章节（`ChapterDetector`）。对每个章节按估算时长 `d`：

| `d` | 处理 |
| --- | --- |
| `d ≤ merge_floor`（45s） | 与相邻章节合并到 ≈正片预算 |
| `merge_floor < d ≤ split_threshold`（45–90s） | **压缩到正片预算，不拆集** |
| `d > split_threshold`（90s） | **按自然子边界拆成多个 ≈预算 的集** |

正片预算 = `episode_target_seconds − 片头3s − 片尾2s`（默认 60−5=55s）。
压缩保留开头若干句 + **最后一句**（结尾钩子）。

## 镜头合并规则

逐行剧本默认「一行一镜」。合并时：

- 仅合并**音频上下文一致**的相邻 beat（同 `audio_type` / `speaker` /
  `speaker_kind` / `time_of_day` / `scene`），保证配音不串；
- 累积预估时长达到单镜目标（`shot_target_seconds`，默认 15s）就断开；
- 受每集镜头上限 `ceil(正片预算 / 单镜目标)` 约束（默认 4 镜/集）；
- 合并后旁白顺序拼接，画面取该组第一镜的 `visual_description`（长镜头锚点）。

> 对白密集的剧本受「对话轮次」下限约束，最少镜头数无法无限压低——否则只能把
> 对白改成旁白叙述。

## 参数

全局默认（环境变量，见 `pacing.resolve_pacing_settings`）：

- `EPISODE_TARGET_SECONDS`（默认 60）
- `EPISODE_MERGE_FLOOR_SECONDS`（默认 45）
- `EPISODE_SPLIT_THRESHOLD_SECONDS`（默认 90）
- `SHOT_TARGET_SECONDS`（默认 15）

项目覆盖（`project_config.json`，同名键）：`episode_planning_mode`（默认
`duration`）、`episode_target_seconds`、`episode_merge_floor_seconds`、
`episode_split_threshold_seconds`、`shot_target_seconds`。通过
`PATCH /api/v1/projects/{project}`（`ProjectUpdate`）写入。

分集接口 `POST /api/v1/projects/{project}/episodes/plan`：
`planning_mode` 留空时用项目配置（默认 `duration`）；可传 `target_seconds` 覆盖目标。

## 覆盖 / 回退

- `episode_planning_mode="chapters"` → 回到旧的「一章一集」。
- `shot_target_seconds` 调大 → 镜头更少、单镜更长（上限受视频模型时长约束，
  如 seedance-2.0 为 4–15s，见 `config.NEWAPI_VIDEO_DURATION_BOUNDS`）。
- 直接构造 `LiteralScriptWritingWorkflow` 而不传 `pacing_settings` → 不做镜头合并
  （保持旧的「一行一镜」，仅单测/库调用如此；生产 runner 一定会传）。

## 文件

- `src/novelvideo/pacing.py` —— 全部确定性逻辑 + 单测（`tests/test_pacing.py`）。
- `src/novelvideo/sqlite_store.py::build_episodes_by_duration`
- `src/novelvideo/cognee/store.py::build_episodes_by_duration`（委托）
- `src/novelvideo/task_backend/runners/graph_build.py`（`planning_mode="duration"`）
- `src/novelvideo/pipeline_auto.py`（默认派发 duration）
- `src/novelvideo/workflows/literal_script_writing.py`（镜头合并）
- `src/novelvideo/task_backend/runners/script.py`（解析并传入节奏参数）
