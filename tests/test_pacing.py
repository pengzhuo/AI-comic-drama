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
    # 短剧镜头语言：单镜默认 6s（甜点区），可用窗口 3–8s。
    assert settings.shot_target_seconds == 6.0
    assert settings.shot_min_seconds == 3.0
    assert settings.shot_max_seconds == 8.0
    # 正片预算 = 60 - 3 - 2
    assert settings.content_budget_seconds == 55.0
    # 55 / 6 -> 10 镜；每镜都吃满 8s 时下限 7 镜
    assert settings.max_shots == 10
    assert settings.min_shots == 7

    overridden = resolve_pacing_settings({"shot_target_seconds": 8})
    assert overridden.shot_target_seconds == 8.0
    assert overridden.max_shots == 7  # ceil(55/8)

    # 非法值回退默认
    assert resolve_pacing_settings({"shot_target_seconds": -1}).shot_target_seconds == 6.0


def test_resolve_pacing_settings_uses_selected_model_duration_window(monkeypatch):
    # 未显式配置单镜预算时取短剧甜点区（6s），再按所选模型的时长边界夹紧：
    # 换模型时镜头数基本不变，但绝不会发出模型不接受的时长。
    settings = resolve_pacing_settings({"video_backend": "newapi_seedance-2.5"})
    assert settings.shot_target_seconds == 6.0
    assert (settings.shot_min_seconds, settings.shot_max_seconds) == (4.0, 8.0)

    mini = resolve_pacing_settings({"video_backend": "newapi_seedance-2.0-mini"})
    assert (mini.shot_min_seconds, mini.shot_max_seconds) == (4.0, 8.0)

    # 未知后端：回退短剧默认窗口。
    unknown = resolve_pacing_settings({"video_backend": "unknown-backend"})
    assert unknown.shot_target_seconds == 6.0
    assert (unknown.shot_min_seconds, unknown.shot_max_seconds) == (3.0, 8.0)

    # 模型上限比甜点区还窄时，单镜目标被夹到模型上限。
    from novelvideo import config as config_module

    monkeypatch.setattr(config_module, "NEWAPI_VIDEO_DURATION_BOUNDS", "tiny:4-5")
    narrowed = resolve_pacing_settings({"video_backend": "newapi_tiny"})
    assert narrowed.shot_target_seconds == 5.0
    assert (narrowed.shot_min_seconds, narrowed.shot_max_seconds) == (4.0, 5.0)

    # 显式配置仍优先于模型窗口。
    assert (
        resolve_pacing_settings(
            {"video_backend": "newapi_seedance-2.5", "shot_target_seconds": 8}
        ).shot_target_seconds
        == 8.0
    )


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
    # 按说话人分镜：换人就断开，乙 另起一镜（保证逐行画面与声线一一对应）。
    assert len(merged) == 2
    assert merged[0].speaker == "甲"
    assert merged[0].narration_segment == "对白一，对白二"
    # 同说话人不逐行标人——保持纯文本合并口径。
    assert "\n" not in merged[0].narration_segment
    assert merged[1].speaker == "乙"
    assert merged[1].narration_segment == "对白三"
    # 画面锚点取对白镜，而非首个空镜
    assert merged[0].visual_description == "画面1"
    assert merged[1].visual_description == "画面5"


def test_compact_beats_same_speaker_keeps_plain_text():
    beats = [
        _beat(1, "甲句一", audio_type="dialogue", speaker="甲"),
        _beat(2, "甲句二", audio_type="dialogue", speaker="甲"),
    ]
    merged = compact_beats_for_shots(beats, shot_target_seconds=30)
    assert len(merged) == 1
    # 单一说话人不必逐行标人——保持旧的纯文本合并口径。
    assert "\n" not in merged[0].narration_segment
    assert "甲句一" in merged[0].narration_segment
    assert "甲句二" in merged[0].narration_segment


def test_compact_beats_keeps_different_speakers_separate():
    # 跨说话人**不合并**：每句台词各占一镜，画面/声线逐行对应，避免十几秒双声线长镜。
    beats = [
        _beat(1, "一", audio_type="dialogue", speaker="甲"),
        _beat(2, "二", audio_type="dialogue", speaker="乙"),
        _beat(3, "三", audio_type="dialogue", speaker="丙"),
        _beat(4, "四", audio_type="dialogue", speaker="丁"),
    ]
    merged = compact_beats_for_shots(beats, shot_target_seconds=30)
    assert len(merged) == 4
    assert [b.speaker for b in merged] == ["甲", "乙", "丙", "丁"]
    assert [b.narration_segment for b in merged] == ["一", "二", "三", "四"]
    assert all("\n" not in b.narration_segment for b in merged)


def test_compact_beats_counts_per_line_overhead_for_dialogue():
    # 对白逐行合成再拼接，每行有头尾静音开销（实测 ≈1.3s/行），估时要算进去：
    # 33/4.45 ≈ 7.42s，两行 14.83s 本可合并，加上 2×1.3s 拼接开销后 17.4s > 15s。
    beats = [
        _beat(1, "字" * 33, audio_type="dialogue", speaker="甲"),
        _beat(2, "词" * 33, audio_type="dialogue", speaker="甲"),
    ]
    merged = compact_beats_for_shots(beats, shot_target_seconds=15, chars_per_second=4.45)
    assert len(merged) == 2


def test_dialogue_line_count_counts_nonempty_lines():
    from novelvideo.pacing import _dialogue_line_count

    assert _dialogue_line_count(
        _beat(1, "甲：一\n乙：二", audio_type="dialogue", speaker="甲")
    ) == 2
    # 空行不算行。
    assert _dialogue_line_count(
        _beat(1, "甲：一\n\n乙：二", audio_type="dialogue", speaker="甲")
    ) == 2
    assert _dialogue_line_count(
        _beat(1, "甲：一", audio_type="dialogue", speaker="甲")
    ) == 1
    # 非对白不逐行合成，没有逐行开销。
    assert _dialogue_line_count(_beat(1, "旁白一\n旁白二")) == 0


def test_compact_beats_does_not_charge_dialogue_overhead_for_narration():
    # 旁白走单次合成，没有逐行拼接开销；同样的字数应并成一镜。
    beats = [
        _beat(1, "字" * 33),
        _beat(2, "词" * 33),
    ]
    merged = compact_beats_for_shots(beats, shot_target_seconds=15, chars_per_second=4.45)
    assert len(merged) == 1


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


def test_compact_beats_leading_silence_run_never_becomes_its_own_shot():
    # 一连串过场 beat（▲空镜/字幕/人物行）本身没有台词：即便名义时长早就超过单镜
    # 预算，也必须攒着并入下一段对白，不能各自变成 4s 无声镜头（拖节奏又费钱）。
    beats: list[Any] = [_silence(i) for i in range(1, 6)]
    beats.append(_beat(6, "台词", audio_type="dialogue", speaker="甲"))
    merged = compact_beats_for_shots(beats, shot_target_seconds=6)
    assert len(merged) == 1
    assert merged[0].audio_type == "dialogue"
    assert merged[0].speaker == "甲"
    # 画面锚点取对白镜，而非首个空镜。
    assert merged[0].visual_description == "画面6"


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


def test_split_long_beats_separates_multi_speaker_lines_into_single_speaker_shots():
    # 一镜多声线：整段发一条 seedance2 请求会让模型在一段视频里念两个音色（串味），
    # 还常把镜头拉长到十几二十秒。按**行**拆开，每行连同说话人前缀单独成镜。
    beat = _beat(
        1,
        "甲：" + "字" * 174 + "\n乙：" + "字" * 174,
        audio_type="dialogue",
        speaker="甲",
    )
    split = split_long_beats_for_shots([beat], max_seconds=15, chars_per_second=5.8)
    assert len(split) >= 2
    # 每镜只有一个说话人，台词里不再有换行。
    assert all("\n" not in b.narration_segment for b in split)
    assert all(b.narration_segment.startswith(f"{b.speaker}：") for b in split)
    # 说话人随各自的行还原（第一条是甲，最后一条是乙），序号连续重排。
    assert split[0].speaker == "甲"
    assert split[-1].speaker == "乙"
    assert [b.beat_number for b in split] == list(range(1, len(split) + 1))
    # 台词一字不差、不重复（前缀不算台词）。
    spoken = "".join(b.narration_segment.split("：", 1)[1] for b in split)
    assert spoken.count("字") == 2 * 174
    # 每镜台词都在单镜预算内。
    cap = 15 * 0.85
    for beat_piece in split:
        text = beat_piece.narration_segment.split("：", 1)[1]
        assert (
            estimate_duration_seconds(text, chars_per_second=5.8) <= cap + 1e-6
        )


def test_split_long_beats_separates_short_multi_speaker_beat_too():
    # 即便整段不长，也拆成单说话人镜头——复现基线「一镜一说话人」。
    beat = _beat(1, "甲：你来了\n乙：我来了", audio_type="dialogue", speaker="甲")
    split = split_long_beats_for_shots([beat], max_seconds=15, chars_per_second=5.8)
    assert [b.narration_segment for b in split] == ["甲：你来了", "乙：我来了"]
    assert [b.speaker for b in split] == ["甲", "乙"]
    # 非对白多行（无说话人前缀）沿用 beat 自身声明的说话人，不丢信息。
    plain = _beat(2, "第一行\n第二行", audio_type="dialogue", speaker="甲")
    plain_split = split_long_beats_for_shots([plain], max_seconds=15)
    assert [b.narration_segment for b in plain_split] == ["第一行", "第二行"]
    assert all(b.speaker == "甲" for b in plain_split)


def test_split_long_beats_keeps_manual_shot_untouched():
    manual = _beat(1, "字" * 174, audio_type="dialogue", speaker="甲")
    manual.is_manual_shot = True
    manual.duration_seconds = 20.0
    assert split_long_beats_for_shots([manual], max_seconds=15) == [manual]
    # 手工分镜即使带多行说话人也整段不拆。
    manual_multi = _beat(2, "甲：一\n乙：二", audio_type="dialogue", speaker="甲")
    manual_multi.is_manual_shot = True
    assert (
        split_long_beats_for_shots([manual_multi], max_seconds=15) == [manual_multi]
    )


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

