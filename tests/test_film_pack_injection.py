"""技能包注入：阶段映射、精简规范块、整份正文降级与总开关。"""

from __future__ import annotations

from pathlib import Path

import pytest

from novelvideo.skills import ai_film_process

#: 需要注入精简规范块（逐 beat/逐资产/画面提示词）的阶段。
CONDENSED_STAGES = (
    "character_portrait",
    "identity_image",
    "scene_reference_asset",
    "prop_reference_asset",
    "sketch_generation",
    "sketch_regen",
    "grid_regenerate",
    "selected_regen",
    "beat_video_prompt",
    "seedance2_prompt",
    "global_optimize_video",
    "episode_scene_planner",
    "episode_prop_planner",
    "narration_audio",
    "dialogue_audio",
    "freezone_audio_speech",
    "compose_episode",
    "compose_delivery",
    "canvas_workflow_plan",
)

#: 注入整份正文（项目级/归并级）的阶段。
FULL_STAGES = (
    "build_characters",
    "build_scenes",
    "build_props",
    "build_episodes",
    "content_rewriter",
)


def test_every_registered_stage_declares_an_injection_mode() -> None:
    for stage in ai_film_process.STAGE_PARTS:
        assert stage in ai_film_process.STAGE_INJECTION_MODE, stage
        assert ai_film_process.injection_mode(stage) in {"full", "condensed"}


def test_directive_files_are_bundled_and_bounded() -> None:
    assert set(ai_film_process.DIRECTIVE_FILES) == {"02", "03", "04", "05", "06"}
    for code in ai_film_process.DIRECTIVE_FILES:
        body = ai_film_process.load_directive(code)
        assert body.strip()
        assert len(body) <= ai_film_process.DIRECTIVE_MAX_CHARS


@pytest.mark.parametrize("stage", CONDENSED_STAGES)
def test_condensed_stages_render_a_bounded_directive(stage: str) -> None:
    block = ai_film_process.pack_directive(stage)
    assert "制作流程包规范" in block
    assert len(block) < ai_film_process.DIRECTIVE_MAX_CHARS + 200
    assert ai_film_process.pack_block(stage) == block


@pytest.mark.parametrize("stage", FULL_STAGES)
def test_full_stages_inject_the_whole_part_text(stage: str) -> None:
    block = ai_film_process.pack_block(stage)
    assert "制作流程包正文" in block
    # 整份正文远大于精简块，说明没有误降级。
    assert len(block) > 10_000


def test_full_stage_degrades_to_directive_when_the_whole_part_does_not_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 窗口放不下整份 02（约 28 KB → 约 28.7k token），但放得下精简块。
    monkeypatch.setenv("NEWAPI_TEXT_CONTEXT_TOKENS", "40000")
    block = ai_film_process.pack_block("build_characters")
    assert "制作流程包规范" in block
    assert "制作流程包正文" not in block


def test_section_window_ratio_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ai_film_process.section_window_ratio() > 0
    env = ai_film_process.PACK_SECTION_WINDOW_RATIO_ENV
    monkeypatch.setenv(env, "0.05")
    assert ai_film_process.section_window_ratio() == 0.05
    # 非法取值回落到默认值，不抛错。
    for bad in ("abc", "0", "2", "-1"):
        monkeypatch.setenv(env, bad)
        assert (
            ai_film_process.section_window_ratio()
            == ai_film_process.PACK_SECTION_MAX_WINDOW_RATIO
        )


def test_tiny_window_ratio_degrades_full_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    # 比例收紧到精简块放得下、整份正文放不下的区间。
    monkeypatch.setenv(ai_film_process.PACK_SECTION_WINDOW_RATIO_ENV, "0.05")
    block = ai_film_process.pack_block("build_characters")
    assert "制作流程包规范" in block
    assert "制作流程包正文" not in block


def test_condensed_block_is_skipped_when_the_window_is_tiny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """贡献者回归：注入绝不能把请求推到 MODEL_CONTEXT_BUDGET_EXCEEDED。

    小窗口下（如单测里的 4096）精简块也放不下，此时宁可整体不注入。
    """

    monkeypatch.setenv("NEWAPI_TEXT_CONTEXT_TOKENS", "4096")
    monkeypatch.setenv("NEWAPI_TEXT_OUTPUT_TOKENS", "512")
    for stage in ("character_portrait", "beat_video_prompt", "build_characters"):
        assert ai_film_process.pack_directive(stage) == ""
        assert ai_film_process.pack_block(stage) == ""
        assert ai_film_process.apply_directive(stage, "原始提示词") == "原始提示词"
    assert not ai_film_process.fits_window("正文" * 800)


def test_identity_planner_appearance_scope_prefers_style_part() -> None:
    assert ai_film_process.stage_scope_parts("identity_planner", "appearance") == ("04",)
    block = ai_film_process.pack_block("identity_planner", "appearance")
    assert "风格与标准资产" in block


def test_kill_switch_disables_all_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ai_film_process.PACK_INJECTION_ENV, "0")
    for stage in ("character_portrait", "build_characters", "beat_video_prompt"):
        assert ai_film_process.pack_directive(stage) == ""
        assert ai_film_process.pack_block(stage) == ""


def test_unregistered_stage_injects_nothing() -> None:
    assert ai_film_process.injection_mode("tts") == "condensed"
    for stage in ("tts", "canvas_timeline_export", "video_render_export"):
        assert ai_film_process.pack_directive(stage) == ""
        assert ai_film_process.pack_block(stage) == ""


def test_apply_directive_prepends_and_keeps_text() -> None:
    body = "原始画面提示词"
    wrapped = ai_film_process.apply_directive("character_portrait", body)
    assert wrapped.endswith(body)
    assert "制作流程包规范" in wrapped
    # 未登记阶段原样返回。
    assert ai_film_process.apply_directive("tts", body) == body


def test_append_pack_appends_section_to_system_prompt() -> None:
    base = "系统提示"
    wrapped = ai_film_process.append_pack("build_characters", base)
    assert wrapped.startswith(base)
    assert "制作流程包正文" in wrapped
    assert ai_film_process.append_pack("tts", base) == base


# ── 真实构建器接线 ────────────────────────────────────────────────────────


def test_prop_reference_prompt_carries_directive() -> None:
    from novelvideo.generators.nanobanana_prop import build_prop_reference_prompt

    prompt = build_prop_reference_prompt("一把青铜短剑")
    assert "制作流程包规范" in prompt
    assert "3-PANEL" in prompt


def test_unified_grid_builder_carries_directive() -> None:
    from novelvideo.generators.prompt_builder import (
        UnifiedPromptBuilder,
        create_prompt_context,
    )
    from novelvideo.generators.prompt_components import PromptMode

    for mode in (PromptMode.SKETCH, PromptMode.RENDER):
        ctx = create_prompt_context(mode=mode, beats=[], rows=1, cols=1)
        assert UnifiedPromptBuilder(ctx).build().startswith("【制作流程包规范")


def test_canvas_generation_prompts_carry_directive() -> None:
    # ``freezone.route_helpers`` 与 ``api.routes.freezone`` 之间存在既有循环导入；
    # 先导入 API 包可让 route_helpers 正常完成初始化。
    import novelvideo.api  # noqa: F401

    from novelvideo.freezone.presets import _beat_sketch_prompt
    from novelvideo.freezone.route_helpers import build_scene_360_prompt
    from novelvideo.freezone.video_node import build_freezone_omni_video_prompt

    sketch = _beat_sketch_prompt(
        {"beat_data": {"visual_description": "某人推门而入"}}, {}
    )
    assert "制作流程包规范" in sketch
    assert "制作流程包规范" in build_scene_360_prompt("小区")
    assert "制作流程包规范" in build_freezone_omni_video_prompt(user_prompt="镜头推进")


def test_kill_switch_reaches_the_builders(monkeypatch: pytest.MonkeyPatch) -> None:
    from novelvideo.generators.nanobanana_prop import build_prop_reference_prompt
    from novelvideo.generators.prompt_builder import (
        UnifiedPromptBuilder,
        create_prompt_context,
    )
    from novelvideo.generators.prompt_components import PromptMode

    monkeypatch.setenv(ai_film_process.PACK_INJECTION_ENV, "0")
    ctx = create_prompt_context(mode=PromptMode.SKETCH, beats=[], rows=1, cols=1)
    assert "制作流程包规范" not in UnifiedPromptBuilder(ctx).build()
    assert "制作流程包规范" not in build_prop_reference_prompt("一把青铜短剑")


def _fake_agent_capture(monkeypatch: pytest.MonkeyPatch):
    """让 Agent 构造可离线断言：捕获 system_prompt 且不触网。"""

    import pydantic_ai

    import novelvideo.config as config

    captured: dict = {}

    class FakeAgent:
        def __init__(self, model, **kwargs):
            captured["model"] = model
            captured.update(kwargs)

    monkeypatch.setattr(config, "get_newapi_text_pydantic_model", lambda *a, **k: "m")
    monkeypatch.setattr(config, "get_superpower_pydantic_model", lambda *a, **k: "m")
    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    return captured, FakeAgent


def test_video_agents_append_the_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    import novelvideo.agents.global_video_optimizer as optimizer_mod
    import novelvideo.agents.keyframe_prompt_builder as keyframe_mod
    import novelvideo.seedance2_i2v.prompt as seedance2_mod

    captured, fake = _fake_agent_capture(monkeypatch)
    for module in (optimizer_mod, keyframe_mod, seedance2_mod):
        monkeypatch.setattr(module, "Agent", fake, raising=False)

    keyframe_mod.create_keyframe_prompt_builder_agent("zh")
    assert "制作流程包规范" in captured["system_prompt"]

    captured.clear()
    optimizer_mod.create_global_video_optimizer_agent("zh")
    assert "制作流程包规范" in captured["system_prompt"]

    captured.clear()
    seedance2_mod.create_seedance2_prompt_composer_agent("zh")
    assert "制作流程包规范" in captured["system_prompt"]


def test_identity_detector_agent_stays_free_of_the_pack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """纯视觉读取环节不注入：无收益，且小窗口下会直接顶穿预算。"""

    import novelvideo.agents.global_video_optimizer as optimizer_mod

    captured, fake = _fake_agent_capture(monkeypatch)
    monkeypatch.setattr(optimizer_mod, "Agent", fake, raising=False)

    optimizer_mod._create_identity_detector_agent()
    assert "制作流程包" not in captured["system_prompt"]
    assert captured["name"] == "角色颜色识别"


# ── 第 06 份：配音与合成环节 ──────────────────────────────────────────────


def test_part_06_covers_sound_and_delivery() -> None:
    body = ai_film_process.load_directive("06")
    assert "配音与表演" in body
    assert "声音衔接" in body
    assert "成片验收" in body


def test_audio_stages_map_to_part_06() -> None:
    for stage in (
        "narration_audio",
        "dialogue_audio",
        "freezone_audio_speech",
        "compose_episode",
        "compose_delivery",
        "canvas_workflow_plan",
    ):
        assert ai_film_process.STAGE_PARTS[stage] == ("06",)
        assert ai_film_process.injection_mode(stage) == "condensed"


def test_append_audio_directive_keeps_the_base_emotion_prompt_first() -> None:
    base = "以第三人称旁白视角，用客观冷静的解说语气朗读"
    wrapped = ai_film_process.append_audio_directive("narration_audio", base)
    assert wrapped.startswith(base)
    assert "制作流程包规范" in wrapped
    # 空语气提示也要能拿到规范；未登记阶段原样返回。
    assert ai_film_process.append_audio_directive("narration_audio", "")
    assert ai_film_process.append_audio_directive("tts", base) == base


def test_audio_directive_drops_the_delivery_section() -> None:
    """成片验收一节讲镜头衔接，与 TTS 语气无关，不进配音提示。"""

    block = ai_film_process.audio_directive("narration_audio")
    assert "制作流程包规范" in block
    assert "## 配音与表演" in block
    assert "## 声音衔接" in block
    assert "## 成片验收" not in block
    assert len(block) < len(ai_film_process.pack_directive("narration_audio"))


def test_append_audio_directive_respects_the_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ai_film_process.PACK_INJECTION_ENV, "0")
    assert ai_film_process.append_audio_directive("narration_audio", "原样") == "原样"


def _fake_tts_capture() -> tuple[dict, type]:
    captured: dict = {}

    class FakeTTS:
        def __init__(self, *args, **kwargs):
            captured["init"] = kwargs

        async def generate(self, **kwargs):
            captured.update(kwargs)
            from novelvideo.generators.indextts2_fal import TTSResult

            return TTSResult(success=True, duration_seconds=1.5)

    return captured, FakeTTS


@pytest.mark.asyncio
async def test_narration_audio_carries_part_06_without_touching_the_script(
    tmp_path: Path,
) -> None:
    from novelvideo.seedance2_i2v import voice_clone

    captured, fake = _fake_tts_capture()
    reference = tmp_path / "narrator.mp3"
    reference.write_bytes(b"ref")

    result = await voice_clone.generate_seedance2_narration_audio(
        beat={"narration_segment": "他推门而入，屋里空无一人。"},
        episode=1,
        beat_num=1,
        project_dir=tmp_path,
        narrator_audio_path=reference,
        generator=fake(),
        audio_url_builder=lambda _path: "data:audio/mpeg;base64,AA",
    )

    assert result is not None and result.success
    assert "制作流程包规范" in captured["emotion_prompt"]
    # 要朗读的台词原样送出，注入只落在语气提示上。
    assert captured["prompt"] == "他推门而入，屋里空无一人。"


@pytest.mark.asyncio
async def test_dialogue_audio_carries_part_06_without_touching_the_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novelvideo.seedance2_i2v import voice_clone

    captured, fake = _fake_tts_capture()
    reference = tmp_path / "voice.mp3"
    reference.write_bytes(b"ref")

    async def fake_resolve(_beat, _store):
        return reference, "character"

    monkeypatch.setattr(voice_clone, "resolve_dialogue_reference_audio", fake_resolve)
    store = type("Store", (), {"project_dir": tmp_path})()

    result = await voice_clone.generate_seedance2_dialogue_audio(
        beat={"speaker": "阿离", "narration_segment": "她后退半步：「别过来。」"},
        episode=1,
        beat_num=1,
        store=store,
        generator=fake(),
        audio_url_builder=lambda _path: "data:audio/mpeg;base64,AA",
    )

    assert result is not None and result.success
    assert "制作流程包规范" in captured["emotion_prompt"]
    assert captured["prompt"] == "别过来。"


@pytest.mark.asyncio
async def test_canvas_audio_node_carries_part_06(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novelvideo.freezone import audio_node

    captured, fake = _fake_tts_capture()
    monkeypatch.setattr(audio_node, "IndexTTS2FalClient", fake)
    monkeypatch.setattr(
        audio_node, "build_reference_audio_url", lambda _path: "data:audio/mpeg;base64,AA"
    )

    async def fake_resolve(**_kwargs):
        return "default", type("Voice", (), {"audio_path": tmp_path / "v.mp3", "source": "x", "sha256": ""})()

    monkeypatch.setattr(audio_node, "resolve_speech_voice", fake_resolve)

    result = await audio_node.generate_freezone_audio_speech(
        username="alice",
        project="demo",
        project_dir=tmp_path,
        job_id="job-1",
        text="别过来。",
        emotion_prompt="低语",
    )

    assert "制作流程包规范" in captured["emotion_prompt"]
    assert captured["emotion_prompt"].startswith("低语")
    assert captured["prompt"] == "别过来。"
    assert result.audio_path


@pytest.mark.asyncio
async def test_canvas_preset_speech_metadata_stays_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """零配置预设模式走的是平台原生 TTS，不注入流程包（保持原样）。"""

    from novelvideo.freezone import audio_node

    captured: dict = {}

    async def fake_write(**kwargs):
        captured.update(kwargs)
        target = Path(kwargs["output_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"mp3")

    monkeypatch.setattr(audio_node, "_write_newapi_audio_speech", fake_write)

    await audio_node.generate_freezone_audio_speech(
        username="alice",
        project="demo",
        project_dir=tmp_path,
        job_id="job-2",
        text="别过来。",
        emotion_prompt="原始语气",
        speech_mode="preset",
        preset_model="not-the-default-model",
    )

    assert captured["metadata"]["emotion_prompt"] == "原始语气"


def test_workflow_planning_instructions_carry_part_06() -> None:
    from novelvideo.freezone.workflow_planning import (
        WORKFLOW_PLANNING_INSTRUCTIONS,
        workflow_planning_instructions,
    )

    body = workflow_planning_instructions()
    assert body.startswith(WORKFLOW_PLANNING_INSTRUCTIONS)
    assert "制作流程包规范" in body


def test_workflow_planning_instructions_respect_the_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novelvideo.freezone.workflow_planning import (
        WORKFLOW_PLANNING_INSTRUCTIONS,
        workflow_planning_instructions,
    )

    monkeypatch.setenv(ai_film_process.PACK_INJECTION_ENV, "0")
    assert workflow_planning_instructions() == WORKFLOW_PLANNING_INSTRUCTIONS
