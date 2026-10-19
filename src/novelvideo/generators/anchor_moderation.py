"""Seedance 首帧「真人隐私」审核的降级阶梯。

上游 Ark 对 i2v 首帧（``content[0]``）单独做真人隐私审核：只要锚图里有可识别的
真人脸，就返回 4xx 拒绝
（``InputImageSensitiveContentDetected.PrivacyInformation``，"may contain real
person"）。而 ``reference_images`` 槽位**不做这项审核** —— 这正是「多参考兜底能
通过、但画面会漂移」的根因：兜底把锚图从首帧位挪到参考位，审核过关了，锁定却没了。

实测矩阵（同一张 1024×1536 渲染首帧，5 秒生成）：:

    原始分辨率原图          ✗  InputImageSensitiveContentDetected.PrivacyInformation
    高斯模糊 2.5            ✓  构图/服装/道具/场景/光线与原图一致
    384px / 320px / 256px   ✓  同上，细节略有损失
    480px                   ✗  阈值落在 384~480 之间
    纯场景（无人）对照      ✓

4xx 拒绝不会在上游创建任务、也不计费，所以按「质量损失从小到大」逐档降级几乎
免费。本模块只负责**规划**与**本地变换**；上传与提交由调用方按需触发（未用到的
档位不该产生任何上传）。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: 高斯模糊半径。实测 2.5 既能过审、又几乎不损失观感（画面本身是渲染图）。
GAUSSIAN_BLUR_RADIUS = 2.5

#: 逐档下采样宽度，按画质损失从小到大排列。
DOWNSCALE_WIDTHS: tuple[int, ...] = (384, 320, 256)

#: 上游「输入图含真人」拒绝的识别标志（任一命中即可）。刻意只认隐私这一类的
#: 具体串：``InputImageSensitiveContentDetected`` 前缀还有 Violence 等其它子类，
#: 把它们当成同一件事会让日志与统计失真。
PRIVACY_REJECTION_MARKERS: tuple[str, ...] = (
    "PrivacyInformation",
    "may contain real person",
)

ORIGINAL_KIND = "original"
BLUR_KIND = "gaussian_blur"
DOWNSCALE_KIND = "downscale"


@dataclass(frozen=True)
class AnchorVariant:
    """一档首帧降级方案。``kind`` 决定 :func:`materialize_anchor_variant` 的做法。"""

    kind: str
    label: str
    width: int = 0
    blur_radius: float = 0.0

    @property
    def is_original(self) -> bool:
        return self.kind == ORIGINAL_KIND


def is_privacy_information_rejection(message: str) -> bool:
    """Return whether an upstream rejection is the real-person privacy gate."""

    text = str(message or "")
    return any(marker in text for marker in PRIVACY_REJECTION_MARKERS)


def _local_image_path(source: str) -> str | None:
    value = str(source or "").strip()
    if not value or value.startswith(("http://", "https://", "data:")):
        return None
    if not os.path.isfile(value):
        return None
    return value


def plan_anchor_variants(source: str) -> list[AnchorVariant]:
    """Plan the降级顺序 for one anchor image.

    第一档永远是原图（aicf 放宽上游审核后它会直接通过，零质量代价）。之后的档位
    只有在原图被「真人隐私」拒绝时才会被用到，顺序按质量损失从小到大：模糊 →
    384px → 320px → 256px。比源图还宽的档位会被剔除。
    """

    variants = [AnchorVariant(kind=ORIGINAL_KIND, label="首帧原图")]
    if _local_image_path(source) is None:
        # 远程/内联输入没法在本地做变换，只能原样提交。
        return variants

    variants.append(
        AnchorVariant(
            kind=BLUR_KIND,
            label=f"首帧高斯模糊 {GAUSSIAN_BLUR_RADIUS:g}",
            blur_radius=GAUSSIAN_BLUR_RADIUS,
        )
    )
    width = _source_width(source)
    for target in DOWNSCALE_WIDTHS:
        if width and target >= width:
            continue
        variants.append(
            AnchorVariant(
                kind=DOWNSCALE_KIND,
                label=f"首帧下采样到 {target}px",
                width=target,
            )
        )
    return variants


def _source_width(source: str) -> int:
    try:
        from PIL import Image

        with Image.open(source) as image:
            return int(image.size[0])
    except Exception:  # noqa: BLE001 - 读不出尺寸就别剔除档位，交给变换时报错
        return 0


def materialize_anchor_variant(
    source: str,
    variant: AnchorVariant,
    *,
    work_dir: str | None = None,
) -> str:
    """Write ``variant`` to a temp file and return its path.

    ``original`` 档直接返回源路径，不产生任何 IO。临时文件不做自动清理——调用方
    上传完成后应自行删除（见 ``video_generator`` 的降级档位工厂）。
    """

    if variant.is_original:
        return source

    from PIL import Image, ImageFilter

    with Image.open(source) as image:
        prepared = image.convert("RGB")
        if variant.kind == BLUR_KIND:
            prepared = prepared.filter(
                ImageFilter.GaussianBlur(variant.blur_radius or GAUSSIAN_BLUR_RADIUS)
            )
        elif variant.kind == DOWNSCALE_KIND:
            target = max(1, int(variant.width))
            width, height = prepared.size
            if width <= target:
                scale = 1.0
            else:
                scale = target / float(width)
            prepared = prepared.resize(
                (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
                Image.LANCZOS,
            )
        else:  # pragma: no cover - 规划阶段只产出上面两种
            raise ValueError(f"unknown anchor variant kind: {variant.kind}")

        directory = work_dir or tempfile.gettempdir()
        Path(directory).mkdir(parents=True, exist_ok=True)
        handle, path = tempfile.mkstemp(
            prefix="anchor_variant_",
            suffix=".png",
            dir=directory,
        )
        os.close(handle)
        prepared.save(path, format="PNG")
    return path


__all__ = [
    "DOWNSCALE_WIDTHS",
    "GAUSSIAN_BLUR_RADIUS",
    "PRIVACY_REJECTION_MARKERS",
    "AnchorVariant",
    "is_privacy_information_rejection",
    "materialize_anchor_variant",
    "plan_anchor_variants",
]
