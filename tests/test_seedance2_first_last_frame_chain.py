"""单测：相邻 beat 首尾帧逐帧咬合（滚动咬合，全局默认）。

上游禁止把 ``last_frame_image`` 和参考媒体（参考声线也算）放进同一次请求
（``conflicting_media_inputs``），所以不能用厂商的首尾帧参数。滚动咬合改为：每个 beat
都申请厂商返回尾帧，下一 beat 直接拿上一 beat 的成片尾帧当首帧输入，模式保持严格首帧
——于是同一镜既逐帧接住画面，又能带原生多声线。
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from novelvideo.seedance2_i2v.models import Seedance2I2VMode
from novelvideo.seedance2_i2v.pipeline import prepare_seedance2_generation_inputs


@pytest.fixture(autouse=True)
def _enable_chaining(monkeypatch):
    """本文件专门验证「逐帧咬合」能力本身，所以显式打开开关。

    部署级默认已改为**关**（咬合等于不允许切点，与短剧镜头语言冲突），默认行为由
    ``test_chaining_is_off_by_default`` 单独把关。
    """

    monkeypatch.setenv("FIRST_LAST_FRAME_CHAINING", "1")


def _write_png(path: Path, *, width: int = 512, height: int = 768) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = b"\x89PNG\r\n\x1a\n"
    ihdr = b"IHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    path.write_bytes(header + struct.pack(">I", len(ihdr) - 4) + ihdr)


def _write_tail_frame(project_dir: Path, prev_beat: int, *, ext: str = ".png") -> Path:
    tail = (
        project_dir
        / "videos"
        / "beats"
        / "ep001"
        / "returned_last_frames"
        / f"beat_{prev_beat:02d}{ext}"
    )
    _write_png(tail)
    return tail


def _beat(
    number: int,
    *,
    mode: str | None = None,
    strict_first_frame: bool = True,
) -> dict:
    config: dict = {
        "final_prompt": "严格首帧生成，夜巷空镜，原文全文，逐字念出：夜色沉沉。",
    }
    if mode is not None:
        config["mode"] = mode
    if not strict_first_frame:
        config["strict_first_frame"] = False
    return {
        "beat_number": number,
        "audio_type": "silence",
        "speaker": "",
        "narration": "",
        "seedance2_config_json": json.dumps(config, ensure_ascii=False),
    }


async def _prepare(tmp_path, *, next_beat, beat):
    return await prepare_seedance2_generation_inputs(
        project_output=tmp_path / "project",
        state_dir=tmp_path / "state" / "alice" / "project",
        episode=1,
        beat=beat,
        next_beat=next_beat,
        video_mode="first_frame",
        prompt="",
        duration=5.0,
        characters=[],
    )


async def test_prepare_rolls_previous_tail_frame_into_first_frame(tmp_path):
    project_dir = tmp_path / "project"
    _write_png(project_dir / "frames" / "ep001" / "beat_02.png")
    tail = _write_tail_frame(project_dir, 1)

    prepared = await _prepare(tmp_path, next_beat=None, beat=_beat(2))

    assert prepared.mode == Seedance2I2VMode.FIRST_FRAME
    assert prepared.last_frame_path is None
    assert prepared.image_path == str(tail)
    config = json.loads(prepared.seedance2_config_json)
    assert config["return_last_frame"] is True


async def test_prepare_enables_last_frame_return_for_first_beat(tmp_path):
    project_dir = tmp_path / "project"
    frame = project_dir / "frames" / "ep001" / "beat_01.png"
    _write_png(frame)

    prepared = await _prepare(tmp_path, next_beat=None, beat=_beat(1))

    assert prepared.mode == Seedance2I2VMode.FIRST_FRAME
    assert prepared.last_frame_path is None
    assert prepared.image_path == str(frame)
    config = json.loads(prepared.seedance2_config_json)
    assert config["return_last_frame"] is True


async def test_prepare_falls_back_to_rendered_frame_when_tail_missing(tmp_path):
    project_dir = tmp_path / "project"
    frame = project_dir / "frames" / "ep001" / "beat_02.png"
    _write_png(frame)

    prepared = await _prepare(tmp_path, next_beat=None, beat=_beat(2))

    assert prepared.mode == Seedance2I2VMode.FIRST_FRAME
    assert prepared.image_path == str(frame)
    config = json.loads(prepared.seedance2_config_json)
    assert config["return_last_frame"] is True


async def test_prepare_rolls_tail_frame_with_non_png_extension(tmp_path):
    project_dir = tmp_path / "project"
    _write_png(project_dir / "frames" / "ep001" / "beat_02.png")
    tail = _write_tail_frame(project_dir, 1, ext=".jpg")

    prepared = await _prepare(tmp_path, next_beat=None, beat=_beat(2))

    assert prepared.image_path == str(tail)


async def test_prepare_chaining_can_be_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FIRST_LAST_FRAME_CHAINING", "0")
    project_dir = tmp_path / "project"
    frame = project_dir / "frames" / "ep001" / "beat_01.png"
    _write_png(frame)

    prepared = await _prepare(tmp_path, next_beat=None, beat=_beat(1))

    assert prepared.mode == Seedance2I2VMode.FIRST_FRAME
    config = json.loads(prepared.seedance2_config_json)
    assert config.get("return_last_frame") in (None, False)


async def test_prepare_respects_explicit_multimodal_mode(tmp_path):
    project_dir = tmp_path / "project"
    _write_png(project_dir / "frames" / "ep001" / "beat_01.png")

    prepared = await _prepare(
        tmp_path,
        next_beat=None,
        beat=_beat(1, mode="multimodal_reference", strict_first_frame=False),
    )

    assert prepared.mode == Seedance2I2VMode.MULTIMODAL_REFERENCE


async def test_prepare_last_frame_mode_keeps_multi_voice_references(tmp_path):
    """显式 keyframe（首尾帧）链路不能丢参考声线。

    请求里的 ``reference_audios`` 只从 ``references`` 组装；首尾帧分支若不把
    ``config.reference_audio_paths`` 也放进 ``references``，有台词的一镜多声线
    beat 会静默丢声线（上游自编音色），声线预检也会直接判定「缺参考音频」。
    """

    from novelvideo.models import NovelCharacter

    project_dir = tmp_path / "project"
    _write_png(project_dir / "frames" / "ep001" / "beat_01.png")
    _write_png(project_dir / "frames" / "ep001" / "beat_02.png")
    man_voice = project_dir / "assets" / "characters" / "阿辉" / "voices" / "voice_default.mp3"
    woman_voice = project_dir / "assets" / "characters" / "主播" / "voices" / "voice_default.mp3"
    for path in (man_voice, woman_voice):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(path.name.encode())
    man = NovelCharacter(
        name="阿辉",
        reference_audio_path="assets/characters/阿辉/voices/voice_default.mp3",
    )
    woman = NovelCharacter(
        name="主播",
        reference_audio_path="assets/characters/主播/voices/voice_default.mp3",
    )
    spoken = "阿辉：这下坏了。 主播：别急。"
    beat = {
        "beat_number": 1,
        "audio_type": "dialogue",
        "speaker": "阿辉",
        "detected_identities": ["阿辉", "主播"],
        "narration": "阿辉：这下坏了。\n主播：别急。",
        "narration_segment": "阿辉：这下坏了。\n主播：别急。",
        "seedance2_config_json": json.dumps(
            {
                "final_prompt": (
                    "严格首帧生成。参考@音频1作为阿辉的声线，参考@音频2作为主播的声线。"
                    f"原文全文，逐字念出：{spoken}"
                ),
                "generate_audio": True,
            },
            ensure_ascii=False,
        ),
    }

    prepared = await prepare_seedance2_generation_inputs(
        project_output=project_dir,
        state_dir=tmp_path / "state" / "alice" / "project",
        episode=1,
        beat=beat,
        next_beat={"beat_number": 2},
        video_mode="keyframe",
        prompt="",
        duration=5.0,
        characters=[man, woman],
    )

    assert prepared.mode == Seedance2I2VMode.FIRST_LAST_FRAME
    assert prepared.last_frame_path == str(
        project_dir / "frames" / "ep001" / "beat_02.png"
    )
    audio_refs = [ref for ref in prepared.references if ref.type == "audio"]
    assert {ref.path for ref in audio_refs} == {str(man_voice), str(woman_voice)}


async def test_prepare_speech_beat_rolls_tail_frame_and_keeps_voice(tmp_path):
    """有台词的 beat 也能咬合：走滚动（上一镜尾帧当首帧），不再牺牲声线。"""

    from novelvideo.models import NovelCharacter

    project_dir = tmp_path / "project"
    _write_png(project_dir / "frames" / "ep001" / "beat_02.png")
    voice = project_dir / "assets" / "characters" / "甲" / "voices" / "voice_default.mp3"
    voice.parent.mkdir(parents=True, exist_ok=True)
    voice.write_bytes(b"voice")
    character = NovelCharacter(
        name="甲",
        reference_audio_path="assets/characters/甲/voices/voice_default.mp3",
    )
    tail = _write_tail_frame(project_dir, 1)

    prepared = await prepare_seedance2_generation_inputs(
        project_output=project_dir,
        state_dir=tmp_path / "state" / "alice" / "project",
        episode=1,
        beat={
            "beat_number": 2,
            "audio_type": "dialogue",
            "speaker": "甲",
            "narration": "甲：你好。",
            "narration_segment": "甲：你好。",
            "seedance2_config_json": json.dumps(
                {
                    "final_prompt": "参考@音频1作为甲的声线。原文全文，逐字念出：甲：你好。",
                    "generate_audio": True,
                },
                ensure_ascii=False,
            ),
        },
        next_beat=None,
        video_mode="first_frame",
        prompt="",
        duration=5.0,
        characters=[character],
    )

    # 既咬合又保声线：首帧 = 上一镜尾帧，同时仍带参考声线（靠滚动绕开上游互斥）。
    assert prepared.mode == Seedance2I2VMode.FIRST_FRAME
    assert prepared.last_frame_path is None
    assert prepared.image_path == str(tail)
    assert any(ref.type == "audio" for ref in prepared.references)


def test_project_first_last_frame_chaining_parsing():
    from novelvideo.project_config import project_first_last_frame_chaining

    assert project_first_last_frame_chaining({}) is None
    assert project_first_last_frame_chaining({"first_last_frame_chaining": False}) is False
    assert project_first_last_frame_chaining({"first_last_frame_chaining": "off"}) is False
    assert project_first_last_frame_chaining({"first_last_frame_chaining": "on"}) is True
    assert project_first_last_frame_chaining({"first_last_frame_chaining": 1}) is True


def test_chaining_is_off_by_default(tmp_path, monkeypatch):
    """部署级默认改为**关**：逐帧咬合等于不允许切点，与短剧镜头语言冲突。"""

    from novelvideo.seedance2_i2v.pipeline import _first_last_frame_chaining_enabled

    monkeypatch.delenv("FIRST_LAST_FRAME_CHAINING", raising=False)
    state_dir = tmp_path / "state" / "alice" / "project"
    assert _first_last_frame_chaining_enabled(state_dir) is False

    # 仍可显式打开（部署级 env 优先）。
    monkeypatch.setenv("FIRST_LAST_FRAME_CHAINING", "1")
    assert _first_last_frame_chaining_enabled(state_dir) is True
