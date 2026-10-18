from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.m09


class _FakeTaskManager:
    def __init__(self):
        self.updates = []

    def update_progress_for_project(self, *args, **kwargs):
        self.updates.append((args, kwargs))


def _ctx(tmp_path):
    return SimpleNamespace(output_dir=tmp_path)


def _write_beat_video(project_dir, episode: int, beat_num: int) -> None:
    video_dir = project_dir / "videos" / "beats" / f"ep{episode:03d}"
    video_dir.mkdir(parents=True)
    (video_dir / f"beat_{beat_num:02d}.mp4").write_bytes(b"video")


def _write_beat_audio(project_dir, episode: int, beat_num: int) -> None:
    audio_dir = project_dir / "audio" / f"ep{episode:03d}"
    audio_dir.mkdir(parents=True)
    (audio_dir / f"beat_{beat_num:02d}.mp3").write_bytes(b"audio")


def _fake_probe_runner(commands, *, video_seconds: str, dub_seconds: str | None):
    """ffprobe stub: stream probe says "has audio", duration probe is controlled."""

    def fake_run(cmd, **_kwargs):
        commands.append(cmd)
        if cmd[0] == "ffprobe":
            if "-select_streams" in cmd:
                return SimpleNamespace(returncode=0, stdout="0\n", stderr="")
            seconds = video_seconds
            if dub_seconds and str(cmd[-1]).endswith(".mp3"):
                seconds = dub_seconds
            return SimpleNamespace(returncode=0, stdout=f"{seconds}\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return fake_run


def _clip_command(commands, beat_num: int) -> list[str]:
    return next(
        cmd
        for cmd in commands
        if cmd[0] == "ffmpeg" and cmd[-1].endswith(f"beat_{beat_num:04d}.mp4")
    )


def test_compose_episode_preserves_embedded_audio_when_no_external_mp3(
    monkeypatch,
    tmp_path,
):
    from novelvideo.task_backend.runners import video

    _write_beat_video(tmp_path, episode=1, beat_num=3)
    manager = _FakeTaskManager()
    commands = []

    def fake_run(cmd, **_kwargs):
        commands.append(cmd)
        if cmd[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout="0\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(video, "get_task_manager", lambda: manager)
    monkeypatch.setattr(video, "run_project_subprocess", fake_run)

    result = video.run_compose_episode(
        {
            "episode": 1,
            "payload": {
                "output_dir": str(tmp_path),
                "beats": [{"beat_number": 3}],
            },
        },
        _ctx(tmp_path),
    )

    clip_cmd = next(cmd for cmd in commands if cmd[0] == "ffmpeg" and "beat_0003.mp4" in cmd[-1])
    assert result["video_path"].endswith("videos/episodes/ep001_final.mp4")
    assert "anullsrc=r=44100:cl=stereo" not in clip_cmd
    assert clip_cmd[clip_cmd.index("-map") + 1] == "0:v:0"
    assert "0:a:0" in clip_cmd
    assert any(
        "使用视频内置音轨" in line
        for _args, kwargs in manager.updates
        for line in kwargs.get("logs", [])
    )


def test_compose_episode_prefers_seedance2_native_dialogue_over_dub(
    monkeypatch,
    tmp_path,
):
    from novelvideo.task_backend.runners import video

    _write_beat_video(tmp_path, episode=1, beat_num=5)
    _write_beat_audio(tmp_path, episode=1, beat_num=5)
    manager = _FakeTaskManager()
    commands = []

    monkeypatch.setattr(video, "get_task_manager", lambda: manager)
    monkeypatch.setattr(
        video,
        "run_project_subprocess",
        _fake_probe_runner(commands, video_seconds="5.0", dub_seconds="9.4"),
    )

    result = video.run_compose_episode(
        {
            "episode": 1,
            "payload": {
                "output_dir": str(tmp_path),
                "beats": [
                    {
                        "beat_number": 5,
                        "seedance2_config_json": '{"generate_audio": true}',
                    }
                ],
            },
        },
        _ctx(tmp_path),
    )

    clip_cmd = _clip_command(commands, 5)
    assert result["video_path"].endswith("videos/episodes/ep001_final.mp4")
    # Seedance 2.0 原生对白优先：不再被独立配音覆盖，也不再用 -shortest 硬截。
    assert not any(str(part).endswith("beat_05.mp3") for part in clip_cmd)
    assert "-shortest" not in clip_cmd
    assert clip_cmd[clip_cmd.index("-map") + 1] == "0:v:0"
    assert "0:a:0" in clip_cmd
    logs = [line for _args, kwargs in manager.updates for line in kwargs.get("logs", [])]
    assert any("使用视频内置音轨" in line for line in logs)
    assert not any("使用独立音频" in line for line in logs)


def test_compose_episode_pads_dub_and_picture_to_the_longer_side(
    monkeypatch,
    tmp_path,
):
    from novelvideo.task_backend.runners import video

    _write_beat_video(tmp_path, episode=1, beat_num=2)
    _write_beat_audio(tmp_path, episode=1, beat_num=2)
    manager = _FakeTaskManager()
    commands = []

    monkeypatch.setattr(video, "get_task_manager", lambda: manager)
    monkeypatch.setattr(
        video,
        "run_project_subprocess",
        _fake_probe_runner(commands, video_seconds="5.0", dub_seconds="8.4"),
    )

    video.run_compose_episode(
        {
            "episode": 1,
            "payload": {
                "output_dir": str(tmp_path),
                "beats": [{"beat_number": 2}],
            },
        },
        _ctx(tmp_path),
    )

    clip_cmd = _clip_command(commands, 2)
    joined = " ".join(clip_cmd)
    assert "-shortest" not in clip_cmd
    assert "tpad=stop_mode=clone:stop_duration=3.400" in joined
    assert clip_cmd[clip_cmd.index("-t") + 1] == "8.400"
    assert clip_cmd[clip_cmd.index("-map") + 1] == "[v]"
    assert any(str(part).endswith("beat_02.mp3") for part in clip_cmd)


def test_compose_episode_keeps_truncating_when_durations_cannot_be_probed(
    monkeypatch,
    tmp_path,
):
    from novelvideo.task_backend.runners import video

    _write_beat_video(tmp_path, episode=1, beat_num=2)
    _write_beat_audio(tmp_path, episode=1, beat_num=2)
    manager = _FakeTaskManager()
    commands = []

    monkeypatch.setattr(video, "get_task_manager", lambda: manager)
    monkeypatch.setattr(
        video,
        "run_project_subprocess",
        _fake_probe_runner(commands, video_seconds="0", dub_seconds="0"),
    )

    video.run_compose_episode(
        {
            "episode": 1,
            "payload": {
                "output_dir": str(tmp_path),
                "beats": [{"beat_number": 2}],
            },
        },
        _ctx(tmp_path),
    )

    clip_cmd = _clip_command(commands, 2)
    # 探测失败时保留旧的收口方式，避免 apad 产出无限时长。
    assert "-shortest" in clip_cmd
    assert not any("tpad" in str(part) for part in clip_cmd)
