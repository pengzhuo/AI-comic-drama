"""视频一致性自检：重跑用尽时选「最接近首帧」的一版，并落下人工复核标记。

回归的是三个静默失效点：

1. 重跑用尽后原本盲留最后一版（可能比上一版更差）；
2. 首帧画幅与请求画幅不一致没有任何提示（视频模型补画布 = 穿帮入口）；
3. 严格首帧通路被上游拒绝、降级为多参考通路时不留痕。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from novelvideo.task_backend.runners import video as video_runner
from novelvideo.verification import video_consistency_gate as gate_module
from novelvideo.verification.video_consistency_gate import (
    VideoConsistencyVerdict,
    audio_track_state,
    frame_aspect_warning,
    parse_aspect_ratio,
    pick_best_attempt,
)


def test_parse_aspect_ratio_accepts_common_forms() -> None:
    assert parse_aspect_ratio("9:16") == pytest.approx(9 / 16)
    assert parse_aspect_ratio("16:9") == pytest.approx(16 / 9)
    assert parse_aspect_ratio("1x1") == pytest.approx(1.0)
    assert parse_aspect_ratio("") is None
    assert parse_aspect_ratio("portrait") is None
    assert parse_aspect_ratio("9:0") is None


def test_frame_aspect_warning_flags_a_mismatch(tmp_path: Path) -> None:
    from PIL import Image

    portrait = tmp_path / "frame.png"
    Image.new("RGB", (688, 1024)).save(portrait)

    warning = frame_aspect_warning(portrait, "9:16")
    assert "不一致" in warning
    assert "688x1024" in warning

    # 2:3 首帧本身就是 9:16 请求的老问题：不能静默。
    assert frame_aspect_warning(portrait, "2:3") == ""
    assert frame_aspect_warning(portrait, "") == ""
    assert frame_aspect_warning(tmp_path / "missing.png", "9:16") == ""


def test_pick_best_attempt_prefers_fewer_reasons_then_higher_similarity() -> None:
    attempts = [
        {"attempt": 1, "passed": False, "reasons": ["a", "b"], "frame0_similarity": 0.95},
        {"attempt": 2, "passed": False, "reasons": ["a"], "frame0_similarity": 0.5},
    ]
    assert pick_best_attempt(attempts)["attempt"] == 2

    same_reason_count = [
        {"attempt": 1, "passed": False, "reasons": ["a"], "frame0_similarity": 0.5},
        {"attempt": 2, "passed": False, "reasons": ["a"], "frame0_similarity": 0.8},
    ]
    assert pick_best_attempt(same_reason_count)["attempt"] == 2

    unmeasured = [
        {"attempt": 1, "passed": False, "reasons": [], "frame0_similarity": None},
        {"attempt": 2, "passed": False, "reasons": [], "frame0_similarity": None},
    ]
    # 都没有硬问题时偏向更晚生成的一版（强化提示词之后的那次）。
    assert pick_best_attempt(unmeasured)["attempt"] == 2
    assert pick_best_attempt([]) is None


class _FakeRun:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.stderr = ""


def _patch_ffmpeg(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    monkeypatch.setattr(gate_module, "_ffprobe", lambda: "ffprobe")
    monkeypatch.setattr(gate_module, "_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(gate_module.subprocess, "run", lambda *_a, **_k: _FakeRun(stdout))


def test_audio_track_state_detects_missing_and_silent_tracks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    video = tmp_path / "beat.mp4"
    video.write_bytes(b"x")

    assert audio_track_state(tmp_path / "missing.mp4") == "unknown"

    # 没有音轨：ffprobe 的流列表为空。
    _patch_ffmpeg(monkeypatch, "")
    assert audio_track_state(video) == "missing"

    # 有音轨但接近静音（audio 流列表 + volumedetect 的 mean_volume）。
    _patch_ffmpeg(monkeypatch, "audio\nmean_volume: -70.0 dB")
    assert audio_track_state(video) == "silent"

    _patch_ffmpeg(monkeypatch, "audio\nmean_volume: -21.3 dB")
    assert audio_track_state(video) == "audible"

    # 解析不出音量时按未知处理，不能把整条产线卡在自检上。
    _patch_ffmpeg(monkeypatch, "audio\nno volume info")
    assert audio_track_state(video) == "unknown"


def test_beat_expects_speech_only_for_spoken_beats() -> None:
    assert video_runner._beat_expects_speech(
        {"audio_type": "dialogue", "dialogue": "今天是我的生日。"}
    )
    assert video_runner._beat_expects_speech(
        {"audio_type": "narration", "narration": "雨下了一整夜。"}
    )
    # 动作 / 静音 beat 本来就该没有人声。
    assert not video_runner._beat_expects_speech(
        {"audio_type": "action", "narration": "他转身离开。"}
    )
    # 有音频类型但没有文本，不构成「必须有声音」。
    assert not video_runner._beat_expects_speech({"audio_type": "dialogue"})


def test_write_review_flags_accumulates_and_prunes(tmp_path: Path) -> None:
    flags_path = tmp_path / "review_flags.json"

    video_runner.write_review_flags(
        videos_dir=tmp_path,
        beat_num=3,
        report={
            "needs_human_review": True,
            "review_reasons": ["服装与首帧不一致"],
            "final_attempt": 3,
            "frame0_similarity": 0.5,
            "strict_first_frame_degraded": True,
        },
    )
    video_runner.write_review_flags(
        videos_dir=tmp_path,
        beat_num=4,
        report={
            "needs_human_review": True,
            "review_reasons": ["脸型与首帧不一致"],
            "final_attempt": 3,
            "frame0_similarity": 0.6,
        },
    )

    payload = json.loads(flags_path.read_text(encoding="utf-8"))
    assert payload["schema"] == "beat_review_flags_v1"
    assert sorted(payload["beats"]) == ["3", "4"]
    assert payload["beats"]["3"]["strict_first_frame_degraded"] is True
    assert payload["beats"]["4"]["reasons"] == ["脸型与首帧不一致"]

    # 重跑通过后该 beat 的标记要被清掉，不能留下过期的人工复核。
    video_runner.write_review_flags(
        videos_dir=tmp_path, beat_num=4, report={"needs_human_review": False}
    )
    payload = json.loads(flags_path.read_text(encoding="utf-8"))
    assert sorted(payload["beats"]) == ["3"]

    video_runner.write_review_flags(
        videos_dir=tmp_path, beat_num=3, report={"needs_human_review": False}
    )
    assert not flags_path.exists()


class _FakeVideoGen:
    def __init__(self, video_path: Path) -> None:
        self._video_path = video_path
        self.calls = 0

    async def generate(self, **_kwargs):
        self.calls += 1
        self._video_path.write_bytes(f"attempt{self.calls + 1}".encode())
        return SimpleNamespace(status=SimpleNamespace(value="done"), error="")


@pytest.mark.asyncio
async def test_gate_keeps_the_best_attempt_when_retries_run_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    video_path = tmp_path / "beat_01.mp4"
    video_path.write_bytes(b"attempt1")

    verdicts = [
        VideoConsistencyVerdict(passed=False, reasons=["服装"], frame0_similarity=0.92),
        VideoConsistencyVerdict(
            passed=False, reasons=["服装", "脸型"], frame0_similarity=0.4
        ),
    ]
    calls: list[int] = []

    class _Gate:
        async def check(self, *, video_path: Path, **_kwargs):
            calls.append(len(calls) + 1)
            return verdicts[len(calls) - 1]

    monkeypatch.setattr(gate_module, "VideoConsistencyGate", _Gate)

    logs: list[str] = []
    video_gen = _FakeVideoGen(video_path)
    result = SimpleNamespace(status=SimpleNamespace(value="done"), error="")

    current, report = await video_runner._apply_video_consistency_gate(
        video_gen=video_gen,
        generate_kwargs={"prompt": "原始提示词"},
        result=result,
        frame_path=tmp_path / "frame.png",
        video_path=video_path,
        beat={},
        output_dir=str(tmp_path),
        payload_config={"video_consistency_attempts": 2},
        on_log=logs.append,
        beat_num=1,
    )

    assert video_gen.calls == 1
    # 第 1 版硬问题更少 —— 落盘的必须是它，而不是更差的第 2 版。
    assert video_path.read_bytes() == b"attempt1"
    assert report["passed"] is False
    # 报告描述的是「真正落盘的那一版」，不是最后一次生成的那版。
    assert report["final_attempt"] == 1
    assert report["frame0_similarity"] == 0.92
    assert report["needs_human_review"] is True
    assert report["review_reasons"] == ["服装"]
    assert any("最接近首帧" in line for line in logs)

    flags = json.loads((tmp_path / "review_flags.json").read_text(encoding="utf-8"))
    assert flags["beats"]["1"]["reasons"] == ["服装"]


@pytest.mark.asyncio
async def test_gate_reports_strict_first_frame_degradation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    video_path = tmp_path / "beat_02.mp4"
    video_path.write_bytes(b"only")

    class _Gate:
        async def check(self, **_kwargs):
            return VideoConsistencyVerdict(passed=True, frame0_similarity=0.99)

    monkeypatch.setattr(gate_module, "VideoConsistencyGate", _Gate)

    logs: list[str] = []
    current, report = await video_runner._apply_video_consistency_gate(
        video_gen=_FakeVideoGen(video_path),
        generate_kwargs={"prompt": "p"},
        result=SimpleNamespace(status=SimpleNamespace(value="done"), error=""),
        frame_path=tmp_path / "frame.png",
        video_path=video_path,
        beat={},
        output_dir=str(tmp_path),
        payload_config={"video_consistency_attempts": 1},
        on_log=logs.append,
        strict_first_frame_degraded=True,
        beat_num=2,
    )

    assert report["strict_first_frame_degraded"] is True
    # 画面本身通过了，但「严格首帧实际没生效」仍要留痕，不能静默。
    assert report["needs_human_review"] is False
    assert any("严格首帧未生效" in line for line in logs)
    flags = json.loads((tmp_path / "review_flags.json").read_text(encoding="utf-8"))
    assert flags["beats"]["2"]["strict_first_frame_degraded"] is True
