"""视频生成后置一致性自检（严格首帧 / 服装脸型 / 肢体物理）。

生成链路里的 D 环节：抽视频第 0 帧与 render 首帧做确定性比对，再用视觉模型
比对中后段帧与首帧，检查服装、脸型、景别与肢体物理穿帮。不通过时由调用方按
有界次数重跑，并把问题写进强化提示词。

设计口径：
* 只拦截「肉眼可确认」的硬问题，避免正常表演被误判导致无限重跑；
* 任何基础设施问题（缺 ffmpeg、视觉模型不可用）都按「通过+记录」处理，
  不能因为自检本身让整条产线失败。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from novelvideo.prompt_safety import sanitize_prompt_for_moderation

from .models import VideoConsistencyIssues
from .prompts import VIDEO_CONSISTENCY_VERIFY_PROMPT
from .utils import compress_image

logger = logging.getLogger(__name__)

#: 第 0 帧与首帧的确定性相似度下限：低于它说明画面被整体重做（换人/换场）。
FRAME0_HARD_FAIL_SIMILARITY = 0.45
#: 低于这个值只在报告里提示，不直接判不通过（画幅裁剪会造成温和下降）。
FRAME0_WARN_SIMILARITY = 0.9

#: 自检总开关环境变量（``0/false/off/no`` 关闭）。
GATE_ENV = "DRAMACLAW_VIDEO_CONSISTENCY_GATE"
#: 重跑次数上限（含首次生成，默认 2 次机会 = 最多重跑 1 次）。
#:
#: 从 3 降到 2 是刻意的：重跑一轮要花一次完整生成，而绝大多数重跑只是景别变紧
#: （见下面 ``MARK_ONLY_REASON_KINDS``），画面本身并没有坏。把「值得重跑的问题」
#: 收窄之后，第 3 次机会的边际收益已经低于它的时间成本。
GATE_ATTEMPTS_ENV = "DRAMACLAW_VIDEO_CONSISTENCY_ATTEMPTS"
DEFAULT_MAX_ATTEMPTS = 2

#: 问题维度。自检不再只看「有没有问题」，而是按维度决定「值不值得重跑」。
REASON_KIND_FRAME0 = "frame0"
REASON_KIND_AUDIO = "audio"
REASON_KIND_IDENTITY = "identity"
REASON_KIND_COSTUME = "costume"
REASON_KIND_PHYSICAL = "physical"
REASON_KIND_FRAMING = "framing"
REASON_KIND_OTHER = "other"

#: 会触发重跑的维度：首帧锁失效（画面被整体重做＝一致性硬指标崩了）、身份/服装
#: 道具（一致性硬指标）、声音（口型必然对不上）、肢体物理穿帮（画面确实坏了）。
#:
#: 首帧硬失配刻意放在这里：它不是「构图偏好」，而是「这一版根本没有从首帧开始
#: 渲染」——衣服/化妆/道具的锁定全靠首帧，只标记不重跑等于放任漂移过闸。
RERUN_REASON_KINDS: frozenset[str] = frozenset(
    {
        REASON_KIND_FRAME0,
        REASON_KIND_AUDIO,
        REASON_KIND_IDENTITY,
        REASON_KIND_COSTUME,
        REASON_KIND_PHYSICAL,
    }
)

#: 只标记不重跑的维度：景别/构图偏好，以及视觉模型的自由描述。这些偏差记进
#: ``review_flags.json`` 供人工判断，但不值得再花一整轮生成——也正是原先最大的
#: 重跑来源。
MARK_ONLY_REASON_KINDS: frozenset[str] = frozenset(
    {REASON_KIND_FRAMING, REASON_KIND_OTHER}
)

#: 关键字 → 维度。先出现的规则优先，所以顺序即优先级。
_REASON_KIND_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # 首帧硬失配优先于其它维度：它一旦命中，说明这一版没有从首帧开始渲染。
    (REASON_KIND_FRAME0, ("差异过大", "整体重做")),
    (REASON_KIND_AUDIO, ("音轨", "口型", "静音", "对不上")),
    (REASON_KIND_IDENTITY, ("脸型", "身份", "换脸", "五官", "长相")),
    (
        REASON_KIND_COSTUME,
        ("服装", "配饰", "换装", "衣", "道具", "发型", "发饰", "鞋"),
    ),
    (
        REASON_KIND_PHYSICAL,
        ("肢体", "姿态物理", "物理穿帮", "悬空", "贴地", "融接", "肢体缺失"),
    ),
    (REASON_KIND_FRAMING, ("景别", "构图", "取景", "镜头", "画幅", "裁剪", "特写", "重构")),
)


def classify_reason(reason: str) -> str:
    """把一个自检原因归到某个维度（无法归类时归 ``other``，即只标记）。"""

    text = str(reason or "")
    for kind, keywords in _REASON_KIND_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return kind
    return REASON_KIND_OTHER

#: 平均音量低于这个 dB 视为静音（ffmpeg volumedetect 的 ``mean_volume``）。
SILENT_MEAN_VOLUME_DB = -50.0
#: 需要真实人声的音频类型：这类 beat 缺音轨/静音，口型必然对不上。
SPEECH_AUDIO_TYPES = frozenset({"dialogue", "narration"})

#: 视觉审核走的网关路由名（可用同名环境变量覆盖具体模型）。
GATE_MODEL_ENV = "VIDEO_CONSISTENCY_GATE_MODEL"
#: 默认复用剧集链路里已经在用的视觉别名（网关已开通），
#: 避免落到普通文本模型上导致「模型不存在」而永远跳过审核。
DEFAULT_GATE_MODEL = "DC-video-identity-detector-LLM"

REINFORCE_SUFFIX = (
    "\n\n【一致性重跑强化·必须执行】上一版视频出现了以下问题：{issues}。"
    "整段视频必须严格延续首帧画面（图片1）：景别、取景范围、人物在画内的位置与占比、"
    "服装、发型、配饰、道具、场景结构与光线都不得改变；禁止推近或裁掉首帧已可见的"
    "身体部位与地标，禁止把全身镜头重构成胸像特写，禁止换装、换色、换脸。"
    "姿态必须与画面内支撑面真实接触与承重（坐有座椅/台阶/地面，站有落脚点），"
    "不得悬空、不得上半身直接贴地、不得腰部与地面粘连、不得肢体缺失或人体融接。"
)


@dataclass
class VideoConsistencyVerdict:
    """一次自检的结论。"""

    passed: bool
    reasons: list[str] = field(default_factory=list)
    frame0_similarity: float | None = None
    issues: VideoConsistencyIssues | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def rerun_reasons(self) -> list[str]:
        """值得再花一轮生成去修的原因（身份/服装道具/声音/肢体）。"""

        return [
            reason
            for reason in self.reasons
            if classify_reason(reason) in RERUN_REASON_KINDS
        ]

    @property
    def marked_reasons(self) -> list[str]:
        """只标记人工复核、不触发重跑的原因（景别类 + 自由描述）。"""

        return [
            reason
            for reason in self.reasons
            if classify_reason(reason) not in RERUN_REASON_KINDS
        ]

    @property
    def should_rerun(self) -> bool:
        """是否值得重跑：只有硬问题才重跑，景别类偏差只标记。"""

        return bool(self.rerun_reasons)

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "rerun_reasons": self.rerun_reasons,
            "marked_reasons": self.marked_reasons,
            "should_rerun": self.should_rerun,
            "frame0_similarity": self.frame0_similarity,
            "notes": list(self.notes),
            "issues": (
                self.issues.model_dump() if self.issues is not None else None
            ),
        }

    def reinforce_prompt(self, prompt: str) -> str:
        """把本次判定到的问题写成下一版的强化提示词。

        只强化**值得重跑**的问题：把景别类偏差也写进强化提示词，会让每一版都被
        迫「复刻构图」，反而更容易崩；强化提示词只该锁定身份、服装道具、物理与口型。

        注入前统一过一遍内容审核中性化：视觉模型对画面的自由描述里会出现
        「吊带背心」这类词，一旦被上游文本审核命中，重跑的这一版会直接 4xx，
        等于让「重跑一次」变成「必失败一次」。
        """

        focus = self.rerun_reasons or self.reasons
        issues = "；".join(focus) or "画面对首帧的延续不够严格"
        suffix = sanitize_prompt_for_moderation(REINFORCE_SUFFIX.format(issues=issues))
        base = sanitize_prompt_for_moderation(str(prompt or "").rstrip())
        return f"{base}{suffix}" if base else suffix.lstrip("\n")


@dataclass
class GateSettings:
    enabled: bool
    max_attempts: int


def consistency_gate_config(config: dict[str, Any] | None = None) -> GateSettings:
    """解析自检开关与重跑上限（payload 优先，其次环境变量）。"""

    raw_enabled: Any = None
    if isinstance(config, dict):
        raw_enabled = config.get("video_consistency_gate")
    if raw_enabled is None:
        raw_enabled = os.environ.get(GATE_ENV)
    enabled = True
    if raw_enabled is not None:
        enabled = str(raw_enabled).strip().lower() not in {
            "",
            "0",
            "false",
            "no",
            "off",
        }

    raw_attempts: Any = None
    if isinstance(config, dict):
        raw_attempts = config.get("video_consistency_attempts")
    if raw_attempts is None:
        raw_attempts = os.environ.get(GATE_ATTEMPTS_ENV)
    try:
        max_attempts = int(str(raw_attempts))
    except (TypeError, ValueError):
        max_attempts = DEFAULT_MAX_ATTEMPTS
    max_attempts = max(1, min(max_attempts, 5))
    return GateSettings(enabled=enabled, max_attempts=max_attempts)


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _ffprobe() -> str | None:
    return shutil.which("ffprobe")


def probe_duration_seconds(video_path: Path) -> float:
    binary = _ffprobe()
    if binary is None or not video_path.exists():
        return 0.0
    try:
        result = subprocess.run(
            [
                binary,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return max(0.0, float(str(result.stdout).strip() or 0.0))
    except Exception:  # noqa: BLE001 - 探测失败按未知时长处理
        return 0.0


def audio_track_state(video_path: Path) -> str:
    """成片音轨状态：``unknown`` / ``missing`` / ``silent`` / ``audible``。

    口型与声音能对上，靠的是「画面用模型原生音轨」：模型在生成画面时同时生成声音，
    两者天然同源。因此「有台词却没有音轨、或音轨接近静音」是唯一能确定性判定的
    口型失配 —— 这一版无论画面多好，都必须重跑。
    """

    video_path = Path(video_path)
    ffprobe = _ffprobe()
    ffmpeg = _ffmpeg()
    if not video_path.exists() or ffprobe is None:
        return "unknown"
    try:
        probe = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:  # noqa: BLE001 - 探测失败按未知处理，不阻断产线
        return "unknown"
    if "audio" not in str(probe.stdout):
        return "missing"
    if ffmpeg is None:
        return "unknown"
    try:
        detect = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-i",
                str(video_path),
                "-vn",
                "-af",
                "volumedetect",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception:  # noqa: BLE001
        return "unknown"
    match = re.search(
        r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB",
        f"{detect.stdout}\n{detect.stderr}",
    )
    if match is None:
        return "unknown"
    try:
        mean_volume = float(match.group(1))
    except ValueError:
        return "unknown"
    return "silent" if mean_volume <= SILENT_MEAN_VOLUME_DB else "audible"


def extract_frame(video_path: Path, seconds: float, out_path: Path) -> bool:
    binary = _ffmpeg()
    if binary is None or not video_path.exists():
        return False
    try:
        subprocess.run(
            [
                binary,
                "-y",
                "-ss",
                f"{max(0.0, float(seconds)):.3f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                str(out_path),
            ],
            capture_output=True,
            check=False,
            timeout=120,
        )
    except Exception:  # noqa: BLE001
        return False
    return out_path.exists() and out_path.stat().st_size > 0


def frame_similarity(left_path: Path, right_path: Path, size: int = 64) -> float | None:
    """64x64 灰度平均绝对差 → 相似度（1 = 完全一致）。"""

    try:
        from PIL import Image, ImageChops, ImageStat
    except Exception:  # noqa: BLE001
        return None
    try:
        left = Image.open(left_path).convert("L").resize((size, size))
        right = Image.open(right_path).convert("L").resize((size, size))
        diff = ImageChops.difference(left, right)
        mean = float(sum(ImageStat.Stat(diff).mean))
    except Exception:  # noqa: BLE001
        return None
    return round(1.0 - mean / 255.0, 4)


def parse_aspect_ratio(raw: str) -> float | None:
    """把 ``"9:16"`` 之类的画幅串解析成宽高比；无法解析时返回 None。"""

    text = str(raw or "").strip()
    if not text:
        return None
    for separator in (":", "x", "X", "/", "×"):
        if separator in text:
            left, _, right = text.partition(separator)
            try:
                width = float(left.strip())
                height = float(right.strip())
            except ValueError:
                return None
            if width > 0 and height > 0:
                return width / height
            return None
    return None


#: 首帧画幅与请求画幅的相对差超过这个比例就告警（视频模型会自行补/裁画布）。
FRAME_ASPECT_TOLERANCE = 0.02


def frame_aspect_warning(first_frame_path: Path, requested_ratio: str) -> str:
    """首帧画幅 ≠ 请求画幅时返回告警文案（否则空串）。

    首帧与请求画幅不一致时，视频模型必须自己补画布或裁画面，补出来的部分经常变成
    凭空长出的身体/衣物，是人物一致性与穿帮的主因之一；这里只告警不阻断。
    """

    wanted = parse_aspect_ratio(requested_ratio)
    if wanted is None:
        return ""
    path = Path(first_frame_path)
    if not path.exists():
        return ""
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
    except Exception:  # noqa: BLE001 - 读不了首帧就不告警
        return ""
    if width <= 0 or height <= 0:
        return ""
    actual = width / height
    if abs(actual - wanted) / wanted <= FRAME_ASPECT_TOLERANCE:
        return ""
    return (
        f"首帧画幅 {width}x{height}（{actual:.3f}）与请求画幅 {requested_ratio}"
        f"（{wanted:.3f}）不一致，视频模型需自行补/裁画布，容易出现画面外扩穿帮"
    )


def pick_best_attempt(attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """重跑用尽时选「最接近首帧」的那一版。

    排序口径：先看硬问题条数（越少越好），再看第 0 帧相似度（越高越好），
    最后偏向更晚的一版（强化提示词之后生成的）。全都没通过时返回最优的那条。
    """

    candidates = [item for item in attempts if isinstance(item, dict)]
    if not candidates:
        return None

    def sort_key(indexed: tuple[int, dict[str, Any]]) -> tuple[int, float, int]:
        index, item = indexed
        reasons = item.get("reasons")
        reason_count = len(reasons) if isinstance(reasons, list) else 0
        similarity = item.get("frame0_similarity")
        try:
            score = float(similarity)
        except (TypeError, ValueError):
            score = 0.0
        return (reason_count, -score, -index)

    return min(enumerate(candidates), key=sort_key)[1]


def _gate_vision_model() -> Any:
    """视觉审核模型：走 CE 网关路由，默认复用已开通的视觉别名。"""

    from novelvideo.config import get_newapi_text_pydantic_model

    return get_newapi_text_pydantic_model(
        GATE_MODEL_ENV,
        DEFAULT_GATE_MODEL,
        capability="text.generate.agent",
    )


class VideoConsistencyGate:
    """生成后一致性自检：确定性首帧比对 + 一次视觉模型复核。"""

    def __init__(self) -> None:
        self._agent: Any = None

    def _get_agent(self) -> Any:
        if self._agent is None:
            from pydantic_ai import Agent

            from novelvideo.config import (
                get_newapi_structured_output_model_settings,
            )

            self._agent = Agent(
                _gate_vision_model(),
                system_prompt=VIDEO_CONSISTENCY_VERIFY_PROMPT,
                output_type=VideoConsistencyIssues,
                model_settings=get_newapi_structured_output_model_settings(),
                output_retries=2,
                name="视频一致性审核员",
            )
        return self._agent

    async def check(
        self,
        *,
        video_path: Path,
        first_frame_path: Path,
        identity_image_paths: list[str] | None = None,
        audio_type: str = "",
        expects_speech: bool = False,
        log: Callable[[str], None] | None = None,
    ) -> VideoConsistencyVerdict:
        def emit(message: str) -> None:
            logger.info("video consistency gate: %s", message)
            if log is not None:
                log(message)

        video_path = Path(video_path)
        first_frame_path = Path(first_frame_path)
        if not video_path.exists() or not first_frame_path.exists():
            return VideoConsistencyVerdict(
                passed=True, notes=["缺少视频或首帧文件，跳过一致性自检"]
            )
        if _ffmpeg() is None:
            return VideoConsistencyVerdict(
                passed=True, notes=["环境缺少 ffmpeg，跳过一致性自检"]
            )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            sampled: list[tuple[str, Path]] = []
            frame0 = tmp_dir / "frame0.png"
            if extract_frame(video_path, 0.0, frame0):
                sampled.append(("视频第0帧", frame0))
            duration = probe_duration_seconds(video_path)
            for label, ratio in (("视频中段帧", 0.45), ("视频后段帧", 0.8)):
                point = duration * ratio if duration > 0 else 0.0
                candidate = tmp_dir / f"frame_{int(ratio * 100)}.png"
                if point > 0 and extract_frame(video_path, point, candidate):
                    sampled.append((label, candidate))
            if not sampled:
                return VideoConsistencyVerdict(
                    passed=True, notes=["视频抽帧失败，跳过一致性自检"]
                )

            similarity = frame_similarity(sampled[0][1], first_frame_path)
            reasons: list[str] = []
            notes: list[str] = []
            if expects_speech:
                # 口型与声音的同源保证来自模型原生音轨；音轨没了就必然对不上。
                track_state = audio_track_state(video_path)
                if track_state == "missing":
                    reasons.append(
                        "这一 beat 有台词/解说，但成片没有音轨，口型与声音不可能对上"
                    )
                elif track_state == "silent":
                    reasons.append(
                        "这一 beat 有台词/解说，但成片音轨接近静音，口型与声音不可能对上"
                    )
                else:
                    notes.append(f"音轨状态 {track_state}")
            if similarity is None:
                notes.append("首帧相似度无法计算")
            else:
                notes.append(f"第0帧与首帧相似度 {similarity}")
                if similarity < FRAME0_HARD_FAIL_SIMILARITY:
                    reasons.append(
                        f"视频第0帧与首帧差异过大（相似度 {similarity}），画面被整体重做"
                    )
                elif similarity < FRAME0_WARN_SIMILARITY:
                    notes.append("相似度偏低，通常来自画幅裁剪或轻微重构")

            issues = await self._verify_with_vision(
                first_frame_path=first_frame_path,
                sampled=sampled,
                identity_image_paths=list(identity_image_paths or []),
                audio_type=audio_type,
                emit=emit,
            )
            if issues is not None:
                if issues.costume_changed:
                    reasons.append("服装或配饰与首帧不一致（疑似换装）")
                if issues.face_changed:
                    reasons.append("脸型或身份与首帧不一致（疑似换脸）")
                if issues.framing_changed:
                    reasons.append("景别与首帧不一致（被重构为更近的镜头或裁掉了画面内容）")
                if issues.physical_defect:
                    reasons.append("肢体/姿态物理穿帮（悬空、贴地、肢体缺失或融接）")
                reasons.extend(
                    item for item in issues.issues if str(item or "").strip()
                )
                if issues.summary:
                    notes.append(f"视觉审核：{issues.summary}")

            deduped: list[str] = []
            for reason in reasons:
                text = str(reason or "").strip()
                if text and text not in deduped:
                    deduped.append(text)
            return VideoConsistencyVerdict(
                passed=not deduped,
                reasons=deduped,
                frame0_similarity=similarity,
                issues=issues,
                notes=notes,
            )

    async def _verify_with_vision(
        self,
        *,
        first_frame_path: Path,
        sampled: list[tuple[str, Path]],
        identity_image_paths: list[str],
        audio_type: str,
        emit: Callable[[str], None],
    ) -> VideoConsistencyIssues | None:
        try:
            from pydantic_ai import BinaryContent
        except Exception:  # noqa: BLE001
            return None

        payloads: list[Any] = []
        try:
            payloads.append(
                BinaryContent(
                    data=compress_image(str(first_frame_path)),
                    media_type="image/jpeg",
                )
            )
            for _label, path in sampled:
                payloads.append(
                    BinaryContent(data=compress_image(str(path)), media_type="image/jpeg")
                )
            for identity_path in identity_image_paths[:3]:
                candidate = Path(identity_path)
                if candidate.exists():
                    payloads.append(
                        BinaryContent(
                            data=compress_image(str(candidate)),
                            media_type="image/jpeg",
                        )
                    )
        except Exception as exc:  # noqa: BLE001
            emit(f"抽帧压缩失败，跳过视觉审核: {exc}")
            return None

        labels = ["首帧render", *[label for label, _path in sampled]]
        labels.extend(["身份参考图"] * max(0, len(payloads) - len(labels)))
        task = (
            "请按图片顺序审核这一镜视频：\n"
            + "\n".join(
                f"{index + 1}. {label}" for index, label in enumerate(labels)
            )
            + f"\n本镜音频类型: {audio_type or '未知'}\n"
            "第 1 张是首帧（唯一基准），其余是生成视频的帧与身份参考图。"
        )
        try:
            result = await self._get_agent().run([task, *payloads])
        except Exception as exc:  # noqa: BLE001 - 自检失败不能中断产线
            emit(f"视觉审核调用失败，按通过处理: {exc}")
            return None
        return result.output


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "MARK_ONLY_REASON_KINDS",
    "REASON_KIND_AUDIO",
    "REASON_KIND_COSTUME",
    "REASON_KIND_FRAME0",
    "REASON_KIND_FRAMING",
    "REASON_KIND_IDENTITY",
    "REASON_KIND_OTHER",
    "REASON_KIND_PHYSICAL",
    "RERUN_REASON_KINDS",
    "VideoConsistencyGate",
    "VideoConsistencyVerdict",
    "audio_track_state",
    "classify_reason",
    "consistency_gate_config",
    "extract_frame",
    "frame_aspect_warning",
    "frame_similarity",
    "parse_aspect_ratio",
    "pick_best_attempt",
    "probe_duration_seconds",
]
