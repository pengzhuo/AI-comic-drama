"""Seedance 2.0 clip-duration rules: 5 秒基准，台词长度只做提示不拉长镜头。"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.m09


def test_target_duration_never_below_product_basis():
    from novelvideo.seedance2_i2v.duration import (
        SEEDANCE2_BASE_DURATION_SECONDS,
        resolve_seedance2_target_duration,
    )

    assert SEEDANCE2_BASE_DURATION_SECONDS == 5
    # 历史配置里存的是 4 秒，不能被静默沿用。
    assert resolve_seedance2_target_duration(config_duration=4) == 5
    assert resolve_seedance2_target_duration(config_duration=4, requested=4) == 5
    assert resolve_seedance2_target_duration() == 5


def test_target_duration_keeps_only_explicit_requests():
    from novelvideo.seedance2_i2v.duration import resolve_seedance2_target_duration

    # 后台只回约 5 秒，所以台词再长也不能把请求时长抬上去：
    # 请求 7/10 秒既不会让画面变长，还会让提示词里的节拍时钟指向不存在的画面，
    # 并按请求秒数多付一次网关费用。
    assert resolve_seedance2_target_duration(config_duration=4) == 5
    with pytest.raises(TypeError):
        resolve_seedance2_target_duration(config_duration=4, floor=7)
    # 用户显式要的时长仍然生效。
    assert resolve_seedance2_target_duration(config_duration=12) == 12
    assert resolve_seedance2_target_duration(config_duration=4, requested=9.38) == 10


def test_target_duration_is_clamped_to_model_range():
    from novelvideo.seedance2_i2v.duration import (
        SEEDANCE2_MAX_DURATION_SECONDS,
        resolve_seedance2_target_duration,
    )

    assert SEEDANCE2_MAX_DURATION_SECONDS == 15
    assert resolve_seedance2_target_duration(config_duration=99) == 15
    assert resolve_seedance2_target_duration(config_duration="bad") == 5


def test_duration_floor_follows_the_dubbing_track(monkeypatch, tmp_path):
    from novelvideo.seedance2_i2v.duration import seedance2_duration_floor
    from novelvideo.utils import media_io

    audio_dir = tmp_path / "audio" / "ep001"
    audio_dir.mkdir(parents=True)
    (audio_dir / "beat_06.mp3").write_bytes(b"audio")
    monkeypatch.setattr(media_io, "get_audio_duration", lambda path: 8.67)

    floor = seedance2_duration_floor(
        project_dir=tmp_path,
        episode=1,
        beat={"beat_number": 6, "audio_type": "dialogue", "speaker": "江辰"},
    )

    assert floor == 9


def test_duration_floor_stays_at_basis_for_silent_and_unvoiced_beats(
    monkeypatch,
    tmp_path,
):
    from novelvideo.seedance2_i2v.duration import seedance2_duration_floor
    from novelvideo.utils import media_io

    monkeypatch.setattr(media_io, "get_audio_duration", lambda path: 12.0)

    assert (
        seedance2_duration_floor(
            project_dir=tmp_path,
            episode=1,
            beat={"beat_number": 4, "audio_type": "silence"},
        )
        == 5
    )
    # 有文件但仍是静音类型：不因残留音频把镜头拉长。
    audio_dir = tmp_path / "audio" / "ep001"
    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / "beat_04.mp3").write_bytes(b"audio")
    assert (
        seedance2_duration_floor(
            project_dir=tmp_path,
            episode=1,
            beat={"beat_number": 4, "audio_type": "silence"},
        )
        == 5
    )


def test_duration_floor_respects_an_explicit_beat_duration(monkeypatch, tmp_path):
    from novelvideo.seedance2_i2v.duration import seedance2_duration_floor
    from novelvideo.utils import media_io

    monkeypatch.setattr(media_io, "get_audio_duration", lambda path: 3.0)

    floor = seedance2_duration_floor(
        project_dir=tmp_path,
        episode=1,
        beat={"beat_number": 2, "audio_type": "dialogue", "duration_seconds": 6},
    )

    assert floor == 6
