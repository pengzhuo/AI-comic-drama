"""Seedance 首帧「真人隐私」审核的降级阶梯。

上游 Ark 对 i2v 首帧（``content[0]``）单独做真人隐私审核：只要锚图里有可识别的
真人脸，就返回 4xx 拒绝
（``InputImageSensitiveContentDetected.PrivacyInformation``，"may contain real
person"）。而 ``reference_images`` 槽位**不做这项审核** —— 这正是「多参考兜底能
通过、但画面会漂移」的根因：兜底把锚图从首帧位挪到参考位，审核过关了，锁定却没了。

实测（同一批 1024×1536 渲染首帧，5 秒生成；线上 12 个 beat 的统计）：::

    原始分辨率原图          ✗  InputImageSensitiveContentDetected.PrivacyInformation
    高斯模糊 2.5            ✓  多数 beat 在这一档就过，构图/服装/道具/场景几乎无损
    384px / 320px / 256px   部分 ✓  少数写实人脸更强的帧才需要，细节略有损失
    更实的正脸（少数帧）     ✗  逐档降质仍被拒，只能退回多参考兜底

结论：降质阶梯**通常有效**，并非「每次都会走到兜底」；但确实存在逐档降质也救不回
的写实正脸帧。故在原阶梯之后补三档更强的档位（模糊+下采样组合、模糊 6、模糊 10），
仍以「保住首帧锁定」为目标，全部失败才退回多参考。档位顺序是启发式的（上游阈值并非
随下采样宽度单调变化），如需精调应以实测为准。

4xx 拒绝不会在上游创建任务、也不计费，所以按「质量损失从小到大」逐档降级几乎
免费。本模块只负责**规划**与**本地变换**；上传与提交由调用方按需触发（未用到的
档位不该产生任何上传）。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: 高斯模糊半径。实测 2.5 在多数 beat 即能过审，且几乎不损失观感（画面本身是渲染图）。
GAUSSIAN_BLUR_RADIUS = 2.5

#: 更强档的重模糊半径，覆盖「模糊 2.5 与逐档下采样都过不了」的写实正脸帧。
HEAVY_BLUR_RADII: tuple[float, ...] = (6.0, 10.0)

#: 逐档下采样宽度，按画质损失从小到大排列。
DOWNSCALE_WIDTHS: tuple[int, ...] = (384, 320, 256)

#: 组合档的下采样宽度：下采样保构图、模糊压人脸细节，救「单靠任一种都不够」的帧。
COMBO_WIDTH = 320

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
BLUR_DOWNSCALE_KIND = "blur_downscale"

#: newapi 包装上游错误时最多把 code/message 再嵌几层 JSON。解析时按这个上限下钻，
#: 避免异常报文里出现自引用导致无限递归。
_MAX_UPSTREAM_NESTING = 6


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


def is_input_image_sensitive_rejection(message: str) -> bool:
    """Return whether an upstream rejection is about the **input image** itself.

    线上实测：应用链路看到的首帧审核拒绝全是光杆 ``InputImageSensitiveContentDetected``
    与 ``input image 'content[0]' may contain sensitive information``；同一批帧直连上游
    登记素材（不经网关）也是同样文案 —— 所以光杆形态不是网关抹掉子类的产物，而是 Ark
    对「画面内容敏感」本来就回光杆，``.PrivacyInformation`` / 「may contain real person」
    只在隐私分类器命中时出现。只认隐私子类的话，这一整类首帧拒绝在线上就走不进任何
    兜底（素材库 asset:// 档、降质阶梯都挨不到）。

    与输出档拒绝刻意区分：成片审核拒绝的 code 是 ``upstream_task_failed``，文案是
    ``the output video ...``，语义是「生成结果敏感」，改输入图救不回来。
    """

    text = str(message or "")
    if "output video" in text:
        return False
    return "InputImageSensitiveContentDetected" in text


def _innermost_code(payload: object, _depth: int = 0) -> str:
    """从（可能多层 JSON 转义的）上游错误体里取最内层的 ``code``。"""

    if _depth >= _MAX_UPSTREAM_NESTING or not isinstance(payload, dict):
        return ""
    nested = payload.get("message")
    if isinstance(nested, str) and nested.lstrip().startswith(("{", "[")):
        try:
            inner = json.loads(nested)
        except (ValueError, TypeError):
            inner = None
        found = _innermost_code(inner, _depth + 1)
        if found:
            return found
    error = payload.get("error")
    if isinstance(error, dict):
        found = _innermost_code(error, _depth + 1)
        if found:
            return found
    code = payload.get("code")
    return str(code) if isinstance(code, str) else ""


def extract_upstream_code(message: str) -> str:
    """尽力从一段上游错误文本里抽出最内层 ``code``；抽不到返回空串。"""

    text = str(message or "")
    start = text.find("{")
    if start >= 0:
        try:
            payload = json.loads(text[start:])
        except (ValueError, TypeError):
            payload = None
        code = _innermost_code(payload)
        if code:
            return code
    matches = re.findall(r'\\?"code\\?"\s*:\s*\\?"([^"\\]+)', text)
    return matches[-1] if matches else ""


def describe_upstream_rejection(message: str) -> str:
    """把上游拒绝压成一句人能看的摘要，用于日志与诊断。"""

    text = str(message or "")
    if is_privacy_information_rejection(text):
        code = (
            extract_upstream_code(text)
            or "InputImageSensitiveContentDetected.PrivacyInformation"
        )
        return f"真人隐私审核（{code}）"
    code = extract_upstream_code(text)
    # ``fail_to_fetch_task`` 只是 newapi 的外层包装，对内层原因没有信息量。
    if code and code != "fail_to_fetch_task":
        return code
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) > 140:
        compact = compact[:140] + "…"
    return compact or "未知错误"


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
    只有在原图被「真人隐私」拒绝时才会被用到，顺序按质量损失从小到大：模糊 2.5 →
    384px → 320px → 256px → 模糊+下采样 → 模糊 6 → 模糊 10。比源图还宽的下采样档位
    会被剔除。
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
    if not width or COMBO_WIDTH < width:
        variants.append(
            AnchorVariant(
                kind=BLUR_DOWNSCALE_KIND,
                label=(
                    f"首帧高斯模糊 {GAUSSIAN_BLUR_RADIUS:g}"
                    f" + 下采样到 {COMBO_WIDTH}px"
                ),
                width=COMBO_WIDTH,
                blur_radius=GAUSSIAN_BLUR_RADIUS,
            )
        )
    for radius in HEAVY_BLUR_RADII:
        variants.append(
            AnchorVariant(
                kind=BLUR_KIND,
                label=f"首帧高斯模糊 {radius:g}",
                blur_radius=radius,
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

    if variant.kind not in (BLUR_KIND, DOWNSCALE_KIND, BLUR_DOWNSCALE_KIND):
        raise ValueError(f"unknown anchor variant kind: {variant.kind}")

    from PIL import Image, ImageFilter

    with Image.open(source) as image:
        prepared = image.convert("RGB")
        if variant.kind in (BLUR_KIND, BLUR_DOWNSCALE_KIND):
            prepared = prepared.filter(
                ImageFilter.GaussianBlur(variant.blur_radius or GAUSSIAN_BLUR_RADIUS)
            )
        if variant.kind in (DOWNSCALE_KIND, BLUR_DOWNSCALE_KIND):
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
    "BLUR_DOWNSCALE_KIND",
    "BLUR_KIND",
    "COMBO_WIDTH",
    "DOWNSCALE_KIND",
    "DOWNSCALE_WIDTHS",
    "GAUSSIAN_BLUR_RADIUS",
    "HEAVY_BLUR_RADII",
    "ORIGINAL_KIND",
    "PRIVACY_REJECTION_MARKERS",
    "AnchorVariant",
    "describe_upstream_rejection",
    "extract_upstream_code",
    "is_input_image_sensitive_rejection",
    "is_privacy_information_rejection",
    "materialize_anchor_variant",
    "plan_anchor_variants",
]
