"""成片必须 faststart（moov 前置）。

现场问题：整集合成的最后一步 ffmpeg 用 ``-c:v libx264`` 直出，没带
``-movflags +faststart``，于是 ``moov`` 被写在 ``mdat`` 之后（文件尾部）。
手机浏览器要拿到 ``moov`` 才能起播，而它排在 196MB 的最后，等于必须下完整片
——在 iPhone 上就表现为「第一集无法预览 / 无法下载」。
"""

from pathlib import Path
from types import SimpleNamespace


class _FakeTaskManager:
    def update_progress_for_project(self, *args, **kwargs):
        pass


def _write_beat_video(project_dir: Path, episode: int, beat_num: int) -> None:
    video_dir = project_dir / "videos" / "beats" / f"ep{episode:03d}"
    video_dir.mkdir(parents=True, exist_ok=True)
    (video_dir / f"beat_{beat_num:02d}.mp4").write_bytes(b"video")


def test_compose_episode_final_encodes_faststart(tmp_path, monkeypatch):
    from novelvideo.task_backend.runners import video

    _write_beat_video(tmp_path, episode=1, beat_num=1)
    _write_beat_video(tmp_path, episode=1, beat_num=2)

    commands: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        commands.append([str(part) for part in cmd])
        if cmd[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout="0\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(video, "get_task_manager", lambda: _FakeTaskManager())
    monkeypatch.setattr(video, "run_project_subprocess", fake_run)

    video.run_compose_episode(
        {
            "episode": 1,
            "payload": {
                "output_dir": str(tmp_path),
                "beats": [
                    {
                        "beat_number": 1,
                        "audio_type": "dialogue",
                        "speaker": "许知意",
                        "narration_segment": "今天是我的生日。",
                    },
                    {
                        "beat_number": 2,
                        "audio_type": "silence",
                        "speaker": "",
                        "narration_segment": "雨越下越大。",
                    },
                ],
            },
        },
        SimpleNamespace(output_dir=tmp_path),
    )

    final_cmd = next(
        cmd
        for cmd in reversed(commands)
        if cmd and cmd[0] == "ffmpeg" and cmd[-1].endswith("ep001_final.mp4")
    )
    assert "-movflags" in final_cmd
    assert final_cmd[final_cmd.index("-movflags") + 1] == "+faststart"
