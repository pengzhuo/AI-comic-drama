"""说话人匹配必须边界感知：短名不能吞掉同前缀的长名。

故障现场：``系统`` 用 ``startswith`` 抢走了 ``系统提示音_系统界面形态``，
于是配音用了「系统」的声线，而视频侧要求「系统提示音」自己的参考声线、
永远判缺，报「缺少参考声线」。
"""

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from novelvideo.seedance2_i2v.speaker_matching import (
    select_character_for_speaker,
    speaker_character_name,
    speaker_matches_character_name,
)

SHADOW_SPEAKER = "系统提示音_系统界面形态"


@dataclass
class _Identity:
    identity_id: str
    identity_name: str
    age_group: str = ""
    reference_audio_path: str = ""
    reference_audio_sha256: str = ""


@dataclass
class _Character:
    name: str
    identities: list = field(default_factory=list)
    aliases: list = field(default_factory=list)
    reference_audio_path: str = ""
    reference_audio_sha256: str = ""
    voice_samples_by_age_group: dict = field(default_factory=dict)
    is_main: bool = False


def _store(tmp_path: Path, characters: list) -> SimpleNamespace:
    async def list_characters():
        return list(characters)

    return SimpleNamespace(project_dir=str(tmp_path), list_characters=list_characters)


def _write_voice(tmp_path: Path, name: str) -> str:
    relative = f"assets/characters/{name}/voices/voice_default.mp3"
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"voice:{name}".encode())
    return relative


def _shadow_pair(tmp_path: Path) -> list[_Character]:
    system = _Character(
        name="系统",
        reference_audio_path=_write_voice(tmp_path, "系统"),
        aliases=["旁白系统"],
    )
    prompt_tone = _Character(
        name="系统提示音",
        identities=[
            _Identity(identity_id=SHADOW_SPEAKER, identity_name="系统界面形态"),
        ],
    )
    return [system, prompt_tone]


def test_speaker_character_name_splits_identity_suffix():
    assert speaker_character_name(SHADOW_SPEAKER) == "系统提示音"
    assert speaker_character_name("苏知意") == "苏知意"
    assert speaker_character_name("") == ""


def test_prefix_identical_shorter_name_is_not_a_match():
    assert speaker_matches_character_name(SHADOW_SPEAKER, "系统提示音") is True
    assert speaker_matches_character_name(SHADOW_SPEAKER, "系统") is False
    assert speaker_matches_character_name("苏知意", "苏知意") is True


def test_select_character_prefers_the_longest_matching_name(tmp_path):
    characters = _shadow_pair(tmp_path)
    assert select_character_for_speaker(SHADOW_SPEAKER, characters).name == "系统提示音"
    assert select_character_for_speaker("系统", characters).name == "系统"
    assert select_character_for_speaker("未知角色", characters) is None


def test_asset_speaker_match_keeps_alias_fallback(tmp_path):
    from novelvideo.seedance2_i2v.assets import _speaker_matches_character

    system, prompt_tone = _shadow_pair(tmp_path)
    assert _speaker_matches_character(SHADOW_SPEAKER, prompt_tone) is True
    assert _speaker_matches_character(SHADOW_SPEAKER, system) is False
    assert _speaker_matches_character("旁白系统", system) is True


async def test_dialogue_voice_resolution_rejects_shadow_match(tmp_path):
    from novelvideo.seedance2_i2v.voice_clone import resolve_dialogue_reference_audio

    characters = _shadow_pair(tmp_path)
    store = _store(tmp_path, characters)
    beat = {
        "audio_type": "dialogue",
        "speaker": SHADOW_SPEAKER,
        "narration_segment": "是否重生？",
    }

    assert await resolve_dialogue_reference_audio(beat, store) is None


async def test_dialogue_voice_resolution_uses_the_owning_character(tmp_path):
    from novelvideo.seedance2_i2v.voice_clone import resolve_dialogue_reference_audio

    characters = _shadow_pair(tmp_path)
    prompt_tone = characters[1]
    prompt_tone.reference_audio_path = _write_voice(tmp_path, "系统提示音")
    store = _store(tmp_path, characters)
    beat = {
        "audio_type": "dialogue",
        "speaker": SHADOW_SPEAKER,
        "narration_segment": "是否重生？",
    }

    resolved = await resolve_dialogue_reference_audio(beat, store)
    assert resolved is not None
    assert resolved[0] == tmp_path / "assets/characters/系统提示音/voices/voice_default.mp3"


async def test_identity_voice_sha_lookup_rejects_shadow_match(tmp_path):
    from novelvideo.seedance2_i2v.voice_clone import file_sha256
    from novelvideo.seedance2_i2v.voice_audio_task import resolve_identity_voice_sha256

    characters = _shadow_pair(tmp_path)
    store = _store(tmp_path, characters)
    shadowed_sha = file_sha256(tmp_path / "assets/characters/系统/voices/voice_default.mp3")

    assert await resolve_identity_voice_sha256(store, SHADOW_SPEAKER) == ""

    relative = _write_voice(tmp_path, "系统提示音")
    characters[1].reference_audio_path = relative

    own_sha = await resolve_identity_voice_sha256(store, SHADOW_SPEAKER)
    assert own_sha == file_sha256(tmp_path / relative)
    assert own_sha != shadowed_sha


def test_system_voice_setup_targets_the_longest_name(tmp_path):
    from novelvideo.audio.system_voice_setup import _matching_character

    characters = _shadow_pair(tmp_path)
    assert _matching_character(SHADOW_SPEAKER, characters).name == "系统提示音"
    assert _matching_character("系统", characters).name == "系统"


async def test_missing_voice_diagnosis_names_the_owning_character(tmp_path):
    from novelvideo.audio.indextts2_beat_audio_task import _diagnose_missing_dialogue_voice

    characters = _shadow_pair(tmp_path)
    hint = await _diagnose_missing_dialogue_voice(SHADOW_SPEAKER, _store(tmp_path, characters))

    assert "找到角色「系统提示音」" in hint
    assert "系统界面形态" in hint


def test_missing_reference_voice_error_points_at_the_fix():
    from novelvideo.seedance2_i2v.assets import Seedance2ResolvedAsset
    from novelvideo.seedance2_i2v.pipeline import _validate_dialogue_final_prompt

    asset = Seedance2ResolvedAsset(
        key=f"voice:{SHADOW_SPEAKER}",
        label="系统提示音 · 系统界面形态声线",
        media_type="audio",
        path=Path("/missing/voice_default.mp3"),
        exists=False,
        selected=False,
        request_field="",
        reference_label="未发送",
    )

    with pytest.raises(ValueError) as excinfo:
        _validate_dialogue_final_prompt(
            beat={
                "audio_type": "dialogue",
                "speaker": SHADOW_SPEAKER,
                "narration_segment": "是否重生？",
            },
            final_prompt="台词：是否重生？",
            assets=[asset],
        )

    message = str(excinfo.value)
    assert "系统提示音 · 系统界面形态声线" in message
    assert "系统声线" in message


def test_prompt_without_audio_label_keeps_the_audio_number_hint():
    from novelvideo.seedance2_i2v.assets import Seedance2ResolvedAsset
    from novelvideo.seedance2_i2v.pipeline import _validate_dialogue_final_prompt

    asset = Seedance2ResolvedAsset(
        key="voice:苏知意",
        label="苏知意声线",
        media_type="audio",
        path=Path("/existing/voice_default.mp3"),
        exists=True,
        selected=False,
        request_field="",
        reference_label="音频1",
    )

    with pytest.raises(ValueError) as excinfo:
        _validate_dialogue_final_prompt(
            beat={
                "audio_type": "dialogue",
                "speaker": "苏知意",
                "narration_segment": "为什么还是没人愿意留下？",
            },
            final_prompt="台词：为什么还是没人愿意留下？",
            assets=[asset],
        )

    assert "音频编号" in str(excinfo.value)
