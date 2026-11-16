"""整集「声音层」（配乐/环境音/音效）的设计与生成单测。"""

import asyncio
import json
from pathlib import Path

from novelvideo.audio import episode_audio_design as design


def _beat(number, scene, text="", time_of_day="白天"):
    return {
        "beat_number": number,
        "scene_ref": {"scene_id": scene},
        "narration_segment": text,
        "visual_description": "",
        "time_of_day": time_of_day,
    }


def _timeline(*items):
    return [(beat, start, end) for beat, start, end in items]


def test_extract_sfx_names_handles_marker_forms():
    text = "他冲了出去。【加游戏音效：First Blood】\n[音效：爆头、脚步]"
    names = design.extract_sfx_names(text)
    assert names == ["First Blood", "爆头", "脚步"]


def test_extract_sfx_names_dedupes():
    assert design.extract_sfx_names("【音效：剑鸣】…【音效：剑鸣】") == ["剑鸣"]


def test_design_audio_cues_builds_bgm_bed_and_sfx():
    timeline = _timeline(
        (_beat(1, "大殿", "他冲了出去。【音效：剑鸣】"), 0.0, 5.0),
        (_beat(2, "大殿"), 5.0, 9.0),
        (_beat(3, "后山"), 9.0, 14.0),
    )
    cues = design.design_audio_cues(timeline=timeline, style_hint="古装权谋")

    bgm = [c for c in cues if c.kind == "bgm"]
    assert len(bgm) == 1
    assert (bgm[0].start_seconds, bgm[0].end_seconds) == (0.0, 14.0)
    assert "古装权谋" in bgm[0].prompt

    ambience = [c for c in cues if c.kind == "ambience"]
    # 相邻同场景合成一段，跨场景再起一段。
    assert [(c.start_seconds, c.end_seconds) for c in ambience] == [(0.0, 9.0), (9.0, 14.0)]
    assert "大殿" in ambience[0].prompt

    sfx = [c for c in cues if c.kind == "sfx"]
    assert len(sfx) == 1
    assert sfx[0].start_seconds == 0.0
    assert "剑鸣" in sfx[0].prompt


def test_design_audio_cues_caps_ambience_and_sfx():
    beats = []
    cursor = 0.0
    for index in range(12):
        beats.append((_beat(index + 1, f"场景{index}", "【音效：脚步】"), cursor, cursor + 4.0))
        cursor += 4.0
    cues = design.design_audio_cues(timeline=beats, max_ambience=3, max_sfx=2)

    ambience = [c for c in cues if c.kind == "ambience"]
    sfx = [c for c in cues if c.kind == "sfx"]
    assert len(ambience) == 3
    assert len(sfx) == 2
    # 超上限的环境音并入最后一段，覆盖到最后。
    assert ambience[-1].end_seconds == 48.0


def test_design_audio_cues_empty_timeline():
    assert design.design_audio_cues(timeline=[]) == []


def test_iter_mixable_cues_filters_missing_files(tmp_path):
    present = tmp_path / "audio" / "design" / "ep001" / "bgm_bgm.mp3"
    present.parent.mkdir(parents=True)
    present.write_bytes(b"audio")

    plan = [
        {"cue_id": "bgm", "kind": "bgm", "start_seconds": 0, "end_seconds": 5,
         "audio_path": "audio/design/ep001/bgm_bgm.mp3"},
        {"cue_id": "sfx0", "kind": "sfx", "start_seconds": 1, "end_seconds": 2,
         "audio_path": "audio/design/ep001/sfx_sfx0.mp3"},
    ]
    mixable = design.iter_mixable_cues(plan, tmp_path)
    assert [cue.kind for cue, _path in mixable] == ["bgm"]


def test_audio_design_enabled_env_toggle(monkeypatch):
    monkeypatch.delenv(design.AUDIO_DESIGN_ENABLED_ENV, raising=False)
    assert design.audio_design_enabled() is True
    monkeypatch.setenv(design.AUDIO_DESIGN_ENABLED_ENV, "0")
    assert design.audio_design_enabled() is False


def test_audio_design_caps_env_override(monkeypatch):
    monkeypatch.delenv(design.AUDIO_DESIGN_MAX_AMBIENCE_ENV, raising=False)
    monkeypatch.delenv(design.AUDIO_DESIGN_MAX_SFX_ENV, raising=False)
    assert design.audio_design_caps() == (None, None)
    monkeypatch.setenv(design.AUDIO_DESIGN_MAX_AMBIENCE_ENV, "2")
    monkeypatch.setenv(design.AUDIO_DESIGN_MAX_SFX_ENV, "0")
    assert design.audio_design_caps() == (2, 0)
    monkeypatch.setenv(design.AUDIO_DESIGN_MAX_SFX_ENV, "abc")
    assert design.audio_design_caps() == (2, None)


def test_generate_writes_plan_and_skips_failures(tmp_path):
    timeline = _timeline(
        (_beat(1, "大殿", "【音效：剑鸣】"), 0.0, 6.0),
        (_beat(2, "后山"), 6.0, 12.0),
    )
    calls: list[dict] = []

    async def fake_generate(**kwargs):
        calls.append(kwargs)
        if kwargs["prompt"].startswith("single clean sound effect"):
            raise RuntimeError("gateway rejected")
        Path(kwargs["output_path"]).write_bytes(b"audio")
        return {"ok": True}

    result = asyncio.run(
        design.generate_episode_audio_design(
            project_dir=tmp_path,
            episode=1,
            timeline=timeline,
            style_hint="古装",
            generate=fake_generate,
        )
    )

    # bgm + 2 段环境音成功，1 条音效失败被跳过。
    assert result["cue_count"] == 3
    assert result["generated"] == 3
    assert len(result["failed"]) == 1

    plan_path = design.audio_design_plan_path(tmp_path, 1)
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    assert len(payload["cues"]) == 3
    for cue in payload["cues"]:
        assert (tmp_path / cue["audio_path"]).exists()


def test_generate_reuses_existing_tracks(tmp_path):
    timeline = _timeline((_beat(1, "大殿"), 0.0, 6.0))

    async def fake_generate(**kwargs):
        Path(kwargs["output_path"]).write_bytes(b"audio")
        return {"ok": True}

    asyncio.run(
        design.generate_episode_audio_design(
            project_dir=tmp_path, episode=1, timeline=timeline, generate=fake_generate
        )
    )
    calls: list[dict] = []

    async def counting_generate(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    second = asyncio.run(
        design.generate_episode_audio_design(
            project_dir=tmp_path, episode=1, timeline=timeline, generate=counting_generate
        )
    )
    assert calls == []
    assert second["reused"] == second["cue_count"]


def test_load_audio_design_plan_missing_and_broken(tmp_path):
    assert design.load_audio_design_plan(tmp_path, 1) == []
    plan_path = design.audio_design_plan_path(tmp_path, 1)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("{not json", encoding="utf-8")
    assert design.load_audio_design_plan(tmp_path, 1) == []
