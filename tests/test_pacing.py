"""单测：节奏核心（分集 + 镜头合并）。"""

from __future__ import annotations

from novelvideo.models import SceneRef, VisualBeat
from novelvideo.pacing import (
    PacingSettings,
    build_episode_segments,
    compact_beats_for_shots,
    condense_text,
    estimate_duration_seconds,
    resolve_pacing_settings,
    split_long_beats_for_shots,
)


def _beat(number: int, narration: str, *, audio_type="narration", speaker="", scene="家"):
    return VisualBeat(
        beat_number=number,
        narration_segment=narration,
        visual_description=f"画面{number}",
        audio_type=audio_type,
        speaker=speaker,
        scene_ref=SceneRef(scene_id=scene) if scene else None,
    )


def test_estimate_duration_uses_chars_and_rate():
    # 58 个字 / 5.8 字/秒 = 10 秒
    assert abs(estimate_duration_seconds("字" * 58, chars_per_second=5.8) - 10.0) < 1e-6
    assert estimate_duration_seconds("  \n  ") == 0.0


def test_resolve_pacing_settings_defaults_and_overrides():
    settings = resolve_pacing_settings(None)
    assert settings.episode_target_seconds == 60.0
    assert settings.merge_floor_seconds == 45.0
    assert settings.split_threshold_seconds == 90.0
    assert settings.shot_target_seconds == 30.0
    # 正片预算 = 60 - 3 - 2
    assert settings.content_budget_seconds == 55.0
    # 55 / 30 -> 2 镜
    assert settings.max_shots == 2

    overridden = resolve_pacing_settings({"shot_target_seconds": 8})
    assert overridden.shot_target_seconds == 8.0
    assert overridden.max_shots == 7  # ceil(55/8)

    # 非法值回退默认
    assert resolve_pacing_settings({"shot_target_seconds": -1}).shot_target_seconds == 30.0


def test_condense_text_keeps_head_and_last_sentence():
    text = "第一句话。" + "中间填充。" * 50 + "最后一句钩子。"
    condensed = condense_text(text, max_seconds=10, chars_per_second=5.8)
    assert condensed.endswith("最后一句钩子。")
    assert condensed.startswith("第一句话。")
    assert estimate_duration_seconds(condensed, chars_per_second=5.8) <= 10 + 0.2


def test_build_episode_segments_merges_short_chapters():
    # 每章约 10 秒，目标正片 55 秒 -> 约 5 章一集
    chapters = [(i, "字" * 58) for i in range(1, 13)]
    segments = build_episode_segments(chapters, settings=resolve_pacing_settings({}))
    assert 2 <= len(segments) <= 4
    # 章节范围连续
    assert segments[0]["chapter_start"] == 1
    for seg in segments:
        assert seg["chapter_start"] <= seg["chapter_end"]
        assert estimate_duration_seconds(seg["text"]) <= 55 + 2


def test_build_episode_segments_splits_long_chapter():
    # 一章 ~300 秒（1740 字）> 90s 拆分阈值 -> 拆成多集
    long_chapter = "。".join("句子" * 4 for _ in range(60)) + "。"
    segments = build_episode_segments([(1, long_chapter)])
    assert len(segments) >= 2


def test_build_episode_segments_condenses_medium_chapter():
    # 一章约 80 秒（464 字）：45 < d <= 90 -> 压到 ~55 秒，不拆
    medium = "句子内容。" * 92  # 5 字 * 92 = 460 字 ≈ 79 秒
    segments = build_episode_segments([(1, medium)])
    assert len(segments) == 1
    assert estimate_duration_seconds(segments[0]["text"]) <= 55 + 2


def test_compact_beats_merges_same_audio_into_long_shots():
    beats = [_beat(i, "字" * 29) for i in range(1, 9)]  # 每个 5 秒，共 40 秒
    merged = compact_beats_for_shots(
        beats, shot_target_seconds=15, max_shots=4
    )
    assert len(merged) <= 4
    # 序号从 1 连续
    assert [b.beat_number for b in merged] == list(range(1, len(merged) + 1))
    # 旁白合并后仍包含全部原文
    joined = "".join(b.narration_segment for b in merged)
    assert joined.count("字") == 8 * 29


def test_compact_beats_respects_audio_boundaries():
    beats = [
        _beat(1, "旁白甲" * 3, audio_type="narration"),
        _beat(2, "旁白乙" * 3, audio_type="narration"),
        _beat(3, "角色台词" * 3, audio_type="dialogue", speaker="苏清晏"),
        _beat(4, "旁白丙" * 3, audio_type="narration"),
    ]
    merged = compact_beats_for_shots(beats, shot_target_seconds=15)
    # 台词不能和旁白并到同一镜
    assert not any(
        b.audio_type == "narration"
        and b.narration_segment.count("台词") > 0
        for b in merged
    )
    # 至少保证台词单独成镜
    dialogue_beats = [b for b in merged if b.audio_type == "dialogue"]
    assert len(dialogue_beats) == 1
    assert dialogue_beats[0].speaker == "苏清晏"


def test_compact_beats_single_and_empty():
    assert compact_beats_for_shots([]) == []
    only = [_beat(1, "唯一一镜")]
    assert compact_beats_for_shots(only) == only


def _silence(number: int, *, scene: str = "家"):
    return VisualBeat(
        beat_number=number,
        narration_segment="",
        visual_description=f"空镜{number}",
        audio_type="silence",
        speaker="",
        scene_ref=SceneRef(scene_id=scene) if scene else None,
    )


def test_compact_beats_folds_silence_into_neighbor():
    beats = [
        _beat(1, "对白一", audio_type="dialogue", speaker="甲"),
        _silence(2),
        _beat(3, "对白二", audio_type="dialogue", speaker="甲"),
        _silence(4),
        _beat(5, "对白三", audio_type="dialogue", speaker="乙"),
    ]
    merged = compact_beats_for_shots(beats, shot_target_seconds=15)
    # 纯过场 beat 不再单独成镜
    assert all(b.audio_type != "silence" for b in merged)
    # 甲的两句 + 中间空镜并为一镜；乙单独一镜
    assert len(merged) == 2
    assert merged[0].speaker == "甲"
    assert merged[0].narration_segment.count("对白") == 2
    # 画面锚点取对白镜，而非首个空镜
    assert merged[0].visual_description == "画面1"
    assert merged[1].speaker == "乙"


def test_compact_beats_leading_silence_joins_following_speech():
    beats = [
        _silence(1),
        _beat(2, "唯一对白", audio_type="dialogue", speaker="甲"),
    ]
    merged = compact_beats_for_shots(beats, shot_target_seconds=15)
    assert len(merged) == 1
    assert merged[0].audio_type == "dialogue"
    assert merged[0].speaker == "甲"
    assert merged[0].visual_description == "画面2"


def test_compact_beats_silence_only_keeps_one_group():
    merged = compact_beats_for_shots(
        [_silence(1), _silence(2)], shot_target_seconds=15
    )
    assert len(merged) == 1
    assert merged[0].audio_type == "silence"


def test_pacing_settings_normalized():
    s = PacingSettings(
        episode_target_seconds=30,
        merge_floor_seconds=90,
        split_threshold_seconds=10,
        shot_target_seconds=5,
    ).normalized()
    assert s.merge_floor_seconds <= s.episode_target_seconds
    assert s.split_threshold_seconds > s.episode_target_seconds


# ── 单条超长朗读 beat 拆分 ────────────────────────────────────────────────────


def test_split_long_beats_breaks_over_budget_line_and_renumbers():
    long_line = "字" * 174  # 174 / 5.8 ≈ 30s，远超单镜上限
    beats = [_beat(1, long_line, audio_type="dialogue", speaker="苏清晏")]
    split = split_long_beats_for_shots(beats, max_seconds=15, chars_per_second=5.8)
    assert len(split) >= 2
    # 序号从 1 连续重排
    assert [b.beat_number for b in split] == list(range(1, len(split) + 1))
    cap = 15 * 0.9
    for beat in split:
        assert (
            estimate_duration_seconds(beat.narration_segment, chars_per_second=5.8)
            <= cap + 1e-6
        )
        # 音色/场景/画面随拆分继承
        assert beat.audio_type == "dialogue"
        assert beat.speaker == "苏清晏"
        assert beat.visual_description == "画面1"
    # 原文不丢、不重复
    assert "".join(b.narration_segment for b in split).count("字") == 174


def test_split_long_beats_prefers_sentence_boundaries():
    line = "第一句话。" + "第二句话。" + "第三句话。" + "第四句话。"
    split = split_long_beats_for_shots(
        [_beat(1, line)], max_seconds=2, chars_per_second=5.8
    )
    assert len(split) >= 2
    # 一字不差，且每刀都切在句末标点
    assert "".join(b.narration_segment for b in split) == line
    assert all(b.narration_segment.endswith("。") for b in split)


def test_split_long_beats_leaves_short_and_manual_untouched():
    short = _beat(1, "短台词", audio_type="dialogue", speaker="甲")
    assert split_long_beats_for_shots([short], max_seconds=15) == [short]
    assert split_long_beats_for_shots([], max_seconds=15) == []

    manual = _beat(2, "字" * 174, audio_type="dialogue", speaker="甲")
    manual.is_manual_shot = True
    manual.duration_seconds = 20.0
    assert split_long_beats_for_shots([manual], max_seconds=15) == [manual]


def test_compact_then_split_keeps_every_shot_within_cap():
    beats = [_beat(i, "字" * 58) for i in range(1, 9)]  # 每条 ≈10s，共 ≈80s
    merged = compact_beats_for_shots(beats, shot_target_seconds=15, max_shots=4)
    split = split_long_beats_for_shots(merged, max_seconds=15, chars_per_second=5.8)
    cap = 15 * 0.9
    for beat in split:
        assert (
            estimate_duration_seconds(beat.narration_segment, chars_per_second=5.8)
            <= cap + 1e-6
        )
    # 合并 + 拆分后正文仍完整
    assert "".join(b.narration_segment for b in split).count("字") == 8 * 58

