"""字幕只显示角色**对话台词**，且字号不超过电影字幕带。

现场问题：合成字幕直接取 ``narration_segment`` 原文，于是
``（神色坚定，一手撑住墙面，喘息未平便开口）今天是我的生日……`` 和
``许知意（os）：我已经这么努力了……`` 里的表演提示与说话人标签都被烧进了成片。

口径进一步收敛：**旁白/解说（``narration``）也不出字幕**——旁白照常配 TTS 出声，
只是不上屏。``beat_spoken_text`` 保留「台词 + 旁白」的完整人声口径，供声线预检 /
一致性 gate 使用，不能跟着字幕一起收紧（否则旁白会被误判成无语音）。
"""

import re
from pathlib import Path
from types import SimpleNamespace

from novelvideo.seedance2_i2v.spoken_dialogue import beat_subtitle_text


class _FakeTaskManager:
    def __init__(self):
        self.updates = []

    def update_progress_for_project(self, *args, **kwargs):
        self.updates.append((args, kwargs))


def _ctx(tmp_path):
    return SimpleNamespace(output_dir=tmp_path)


def _write_beat_video(project_dir: Path, episode: int, beat_num: int) -> None:
    video_dir = project_dir / "videos" / "beats" / f"ep{episode:03d}"
    video_dir.mkdir(parents=True, exist_ok=True)
    (video_dir / f"beat_{beat_num:02d}.mp4").write_bytes(b"video")


def test_dialogue_subtitle_drops_the_performance_note():
    beat = {
        "audio_type": "dialogue",
        "speaker": "许知意",
        "narration_segment": "（神色坚定，一手撑住墙面，喘息未平便开口）"
        "今天是我的生日……他们连一句生日快乐都不肯说。",
    }

    assert beat_subtitle_text(beat) == "今天是我的生日……他们连一句生日快乐都不肯说。"


def test_narration_does_not_emit_a_subtitle():
    beat = {
        "audio_type": "narration",
        "narration_segment": "许知意（os）：我已经这么努力了，为什么还是没人愿意留下？",
    }

    assert beat_subtitle_text(beat) == ""


def test_narration_still_counts_as_spoken_text_for_voice_preflight():
    from novelvideo.seedance2_i2v.spoken_dialogue import beat_spoken_text

    beat = {
        "audio_type": "narration",
        "narration_segment": "许知意（os）：我已经这么努力了，为什么还是没人愿意留下？",
    }

    # 字幕不出旁白，但旁白仍有人声：声线预检/gate 的口径不能被字幕带着收紧。
    assert beat_spoken_text(beat) == "我已经这么努力了，为什么还是没人愿意留下？"


def test_label_and_note_are_both_stripped():
    beat = {
        "audio_type": "dialogue",
        "speaker": "系统提示音_系统界面形态",
        "narration_segment": "系统提示音：（面无表情，指尖按下提示键）是否重生？",
    }

    assert beat_subtitle_text(beat) == "是否重生？"


def test_silent_beats_never_emit_a_subtitle():
    beat = {
        "audio_type": "silence",
        "speaker": "",
        "narration_segment": "雨越下越大，镜头缓缓推近苏知意的侧脸。",
    }

    assert beat_subtitle_text(beat) == ""


def test_prose_colon_is_not_treated_as_a_speaker_label():
    beat = {
        "audio_type": "dialogue",
        "speaker": "许知意",
        "narration_segment": "我说的是：我不会再求任何人。",
    }

    assert beat_subtitle_text(beat) == "我说的是：我不会再求任何人。"


def test_every_spoken_line_survives():
    beat = {
        "audio_type": "dialogue",
        "speaker": "许知意",
        "narration_segment": "许知意（强撑着笑）：我不走。\n江辰（冷声）：那就别回来。",
    }

    assert beat_subtitle_text(beat) == "我不走。\n那就别回来。"


def test_compose_episode_burns_spoken_lines_at_movie_size(tmp_path, monkeypatch):
    from novelvideo.task_backend.runners import video

    for beat_num in (1, 2, 3):
        _write_beat_video(tmp_path, episode=1, beat_num=beat_num)

    manager = _FakeTaskManager()
    captured: dict[str, str] = {}

    def fake_run(cmd, **_kwargs):
        joined = " ".join(str(part) for part in cmd)
        if cmd[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout="0\n", stderr="")
        if "ep_subtitles.srt" in joined:
            match = re.search(r"subtitles=filename='([^']+)'", joined)
            assert match is not None
            captured["srt"] = Path(match.group(1)).read_text(encoding="utf-8")
            captured["filter"] = joined
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(video, "get_task_manager", lambda: manager)
    monkeypatch.setattr(video, "run_project_subprocess", fake_run)

    video.run_compose_episode(
        {
            "episode": 1,
            "payload": {
                "output_dir": str(tmp_path),
                "add_subtitles": True,
                "beats": [
                    {
                        "beat_number": 1,
                        "audio_type": "dialogue",
                        "speaker": "许知意",
                        "narration_segment": "（神色坚定，喘息未平便开口）今天是我的生日……"
                        "他们连一句生日快乐都不肯说。",
                    },
                    {
                        "beat_number": 2,
                        "audio_type": "narration",
                        "speaker": "",
                        "narration_segment": "许知意（os）：我已经这么努力了，"
                        "为什么还是没人愿意留下？",
                    },
                    {
                        "beat_number": 3,
                        "audio_type": "silence",
                        "speaker": "",
                        "narration_segment": "雨越下越大，镜头缓缓推近苏知意的侧脸。",
                    },
                ],
            },
        },
        _ctx(tmp_path),
    )

    assert captured["srt"] == (
        "1\n"
        "00:00:00,000 --> 00:00:01,000\n"
        r"{\an2}今天是我的生日……他们连一句生日快乐都不肯说。" "\n"
    )
    assert "（神色坚定" not in captured["srt"]
    assert "许知意（os）" not in captured["srt"]
    # 旁白/解说也不得进字幕。
    assert "我已经这么努力了" not in captured["srt"]
    assert "雨越下越大" not in captured["srt"]

    assert "FontSize=11" in captured["filter"]
    assert "MarginV=24" in captured["filter"]


def test_burned_subtitle_style_centers_every_line():
    from novelvideo.task_backend.runners import video

    style = video._subtitle_force_style(None)

    # Alignment=2 = 整块底部居中，libass 对折行后的每一行也各自居中。
    assert "Alignment=2" in style
    # 多行自动折行时左右留白必须相等，否则行看着像偏了一边。
    margins = dict(re.findall(r"(Margin[LRV])=(\d+)", style))
    assert margins["MarginL"] == margins["MarginR"]
    assert margins["MarginV"] == "24"


def test_burned_subtitle_cues_carry_the_per_line_center_tag(monkeypatch):
    from novelvideo.task_backend.runners import video

    monkeypatch.setattr(video, "_ffprobe_duration", lambda _path: 2.5)

    srt = video._build_srt_from_clips(
        [("我不走。\n那就别回来。", "a.mp4"), ("", "b.mp4")]
    )

    # 一条 beat 的台词按标点逐句拆开，按字数占比分配该 beat 的说话窗口。
    assert srt == (
        "1\n"
        "00:00:00,000 --> 00:00:01,000\n"
        r"{\an2}我不走。" "\n"
        "\n"
        "2\n"
        "00:00:01,000 --> 00:00:02,500\n"
        r"{\an2}那就别回来。" "\n"
    )
    # 每个逐句 cue 都带标签；静音 beat 不占位。
    assert srt.count(video.SUBTITLE_ALIGNMENT_TAG) == 2


def test_subtitle_cue_windows_split_by_sentence_and_share_the_window():
    from novelvideo.seedance2_i2v.spoken_dialogue import (
        split_subtitle_sentences,
        subtitle_cue_windows,
    )

    text = "第一句。这里更长的一句台词。"
    assert split_subtitle_sentences(text) == ["第一句。", "这里更长的一句台词。"]

    windows = subtitle_cue_windows(text, 0.0, 10.0)
    assert [window[0] for window in windows] == ["第一句。", "这里更长的一句台词。"]
    # 首句字数少，窗口也短；两句首尾相接并铺满整个 beat 窗口。
    assert windows[0][1] == 0.0
    assert windows[0][2] == windows[1][1]
    assert windows[-1][2] == 10.0
    assert windows[0][2] < windows[1][2] - windows[1][1]


def test_subtitle_cue_windows_keeps_one_cue_when_window_is_too_short():
    from novelvideo.seedance2_i2v.spoken_dialogue import subtitle_cue_windows

    # 窗口太短撑不起逐句展示时，整句一次上屏，避免字幕一闪而过。
    assert subtitle_cue_windows("一句。两句。", 0.0, 1.0) == [
        ("一句。两句。", 0.0, 1.0)
    ]

