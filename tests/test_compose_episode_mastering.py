"""整集合成的成片规格与响度母带（P0）。

现场问题：成片只有 1.4Mbps / CRF23、没有响度归一，各镜音量忽大忽小，观感比
专业短剧差一档。这里锁定成片输出规格（720p/@24fps/CRF18）与响度母带
（loudnorm + alimiter），避免回归。
"""

from types import SimpleNamespace


class _FakeTaskManager:
    def update_progress_for_project(self, *args, **kwargs):
        pass


def _write_beat_video(project_dir, episode: int, beat_num: int) -> None:
    video_dir = project_dir / "videos" / "beats" / f"ep{episode:03d}"
    video_dir.mkdir(parents=True, exist_ok=True)
    (video_dir / f"beat_{beat_num:02d}.mp4").write_bytes(b"video")


def _run_compose(tmp_path, monkeypatch) -> list[list[str]]:
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
                    {"beat_number": 1, "audio_type": "dialogue", "speaker": "甲"},
                    {"beat_number": 2, "audio_type": "silence", "speaker": ""},
                ],
            },
        },
        SimpleNamespace(output_dir=tmp_path),
    )
    return commands


def _final_command(commands: list[list[str]]) -> list[str]:
    return next(
        cmd
        for cmd in reversed(commands)
        if cmd and cmd[0] == "ffmpeg" and cmd[-1].endswith("ep001_final.mp4")
    )


def test_final_encode_targets_720p_24fps_crf18(tmp_path, monkeypatch):
    final_cmd = _final_command(_run_compose(tmp_path, monkeypatch))

    assert final_cmd[final_cmd.index("-crf") + 1] == "18"
    assert final_cmd[final_cmd.index("-r") + 1] == "24"
    assert final_cmd[final_cmd.index("-b:a") + 1] == "192k"
    assert final_cmd[final_cmd.index("-ar") + 1] == "44100"
    assert final_cmd[final_cmd.index("-ac") + 1] == "2"


def test_final_filter_mastering_and_uniform_fps(tmp_path, monkeypatch):
    final_cmd = _final_command(_run_compose(tmp_path, monkeypatch))
    graph = final_cmd[final_cmd.index("-filter_complex") + 1]

    assert "loudnorm=I=-14.0:TP=-1.5:LRA=11.0" in graph
    # 限幅必须在重采样之后：先限幅再降采样会把峰值重新顶起来。
    assert "aresample=44100,alimiter=limit=" in graph
    assert graph.count("fps=24") == 2  # 逐 clip 归一化
    assert "-map" in final_cmd
    assert final_cmd[final_cmd.index("-map") + 1] == "[outv]"
    assert "[aout]" in final_cmd


def test_clip_normalization_is_24fps_crf18(tmp_path, monkeypatch):
    commands = _run_compose(tmp_path, monkeypatch)
    clip_cmd = next(
        cmd for cmd in commands if cmd[0] == "ffmpeg" and cmd[-1].endswith("beat_0001.mp4")
    )

    assert clip_cmd[clip_cmd.index("-crf") + 1] == "18"
    assert clip_cmd[clip_cmd.index("-r") + 1] == "24"


def _write_audio_design(tmp_path) -> None:
    import json

    cue_dir = tmp_path / "audio" / "design" / "ep001"
    cue_dir.mkdir(parents=True, exist_ok=True)
    (cue_dir / "bgm_bgm.mp3").write_bytes(b"audio")
    (cue_dir / "ambience_amb0.mp3").write_bytes(b"audio")
    (cue_dir / "sfx_sfx0.mp3").write_bytes(b"audio")
    (cue_dir / "plan.json").write_text(
        json.dumps(
            {
                "cues": [
                    {
                        "cue_id": "bgm",
                        "kind": "bgm",
                        "start_seconds": 0.0,
                        "end_seconds": 10.0,
                        "audio_path": "audio/design/ep001/bgm_bgm.mp3",
                        "gain_db": -13.0,
                    },
                    {
                        "cue_id": "amb0",
                        "kind": "ambience",
                        "start_seconds": 4.0,
                        "end_seconds": 10.0,
                        "audio_path": "audio/design/ep001/ambience_amb0.mp3",
                        "gain_db": -17.0,
                    },
                    {
                        "cue_id": "sfx0",
                        "kind": "sfx",
                        "start_seconds": 2.5,
                        "end_seconds": 5.5,
                        "audio_path": "audio/design/ep001/sfx_sfx0.mp3",
                        "gain_db": -5.0,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )


def test_compose_mixes_audio_design_beds_and_sfx(tmp_path, monkeypatch):
    _write_audio_design(tmp_path)
    final_cmd = _final_command(_run_compose(tmp_path, monkeypatch))
    graph = final_cmd[final_cmd.index("-filter_complex") + 1]

    # 三条 cue 音轨作为额外输入接入。
    joined = " ".join(str(part) for part in final_cmd)
    assert "bgm_bgm.mp3" in joined
    assert "ambience_amb0.mp3" in joined
    assert "sfx_sfx0.mp3" in joined
    # 配乐+环境音合成 bed，再被人声侧链压下去；音效不参与闪避。
    assert "sidechaincompress=threshold=0.06" in graph
    assert "amix=inputs=2:duration=longest:normalize=0[beds]" in graph
    assert graph.count("adelay=") == 3
    assert "[voice][bedduck][cue" in graph
    assert graph.endswith("aresample=44100[aout]") or "[aout]" in graph
    assert final_cmd[len(final_cmd) - 1 - final_cmd[::-1].index("-map") + 1] == "[aout]"


def test_compose_without_audio_design_keeps_plain_mastering(tmp_path, monkeypatch):
    final_cmd = _final_command(_run_compose(tmp_path, monkeypatch))
    graph = final_cmd[final_cmd.index("-filter_complex") + 1]

    assert "sidechaincompress" not in graph
    assert "[outa]loudnorm=I=-14.0" in graph
