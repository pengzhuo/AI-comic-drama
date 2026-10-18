"""字幕只显示念出来的台词，且字号不超过电影字幕带。

现场问题：合成字幕直接取 ``narration_segment`` 原文，于是
``（神色坚定，一手撑住墙面，喘息未平便开口）今天是我的生日……`` 和
``许知意（os）：我已经这么努力了……`` 里的表演提示与说话人标签都被烧进了成片。
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


def test_narration_subtitle_drops_the_speaker_label():
    beat = {
        "audio_type": "narration",
        "narration_segment": "许知意（os）：我已经这么努力了，为什么还是没人愿意留下？",
    }

    assert beat_subtitle_text(beat) == "我已经这么努力了，为什么还是没人愿意留下？"


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
        "今天是我的生日……他们连一句生日快乐都不肯说。\n"
        "\n"
        "2\n"
        "00:00:01,000 --> 00:00:02,000\n"
        "我已经这么努力了，为什么还是没人愿意留下？\n"
    )
    assert "（神色坚定" not in captured["srt"]
    assert "许知意（os）" not in captured["srt"]
    assert "雨越下越大" not in captured["srt"]

    assert "FontSize=11" in captured["filter"]
    assert "MarginV=24" in captured["filter"]
