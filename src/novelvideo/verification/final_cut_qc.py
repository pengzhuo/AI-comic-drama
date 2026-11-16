"""成片级质检：给整集最终成片做一次客观体检。

逐镜一致性闸门（``video_consistency_gate``）管的是「单镜好不好」，评审
（``episode_reviewer``）看的是草图九宫格；但用户打开成片时第一眼看出的是
**成片本身**的规格与听感问题：

- 画幅被拉伸或加黑边（2:3 首帧塞进 9:16）；
- 帧率不齐（24/25/30 混切）；
- 码率偏低，暗部糊成一块；
- 响度忽大忽小，或整体过轻/过响；
- 有画面没声音、或有声音却是静音；
- 长时间黑屏或冻帧。

这些只能对最终 mp4 量。这里只做确定性、可复现的 ffprobe/ffmpeg 量测，不调
模型，方便单测：量出 → 按 :class:`FinalCutSpec` 判 → 输出人话 issue 列表。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


#: 成片规格默认值：720p 竖屏 / 24fps / 流媒体响度口径。
#: 与 ``task_backend/runners/video.py`` 的 compose 输出规格保持一致；两处都改时
#: 要一起动（compose 负责产出，这里负责验收）。
@dataclass(frozen=True)
class FinalCutSpec:
    width: int = 720
    height: int = 1280
    fps: float = 24.0
    fps_tolerance: float = 0.5
    #: 低于此码率按「偏糊」报警（720p 竖屏）。
    min_video_bitrate_bps: int = 2_000_000
    #: 综合响度目标与容差（LU）。
    target_loudness_lufs: float = -14.0
    loudness_tolerance_lu: float = 2.5
    #: 真峰值上限（dBTP）；超过就有转码削波风险。
    max_true_peak_dbtp: float = -1.0
    #: 单段黑屏 / 冻帧超过该秒数视为缺陷（有意转场黑屏一般 <1s）。
    max_black_segment_seconds: float = 3.0
    max_freeze_segment_seconds: float = 4.0
    #: 成片短于此秒数视为异常。
    min_duration_seconds: float = 1.0


@dataclass
class FinalCutReport:
    """一次成片体检的量测结果。"""

    path: str
    exists: bool = False
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration_seconds: float = 0.0
    video_bitrate_bps: int = 0
    video_codec: str = ""
    has_audio: bool = False
    audio_codec: str = ""
    audio_sample_rate: int = 0
    audio_channels: int = 0
    loudness_lufs: float | None = None
    true_peak_dbtp: float | None = None
    black_segments: list[float] = field(default_factory=list)
    freeze_segments: list[float] = field(default_factory=list)
    probe_error: str = ""
    issues: list[str] = field(default_factory=list)

    @property
    def longest_black_seconds(self) -> float:
        return max(self.black_segments, default=0.0)

    @property
    def longest_freeze_seconds(self) -> float:
        return max(self.freeze_segments, default=0.0)

    @property
    def ok(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict:
        data = asdict(self)
        data["longest_black_seconds"] = self.longest_black_seconds
        data["longest_freeze_seconds"] = self.longest_freeze_seconds
        data["ok"] = self.ok
        return data

    def summary_lines(self) -> list[str]:
        """一行可读摘要 + 每条 issue 一行，便于直接写进任务日志。"""

        headline = (
            f"成片质检 {self.width}x{self.height} @{self.fps:.2f}fps "
            f"{self.duration_seconds:.1f}s "
            f"{self.video_bitrate_bps / 1_000_000:.2f}Mbps "
            f"音频={'有' if self.has_audio else '无'}"
        )
        if self.loudness_lufs is not None:
            headline += f" 响度={self.loudness_lufs:.1f}LUFS"
        if self.true_peak_dbtp is not None:
            headline += f" 峰值={self.true_peak_dbtp:.1f}dBTP"
        lines = [headline]
        lines.extend(f"成片质检问题: {issue}" for issue in self.issues)
        return lines


def _run_capture(cmd: list[str], *, timeout: int) -> tuple[int, str, str]:
    """跑一条命令，返回 ``(returncode, stdout, stderr)``；异常返回 ``(-1, "", "")``。"""

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 - 探测失败按「量不到」处理，不阻断产线
        return -1, "", ""
    return int(result.returncode), result.stdout or "", result.stderr or ""


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _ffprobe() -> str | None:
    return shutil.which("ffprobe")


def _parse_fraction(raw: object) -> float:
    text = str(raw or "").strip()
    if not text:
        return 0.0
    if "/" in text:
        numerator, _, denominator = text.partition("/")
        try:
            den = float(denominator)
            return float(numerator) / den if den else 0.0
        except ValueError:
            return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _to_int(raw: object) -> int:
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return 0


def _probe_streams(path: Path) -> dict:
    ffprobe = _ffprobe()
    if ffprobe is None:
        return {}
    code, stdout, _stderr = _run_capture(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=120,
    )
    if code != 0:
        return {}
    try:
        return json.loads(stdout)
    except Exception:  # noqa: BLE001
        return {}


def _last_float(pattern: str, text: str) -> float | None:
    matches = list(re.finditer(pattern, text))
    if not matches:
        return None
    try:
        return float(matches[-1].group(1))
    except ValueError:  # pragma: no cover - 正则已限定数字
        return None


def _measure_loudness(path: Path) -> tuple[float | None, float | None]:
    """用 ebur128 量综合响度与真峰值。

    ebur128 在每一帧都往 stderr 打一行进度（``... I: -70.0 LUFS ...``），片头
    还没积累够音频时这个中间值就是 -70，收敛后才变准。真正的结论在片尾的
    ``Summary`` 块里，所以只能读 Summary，绝不能取第一条命中。
    """

    ffmpeg = _ffmpeg()
    if ffmpeg is None:
        return None, None
    _code, _stdout, stderr = _run_capture(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-af",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ],
        timeout=600,
    )
    summary = stderr.rsplit("Integrated loudness:", 1)[-1]
    loudness = _last_float(r"\bI:\s*(-?\d+(?:\.\d+)?)\s*LUFS", summary)
    peak = _last_float(r"\bPeak:\s*(-?\d+(?:\.\d+)?)\s*dBFS", summary)
    return loudness, peak


def _detect_segments(path: Path, filter_expr: str, key: str) -> list[float]:
    ffmpeg = _ffmpeg()
    if ffmpeg is None:
        return []
    _code, _stdout, stderr = _run_capture(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-vf",
            filter_expr,
            "-an",
            "-f",
            "null",
            "-",
        ],
        timeout=600,
    )
    pattern = re.compile(rf"{key}:(\d+(?:\.\d+)?)")
    return [float(match.group(1)) for match in pattern.finditer(stderr)]


def inspect_final_cut(path: Path | str, spec: FinalCutSpec | None = None) -> FinalCutReport:
    """对成片做一次完整体检（量测 + 判据）。"""

    spec = spec or FinalCutSpec()
    target = Path(path)
    report = FinalCutReport(path=str(target))
    if not target.exists():
        report.probe_error = "成片文件不存在"
        report.issues.append(report.probe_error)
        return report
    report.exists = True

    streams = _probe_streams(target)
    if not streams:
        report.probe_error = "ffprobe 量测失败"
        report.issues.append(report.probe_error)
        return report

    fmt = streams.get("format") or {}
    report.duration_seconds = _parse_fraction(fmt.get("duration"))
    report.video_bitrate_bps = _to_int(fmt.get("bit_rate"))

    video_streams = [s for s in streams.get("streams", []) if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams.get("streams", []) if s.get("codec_type") == "audio"]
    if video_streams:
        video = video_streams[0]
        report.width = _to_int(video.get("width"))
        report.height = _to_int(video.get("height"))
        report.fps = _parse_fraction(video.get("avg_frame_rate") or video.get("r_frame_rate"))
        report.video_codec = str(video.get("codec_name") or "")
        stream_bitrate = _to_int(video.get("bit_rate"))
        if stream_bitrate:
            report.video_bitrate_bps = stream_bitrate
    if audio_streams:
        audio = audio_streams[0]
        report.has_audio = True
        report.audio_codec = str(audio.get("codec_name") or "")
        report.audio_sample_rate = _to_int(audio.get("sample_rate"))
        report.audio_channels = _to_int(audio.get("channels"))

    if report.has_audio:
        report.loudness_lufs, report.true_peak_dbtp = _measure_loudness(target)
    report.black_segments = _detect_segments(
        target, "blackdetect=d=0.1:pix_th=0.10", "black_duration"
    )
    report.freeze_segments = _detect_segments(
        target, "freezedetect=n=-60dB:d=2", "freeze_duration"
    )

    report.issues = evaluate_report(report, spec)
    return report


def evaluate_report(report: FinalCutReport, spec: FinalCutSpec | None = None) -> list[str]:
    """按规格判定问题清单（纯函数，方便单测）。"""

    spec = spec or FinalCutSpec()
    issues: list[str] = []
    if not report.exists:
        return ["成片文件不存在"]
    if report.probe_error:
        return [report.probe_error]
    if report.width and (report.width, report.height) != (spec.width, spec.height):
        issues.append(
            f"画幅 {report.width}x{report.height} 与规格 {spec.width}x{spec.height} 不一致"
            "（会被拉伸或留黑边）"
        )
    if report.fps and abs(report.fps - spec.fps) > spec.fps_tolerance:
        issues.append(f"帧率 {report.fps:.2f} 与规格 {spec.fps:.2f} 不一致")
    if report.duration_seconds < spec.min_duration_seconds:
        issues.append(f"成片时长 {report.duration_seconds:.2f}s 过短")
    if report.video_bitrate_bps and report.video_bitrate_bps < spec.min_video_bitrate_bps:
        issues.append(
            f"视频码率 {report.video_bitrate_bps / 1_000_000:.2f}Mbps 偏低"
            f"（低于 {spec.min_video_bitrate_bps / 1_000_000:.1f}Mbps）"
        )
    if not report.has_audio:
        issues.append("成片没有音轨")
    if (
        report.loudness_lufs is not None
        and abs(report.loudness_lufs - spec.target_loudness_lufs) > spec.loudness_tolerance_lu
    ):
        issues.append(
            f"综合响度 {report.loudness_lufs:.1f}LUFS 偏离目标 "
            f"{spec.target_loudness_lufs:.1f}LUFS（容差 ±{spec.loudness_tolerance_lu:.1f}）"
        )
    if report.true_peak_dbtp is not None and report.true_peak_dbtp > spec.max_true_peak_dbtp:
        issues.append(
            f"真峰值 {report.true_peak_dbtp:.1f}dBTP 超过上限 {spec.max_true_peak_dbtp:.1f}dBTP"
        )
    if report.longest_black_seconds > spec.max_black_segment_seconds:
        issues.append(
            f"最长黑屏 {report.longest_black_seconds:.1f}s 超过 "
            f"{spec.max_black_segment_seconds:.1f}s"
        )
    if report.longest_freeze_seconds > spec.max_freeze_segment_seconds:
        issues.append(
            f"最长冻帧 {report.longest_freeze_seconds:.1f}s 超过 "
            f"{spec.max_freeze_segment_seconds:.1f}s"
        )
    return issues


def qc_sidecar_path(video_path: Path | str) -> Path:
    """质检结果落点：与成片同目录的 ``*_final.qc.json``。"""

    target = Path(video_path)
    return target.with_suffix("").parent / f"{target.stem}.qc.json"


def write_qc_sidecar(report: FinalCutReport, video_path: Path | str) -> Path | None:
    """把体检结果写成 sidecar json；写失败只记日志，不影响成片。"""

    sidecar = qc_sidecar_path(video_path)
    try:
        sidecar.write_text(
            json.dumps(report.as_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        logger.warning("成片质检 sidecar 写入失败: %s", sidecar)
        return None
    return sidecar
