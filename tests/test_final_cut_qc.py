"""成片级质检（``verification.final_cut_qc``）单测：量测解析 + 判据。"""

import json
from pathlib import Path

from novelvideo.verification import final_cut_qc as qc


def _clean_report() -> qc.FinalCutReport:
    return qc.FinalCutReport(
        path="ep001_final.mp4",
        exists=True,
        width=720,
        height=1280,
        fps=24.0,
        duration_seconds=147.1,
        video_bitrate_bps=3_200_000,
        has_audio=True,
        audio_sample_rate=44100,
        audio_channels=2,
        loudness_lufs=-14.2,
        true_peak_dbtp=-1.6,
    )


def test_clean_report_has_no_issues():
    assert qc.evaluate_report(_clean_report()) == []


def test_evaluate_flags_spec_deviations():
    report = _clean_report()
    report.width, report.height = 1024, 1536
    report.fps = 30.0
    report.video_bitrate_bps = 1_000_000
    report.loudness_lufs = -22.0
    report.true_peak_dbtp = 0.5
    report.black_segments = [4.5]
    report.freeze_segments = [6.0]

    issues = qc.evaluate_report(report)
    joined = "\n".join(issues)
    assert "画幅" in joined
    assert "帧率" in joined
    assert "码率" in joined
    assert "综合响度" in joined
    assert "真峰值" in joined
    assert "黑屏" in joined
    assert "冻帧" in joined


def test_evaluate_flags_missing_audio():
    report = _clean_report()
    report.has_audio = False
    report.loudness_lufs = None
    assert any("音轨" in issue for issue in qc.evaluate_report(report))


def test_missing_file_reports_issue():
    report = qc.inspect_final_cut(Path("does_not_exist_ep001_final.mp4"))
    assert report.exists is False
    assert report.issues


def test_inspect_final_cut_populates_from_probes(monkeypatch, tmp_path):
    film = tmp_path / "ep001_final.mp4"
    film.write_bytes(b"x")

    monkeypatch.setattr(
        qc,
        "_probe_streams",
        lambda _path: {
            "format": {"duration": "147.1", "bit_rate": "3200000"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 720,
                    "height": 1280,
                    "avg_frame_rate": "24/1",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "44100",
                    "channels": 2,
                },
            ],
        },
    )
    monkeypatch.setattr(qc, "_measure_loudness", lambda _path: (-14.1, -1.7))
    monkeypatch.setattr(qc, "_detect_segments", lambda *a, **k: [0.4])

    report = qc.inspect_final_cut(film)
    assert report.width == 720
    assert report.height == 1280
    assert report.fps == 24.0
    assert report.duration_seconds == 147.1
    assert report.video_bitrate_bps == 3_200_000
    assert report.has_audio is True
    assert report.loudness_lufs == -14.1
    assert report.true_peak_dbtp == -1.7
    assert report.issues == []
    assert report.ok is True


def test_inspect_final_cut_survives_probe_failure(monkeypatch, tmp_path):
    film = tmp_path / "ep001_final.mp4"
    film.write_bytes(b"x")
    monkeypatch.setattr(qc, "_probe_streams", lambda _path: {})

    report = qc.inspect_final_cut(film)
    assert report.exists is True
    assert report.issues == ["ffprobe 量测失败"]


def test_write_qc_sidecar(tmp_path):
    film = tmp_path / "videos" / "episodes" / "ep001_final.mp4"
    film.parent.mkdir(parents=True)
    film.write_bytes(b"x")

    sidecar = qc.write_qc_sidecar(_clean_report(), film)
    assert sidecar == film.with_suffix("").parent / "ep001_final.qc.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["longest_black_seconds"] == 0.0


def test_parse_loudness_and_segments_from_ffmpeg_output(monkeypatch, tmp_path):
    # ebur128 片头会先打一堆 -70.0 的中间值；只有 Summary 里的才是结论。
    ffmpeg_output = (
        "[Parsed_ebur128_0 @ 0x1] t: 0.1 TARGET:-23 LUFS    M: -70.0 S: -70.0 "
        "    I: -70.0 LUFS       LRA:   0.0 LU\n"
        "[Parsed_ebur128_0 @ 0x1] t: 1.1 TARGET:-23 LUFS    M: -30.0 S: -70.0 "
        "    I: -70.0 LUFS       LRA:   0.0 LU\n"
        "[Parsed_ebur128_0 @ 0x1] t: 147.1 TARGET:-23 LUFS    M: -14.6 S: -16.2 "
        "    I: -16.9 LUFS       LRA:   8.7 LU  TPK:  -1.5  -1.6 dBFS\n"
        "[Parsed_ebur128_0 @ 0x1] Summary:\n"
        "\n"
        "  Integrated loudness:\n"
        "    I:         -14.2 LUFS\n"
        "    Threshold: -29.1 LUFS\n"
        "\n"
        "  Loudness range:\n"
        "    LRA:         8.7 LU\n"
        "    Threshold: -39.1 LUFS\n"
        "    LRA low:   -24.4 LUFS\n"
        "    LRA high:  -15.6 LUFS\n"
        "\n"
        "  True peak:\n"
        "    Peak:      -1.6 dBFS\n"
    )

    def fake_run(cmd, **_kwargs):
        from types import SimpleNamespace

        if "-af" in cmd and "ebur128=peak=true" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr=ffmpeg_output)
        if "blackdetect" in " ".join(cmd):
            return SimpleNamespace(
                returncode=0,
                stdout="",
                stderr="[blackdetect] black_start:1 black_end:2 black_duration:1.0\n"
                "[blackdetect] black_start:10 black_end:11 black_duration:1.5\n",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(qc.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(qc.subprocess, "run", fake_run)

    loudness, peak = qc._measure_loudness(tmp_path / "f.mp4")
    assert loudness == -14.2
    assert peak == -1.6

    black = qc._detect_segments(tmp_path / "f.mp4", "blackdetect", "black_duration")
    assert black == [1.0, 1.5]
