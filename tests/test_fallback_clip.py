from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from novelvideo.generators.fallback_clip import render_still_clip


def test_render_still_clip_without_a_frame_is_a_noop(tmp_path: Path) -> None:
    """首帧不存在时不报错、不产出（调用方据此把 degraded 记为 False）。"""

    out = tmp_path / "beat_12.mp4"
    assert render_still_clip(tmp_path / "missing.png", out) is False
    assert not out.exists()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_render_still_clip_produces_a_playable_mp4(tmp_path: Path) -> None:
    """有 ffmpeg 时，静帧应渲染成一支非空的短片。"""

    import subprocess

    frame = tmp_path / "frame.png"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x96",
            "-frames:v",
            "1",
            str(frame),
        ],
        capture_output=True,
        check=True,
    )
    out = tmp_path / "beat_12.mp4"
    assert render_still_clip(frame, out, duration=1.0, width=72, height=128, fps=12) is True
    assert out.is_file() and out.stat().st_size > 0
