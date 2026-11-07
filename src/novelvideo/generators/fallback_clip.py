"""单镜「本地降级兜底」：用已生成的首帧合成一支静帧/缓推短片。

某个镜头在自动改写后仍过不了上游内容审核（或重试耗尽）时，整集不该就此停摆。
这里用本地 ffmpeg 把该镜**已有的首帧**渲染成一支与上游成片同规格的短片：

* 上游不再被调用，也不会再触发审核 —— 不占额度；
* 成片保持完整（``videos/beats/epNNN/beat_XX.mp4`` 有了产物），下游合成/导出
  无需容错改动；
* 调用方把该镜登记为「降级」，客户端工作台醒目报错、逐镜标记，用户改完提示词
  后可重做。

规格对齐上游单镜成片：720x1280 / 24fps / H.264 / yuv420p / ~5s（见线上实测）。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from novelvideo.config import FFMPEG_PATH

logger = logging.getLogger(__name__)

#: 与上游单镜成片对齐的默认规格（线上实测 720x1280 / 24fps / yuv420p / 5.088s）。
DEFAULT_WIDTH = 720
DEFAULT_HEIGHT = 1280
DEFAULT_FPS = 24
DEFAULT_DURATION = 5.0


def _filters(
    *, seconds: float, fps: int, width: int, height: int, zoom: bool
) -> list[str]:
    """返回按「效果优先」排序的 ffmpeg ``-vf`` 候选；逐个尝试直到成功。"""

    frames = max(1, int(round(seconds * fps)))
    static = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
    )
    if not zoom:
        return [static]
    # 可选缓推：极缓慢放大，避免看起来像一张卡死的图片；zoompan 不可用时回退静帧。
    # 默认关（静态优先：没有必要就不动）；只有调用方显式需要"防呆版动感"时才开启。
    # 源只要比输出大一圈就够（2x 输出高度），既平滑又不浪费：把 1024x1536 的首帧
    # 先放大到 8000px 是 40s 的活，2x 只要 3s（实测线上 ffmpeg 7.1.5）。
    zoom_height = max(height * 2, 2)
    zoomed = (
        f"scale=-2:{zoom_height},"
        f"zoompan=z='min(zoom+0.0008,1.08)':d={frames}:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"s={width}x{height}:fps={fps}"
    )
    return [zoomed, static]


def render_still_clip(
    frame_png: str | Path,
    out_mp4: str | Path,
    *,
    duration: float = DEFAULT_DURATION,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    fps: int = DEFAULT_FPS,
    zoom: bool = False,
    timeout: int = 180,
) -> bool:
    """把 ``frame_png`` 渲染成 ``out_mp4``；成功返回 True。

    纯本地操作、不触网。任何失败都返回 False（调用方只据此决定是否标记 degraded，
    不向上抛），所以这里吞掉异常并记录日志。
    """

    src = Path(frame_png)
    dst = Path(out_mp4)
    if not src.is_file():
        logger.warning("fallback clip skipped: frame not found %s", src)
        return False

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # noqa: BLE001 - degrade must never raise
        logger.warning("fallback clip could not create dir %s: %s", dst.parent, exc)
        return False

    seconds = max(1.0, float(duration or DEFAULT_DURATION))
    for vf in _filters(
        seconds=seconds, fps=max(1, int(fps)), width=width, height=height, zoom=zoom
    ):
        cmd = [
            str(FFMPEG_PATH),
            "-y",
            "-loop",
            "1",
            "-i",
            str(src),
            "-t",
            f"{seconds:.3f}",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(max(1, int(fps))),
            "-movflags",
            "+faststart",
            str(dst),
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001 - degrade must never raise
            logger.warning("fallback clip ffmpeg raised: %s", exc)
            continue
        if result.returncode == 0 and dst.is_file() and dst.stat().st_size > 0:
            logger.info("fallback clip written %s (%.2fs)", dst, seconds)
            return True
        logger.warning(
            "fallback clip ffmpeg failed rc=%s stderr=%s",
            result.returncode,
            (result.stderr or "")[-400:],
        )
    return False
