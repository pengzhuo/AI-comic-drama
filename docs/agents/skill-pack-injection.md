# Skill pack injection (制作流程包注入)

How the bundled **通用AI影视制作流程包** reaches the model calls that build
image/video prompts, 配音语气提示 and the agent instructions around 成片合成.
Read this before changing prompt assembly or the pack.

## Where the pack lives

`src/novelvideo/skills/ai_film_process/` — six read-only Chinese documents
(`01_master_control.txt` … `06_handoff_sound_and_delivery.txt`, 29–52 KB each)
plus a loader in `__init__.py`. Bodies are content, not code: edit them as
text, never import them.

- `PART_FILES` / `PART_TITLES` — 份号 → file / title.
- `STAGE_PARTS` — stage → 份号 (which parts that stage reads).
- `STAGE_SCOPE_PARTS` — stage + call scope → 份号 (e.g. `script_process`
  `segment` gets only 02, `digest` gets only 01).
- `STAGE_INJECTION_MODE` — stage → `full` | `condensed` (see below).
- `DIRECTIVE_FILES` — 份号 → condensed block under `directives/`.

## Two injection forms

Whole parts are 29–52 KB. Image templates must never receive them.

| Form | Used by | Helper |
| --- | --- | --- |
| **full** — whole part text | stages that run a few times per project/episode (`build_characters`, `build_scenes`, `build_props`, `build_episodes`, `identity_planner`, `content_rewriter`, `script_process`) | `pack_block(stage, scope, model_env=...)` / `append_pack(stage, prompt, model_env=...)` |
| **condensed** — hand-written block distilled from the part, ≤ `DIRECTIVE_MAX_CHARS` (1200) | per-beat / per-asset calls and every image/video prompt (`character_portrait`, `identity_image`, `scene_reference_asset`, `prop_reference_asset`, `sketch_*`, `selected_regen`, `grid_regenerate`, `beat_video_prompt`, `seedance2_prompt`, `global_optimize_video`, `episode_scene_planner`, `episode_prop_planner`) | `pack_directive(stage, scope)` / `apply_directive(stage, text)` |
| **condensed, appended** — same 06 block after the caller's own voice/style text | TTS 语气提示 (`narration_audio`, `dialogue_audio`, `freezone_audio_speech`) | `append_audio_directive(stage, emotion_prompt)` |

Layering written into the mapping: **01 总控 is only injected at merge/总控
scope** (`script_process` `digest`), never into per-shot or per-asset calls.

## Safety rules (do not break these)

1. **Never raise, never truncate the user's text.** Every helper returns `""`
   when it cannot inject; callers keep working.
2. **Never push a request into `MODEL_CONTEXT_BUDGET_EXCEEDED`.** That error is
   fatal in the auto coordinator (it does not consume the 3-attempt retry
   budget). Both forms are therefore capped by `fits_window()`: the injected text
   must fit inside `section_window_ratio()` of the configured window (default
   0.25), otherwise injection is skipped entirely and a warning is logged. This
   is why the surface is a *small* fixed set of stages — a stage whose window is
   tight simply loses the pack instead of hard-failing.
3. **Injection never changes models, the gateway, or generation params.** It only
   adds text.

## Switches (`.env`)

| Env | Default | Effect |
| --- | --- | --- |
| `DIRECTOR_PACK_INJECTION` | on | `0`/`false`/`no`/`off` disables **all** pack injection |
| `DIRECTOR_PACK_SECTION_WINDOW_RATIO` | `0.25` | max share of the text window one whole part may take (0–1) |
| `NEWAPI_TEXT_CONTEXT_TOKENS` / `MODEL_CONTEXT_WINDOWS_JSON` | `131072` | window used by the budget check |

## Wiring sites

Backend (mainline):

- `generators/prompt_builder.py` — `UnifiedPromptBuilder.build()` prepends the
  condensed block for **every** sketch/render grid prompt. This single point
  also covers the canvas twins (`freezone/presets.py`, `api/routes/freezone.py`)
  because they call the same builder.
- `generators/nanobanana_character.py` (`_build_character_prompt`,
  `_build_identity_locked_prompt`), `generators/nanobanana_prop.py`
  (`build_prop_reference_prompt`), `generators/scene_reference_images.py`
  (`build_scene_reference_prompt`).
- `agents/global_video_optimizer.py` (optimizer + reviewer + identity detector),
  `agents/keyframe_prompt_builder.py`, `seedance2_i2v/prompt.py`.
- `agents/identity_planner.py`, `agents/content_rewriter.py`,
  `agents/episode_planner.py`, `agents/asset_compiler.py`,
  `structured_extraction.py`, `cognee/pipeline.py` — LLM planning/抽取 stages.

Canvas extras: `freezone/presets.py` `_beat_sketch_prompt`,
`freezone/route_helpers.py` (`build_scene_360_prompt`, `build_multi_view_prompt`),
`freezone/video_node.py` `build_freezone_omni_video_prompt`.

配音 (06, condensed appended to the TTS `emotion_prompt` only — the spoken
`prompt` is never touched):

- `seedance2_i2v/voice_clone.py` — `_with_film_pack_emotion("narration_audio" |
  "dialogue_audio", ...)` for the two leaf generators (`generate_seedance2_narration_audio`,
  `generate_seedance2_dialogue_audio`); `…_for_voice` delegates to them.
- `freezone/audio_node.py` `generate_freezone_audio_speech` (clone mode).
- Not injected: `audio/system_voice_setup.py` (fixed sample sentence for voice
  cloning) and the零配置 preset path (`_write_newapi_audio_speech`), which sends
  the user's `emotion_prompt` to the platform TTS verbatim.

合成 06 (`compose_episode`, `compose_delivery`, `canvas_workflow_plan`): 主线
`task_backend/runners/video.py:run_compose_episode` and 画布
`freezone/jobs.py:run_freezone_video_compose` are pure ffmpeg timeline assembly —
they build **no** prompt for any model, and a `videoComposeNode` carries only a
fixed caption. The 06 rules therefore ride the agent instructions around compose:

- `chat/director_auto.py` `_film_pack_delivery_block()` — appended to the durable
  auto run's 成片交付 turn (`_agent_continue(..., final_delivery=True)`).
- `freezone/workflow_planning.py` `workflow_planning_instructions()` — the canvas
  plan-authoring instructions, used by `chat/service.py` (freezone_canvas
  developer instructions) and the workflow skill package's `agent_instruction`
  (`freezone/agent_workflows/catalog.py`), so canvas 配音/合成 nodes are planned
  under the same rules.

Deliberately **not** injected: the literal script writer (it already receives the
02 digest), pure editing tools
(outpaint/erase/upscale/relight/redraw/template-edit) where storyboard rules
would fight the edit instruction, and the AI identity detector
(`_create_identity_detector_agent`): it is a pure vision read (image + colour
map → which markers appear), produces no prompt at all, and injecting a few KB
of storyboard norms into its tight window used to hard-fail the beat with
`MODEL_CONTEXT_BUDGET_EXCEEDED`.

## Tests

`tests/test_film_pack_injection.py` locks the mapping, the directive length cap,
the kill switch, the budget-degradation path, and that the representative
builders (prop prompt, grid builder, canvas sketch/360/video, video LLM agents)
actually carry the block, plus the 06 surfaces (TTS emotion prompts keep the
spoken text untouched, canvas planning instructions, preset TTS metadata).
`tests/test_director_auto.py` covers the 成片交付 turn.
`tests/test_script_processing_workflow.py` keeps the original `script_process`
scope behaviour.
