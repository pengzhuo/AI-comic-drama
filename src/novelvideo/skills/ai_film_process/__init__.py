"""「通用AI影视制作流程包」内置资源（版本 2.0）。

流程包由六份连续正文组成，共用同一套 16 阶段制作流程：

===========  ==============================  ==========================
份号          正文主题                        主要责任阶段
===========  ==============================  ==========================
01           总控与选择                      全部（所有任务先读）
02           剧本与基础调度                  阶段 2—3、6
03           分镜与人物表演                  阶段 3、8—9
04           风格与标准资产                  阶段 4—7
05           节奏与视频提示词                阶段 9—11
06           衔接声音与成片验收              阶段 12—16
===========  ==============================  ==========================

正文以只读文本资源的形式随包分发；加载后按阶段与调用作用域裁剪注入。阶段与
份号的对应关系见 :data:`STAGE_PARTS`，阶段内作用域裁剪见
:data:`STAGE_SCOPE_PARTS`，未列出的阶段默认不注入（后续按需扩展）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PACK_DIR = Path(__file__).resolve().parent

#: 版本编号（仅版本标识，与制作步骤无关）。
PACK_VERSION = "2.0"

#: 份号 → 文件名。
PART_FILES: dict[str, str] = {
    "01": "01_master_control.txt",
    "02": "02_script_and_blocking.txt",
    "03": "03_storyboard_and_performance.txt",
    "04": "04_style_and_assets.txt",
    "05": "05_rhythm_and_video_prompts.txt",
    "06": "06_handoff_sound_and_delivery.txt",
}

#: 份号 → 正文主题（用于拼接时的可见标题）。
PART_TITLES: dict[str, str] = {
    "01": "总控与选择",
    "02": "剧本与基础调度",
    "03": "分镜与人物表演",
    "04": "风格与标准资产",
    "05": "节奏与视频提示词",
    "06": "衔接声音与成片验收",
}

#: 一键流程阶段 → 需要注入的正文份号。
#:
#: 目前只有剧本处理阶段注入正文：总控 01（所有任务先读）＋剧本与基础调度 02。
#: 其余份数（03/04/05/06）按后续阶段需要再注入，因此这里保持可扩展的映射。
STAGE_PARTS: dict[str, tuple[str, ...]] = {
    "script_process": ("01", "02"),
}

#: 阶段内的调用作用域 → 实际注入的正文份号。
#:
#: 长稿按段处理时，每段都要重发一次系统提示；若每段都带上总控 01（约 44 KB），
#: 光流程包就吃掉模型窗口的一半以上。因此按作用域裁剪：
#:
#: * ``segment`` —— 逐段执行体，只带第 02 份「剧本与基础调度」；
#: * ``digest``  —— 全剧归并/总控调用（每个项目一次），带第 01 份「总控与选择」。
#:
#: 未登记的作用域退回 :data:`STAGE_PARTS`，保证行为向后兼容。
STAGE_SCOPE_PARTS: dict[str, dict[str, tuple[str, ...]]] = {
    "script_process": {
        "segment": ("02",),
        "digest": ("01",),
    },
}


@lru_cache(maxsize=None)
def load_part(code: str) -> str:
    """读取单份正文；份号不存在时抛出 :class:`KeyError`。"""

    key = str(code or "").strip()
    filename = PART_FILES[key]
    path = PACK_DIR / filename
    return path.read_text(encoding="utf-8")


def load_pack(codes: tuple[str, ...] | list[str] | None = None) -> str:
    """按份号顺序拼接正文；``codes`` 省略时返回全部六份。"""

    selected = tuple(codes) if codes is not None else tuple(PART_FILES)
    chunks: list[str] = []
    for code in selected:
        title = PART_TITLES.get(code, "")
        header = f"===== 流程包第{code}份：{title or code} ====="
        chunks.append(f"{header}\n\n{load_part(code).strip()}")
    return "\n\n".join(chunks)


def stage_parts(stage: str) -> tuple[str, ...]:
    """返回某个一键流程阶段需要注入的份号（未配置则为空）。"""

    return tuple(STAGE_PARTS.get(str(stage or "").strip(), ()))


def stage_pack(stage: str) -> str:
    """返回某个一键流程阶段需要注入的正文（未配置则为空串）。"""

    codes = stage_parts(stage)
    if not codes:
        return ""
    return load_pack(codes)


def stage_scope_parts(stage: str, scope: str) -> tuple[str, ...]:
    """返回阶段内某个调用作用域实际注入的份号。

    未登记的作用域退回 :func:`stage_parts`，这样即使不配置作用域裁剪，
    也能拿到该阶段的完整正文。
    """

    key = str(stage or "").strip()
    mapped = STAGE_SCOPE_PARTS.get(key)
    if not mapped:
        return stage_parts(key)
    codes = mapped.get(str(scope or "").strip())
    if codes is None:
        return stage_parts(key)
    return tuple(codes)


def stage_scope_pack(stage: str, scope: str) -> str:
    """返回阶段内某个调用作用域实际注入的正文（未配置则为空串）。"""

    codes = stage_scope_parts(stage, scope)
    if not codes:
        return ""
    return load_pack(codes)


__all__ = [
    "PACK_VERSION",
    "PART_FILES",
    "PART_TITLES",
    "STAGE_PARTS",
    "STAGE_SCOPE_PARTS",
    "PACK_DIR",
    "load_part",
    "load_pack",
    "stage_pack",
    "stage_parts",
    "stage_scope_pack",
    "stage_scope_parts",
]
