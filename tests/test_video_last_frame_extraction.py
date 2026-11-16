"""单测：厂商不回尾帧时本地抽帧兜底（滚动咬合）。

``newapi_seedance-2.5`` 走 seedance 网关，只回视频 URL、不回尾帧。相邻 beat 首尾咬合
依赖上一 beat 成片的尾帧当下一 beat 的首帧，所以生成器要在厂商没给尾帧时，用 ffmpeg
从成片本地抽最后一帧落到 ``returned_last_frames/<stem>.png``。抽帧失败时返回空串，
调用方会退回项目渲染首帧，不会阻塞出片。
"""

from __future__ import annotations

from pathlib import Path

from novelvideo.generators.video_generator import (
    HuimengVideoGenerator,
    NewApiVideoGenerator,
)


def _video(tmp_path: Path) -> Path:
    path = tmp_path / "videos" / "beats" / "ep001" / "beat_01.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake-video")
    return path


def _writer_ffmpeg(*, fail_first: bool = False):
    calls = {"n": 0}

    def fake_run(args, **kwargs):
        calls["n"] += 1
        if fail_first and calls["n"] == 1:
            raise RuntimeError("ffmpeg: bad -sseof")
        dst = Path(args[-1])
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"PNG")

    return fake_run


def test_extract_local_last_frame_writes_returned_last_frames(tmp_path, monkeypatch):
    video = _video(tmp_path)
    monkeypatch.setattr(
        "novelvideo.generators.video_generator.subprocess.run", _writer_ffmpeg()
    )

    result = NewApiVideoGenerator._extract_local_last_frame(str(video))

    expected = video.parent / "returned_last_frames" / "beat_01.png"
    assert result == expected.as_posix()
    assert expected.is_file()


def test_extract_local_last_frame_retries_without_sseof(tmp_path, monkeypatch):
    video = _video(tmp_path)
    monkeypatch.setattr(
        "novelvideo.generators.video_generator.subprocess.run",
        _writer_ffmpeg(fail_first=True),
    )

    result = HuimengVideoGenerator._extract_local_last_frame(str(video))

    assert result.endswith("returned_last_frames/beat_01.png")
    assert Path(result).is_file()


def test_extract_local_last_frame_missing_video_returns_empty(tmp_path):
    assert NewApiVideoGenerator._extract_local_last_frame("") == ""
    assert (
        HuimengVideoGenerator._extract_local_last_frame(
            str(tmp_path / "nope" / "beat_01.mp4")
        )
        == ""
    )


def test_extract_local_last_frame_ffmpeg_failure_returns_empty(tmp_path, monkeypatch):
    video = _video(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("ffmpeg not found")

    monkeypatch.setattr("novelvideo.generators.video_generator.subprocess.run", boom)
    assert NewApiVideoGenerator._extract_local_last_frame(str(video)) == ""
