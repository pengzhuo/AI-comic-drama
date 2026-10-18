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
:data:`STAGE_SCOPE_PARTS`。

图像与视频生成环节的注入形态见 :data:`STAGE_INJECTION_MODE`：项目级/归并级
调用（每次运行只跑几次）注入对应份的**整份正文**；逐 beat、逐资产以及直接面向
图像/视频模型的画面提示词，注入 :data:`DIRECTIVE_FILES` 里的**精简规范块**
（正文过长，图像模型吃不下，也会成倍放大会话成本）。未登记的阶段默认按精简块
处理。

第 01 份《总控与选择》体量最大，只在归并/总控类调用注入（如剧本处理归并），
不铺到逐镜、逐资产环节。
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

_log = logging.getLogger(__name__)

#: 已提醒过的「本次不注入」阶段，避免逐 beat/逐资产调用反复刷同一行日志。
_warned_skip_stages: set[str] = set()


def _warn_skip_once(key: str, reason: str) -> None:
    if key in _warned_skip_stages:
        return
    _warned_skip_stages.add(key)
    _log.warning("[skills] 技能包未注入：%s（stage=%s）", reason, key)

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
#: 除剧本处理外，图像/视频生成环节及其提示词来源环节也按包内「16 阶段 → 份号」
#: 责任表登记：
#:
#: * 01 总控与选择：只在归并/总控类调用注入，不铺局部环节；
#: * 02 剧本与基础调度：角色/场景/道具/分集抽取、规划与内容改写；
#: * 03 分镜与人物表演：候选分镜、首帧与分格重生成；
#: * 04 风格与标准资产：人物肖像、身份图、场景参考图、道具参考图；
#: * 05 节奏与视频提示词：逐 beat 视频提示词、Seedance 2.0 提示词、运动优化；
#: * 06 衔接声音与成片验收：配音（解说/对白）的语气提示、成片合成与交付环节的
#:   指令文本。合成本身是纯 ffmpeg 编排（无提示词入口），只能沿其周边 agent
#:   指令送达（见 ``docs/agents/skill-pack-injection.md``）。
#:
#: 未列出的阶段不注入正文（或按 :data:`STAGE_INJECTION_MODE` 退到精简块）。
STAGE_PARTS: dict[str, tuple[str, ...]] = {
    # 剧本处理：分段逐段读 02，归并读总控 01（见 STAGE_SCOPE_PARTS）。
    "script_process": ("01", "02"),
    # 02 剧本与基础调度：结构化抽取、规划与改写。
    "build_characters": ("02",),
    "build_scenes": ("02",),
    "build_props": ("02",),
    "build_episodes": ("02",),
    "identity_planner": ("02",),
    "episode_scene_planner": ("02",),
    "episode_prop_planner": ("02",),
    "content_rewriter": ("02",),
    # 04 风格与标准资产：人物四视角、同源场景正反图、必要道具资产。
    "character_portrait": ("04",),
    "identity_image": ("04",),
    "scene_reference_asset": ("04",),
    "prop_reference_asset": ("04",),
    # 03 分镜与人物表演：候选分镜、首帧与分格重生成。
    "sketch_grid_generation": ("03",),
    "sketch_generation": ("03",),
    "sketch_regen": ("03",),
    "grid_regenerate": ("03",),
    "mainline_sketch_from_context": ("03",),
    "mainline_frame_from_context": ("03",),
    "selected_regen": ("03",),
    # 05 节奏与视频提示词：提示词编写与运动优化。
    "beat_video_prompt": ("05",),
    "seedance2_prompt": ("05",),
    "global_optimize_video": ("05",),
    # 06 衔接声音与成片验收：配音的语气提示与成片合成/交付环节的指令文本。
    # 配音只注入语气提示（emotion prompt），台词正文原样不动。
    "narration_audio": ("06",),
    "dialogue_audio": ("06",),
    "freezone_audio_speech": ("06",),
    # 配音在一键流程里的任务类型别名（注入点仍在 voice_clone 的叶子函数里）。
    "audio_generation_indextts2": ("06",),
    "audio_generation": ("06",),
    # 合成环节：主线/画布合成是纯音视频编排，没有提示词入口，因此接到合成周边
    # 的 agent 指令上（捏导的合成与交付回合、画布工作流规划指令）。
    "compose_episode": ("06",),
    "compose_delivery": ("06",),
    "canvas_workflow_plan": ("06",),
}

#: 阶段内的调用作用域 → 实际注入的正文份号。
#:
#: 长稿按段处理时，每段都要重发一次系统提示；若每段都带上总控 01（约 44 KB），
#: 光流程包就吃掉模型窗口的一半以上。因此按作用域裁剪：
#:
#: * ``segment`` —— 逐段执行体，只带第 02 份「剧本与基础调度」；
#: * ``digest``  —— 全剧归并/总控调用（每个项目一次），带第 01 份「总控与选择」；
#: * ``appearance`` —— 身份规划里生成外观字段的调用，带第 04 份「风格与标准资产」。
#:
#: 未登记的作用域退回 :data:`STAGE_PARTS`，保证行为向后兼容。
STAGE_SCOPE_PARTS: dict[str, dict[str, tuple[str, ...]]] = {
    "script_process": {
        "segment": ("02",),
        "digest": ("01",),
    },
    "identity_planner": {
        "appearance": ("04",),
        "plan": ("02",),
    },
}

#: 阶段 → 注入形态。
#:
#: * ``full``      —— 注入整份正文（项目级/归并级调用，每次运行只跑几次）；
#: * ``condensed`` —— 注入精简规范块（逐 beat、逐资产，以及直接面向图像/视频
#:   模型的画面提示词）。
#:
#: 未登记的阶段退回 :data:`DEFAULT_INJECTION_MODE`，默认按精简块处理，避免把
#: 几十 KB 正文塞进画面提示词。
STAGE_INJECTION_MODE: dict[str, str] = {
    "script_process": "full",
    "build_characters": "full",
    "build_scenes": "full",
    "build_props": "full",
    "build_episodes": "full",
    "identity_planner": "full",
    "content_rewriter": "full",
    "episode_scene_planner": "condensed",
    "episode_prop_planner": "condensed",
    "character_portrait": "condensed",
    "identity_image": "condensed",
    "scene_reference_asset": "condensed",
    "prop_reference_asset": "condensed",
    "sketch_grid_generation": "condensed",
    "sketch_generation": "condensed",
    "sketch_regen": "condensed",
    "grid_regenerate": "condensed",
    "mainline_sketch_from_context": "condensed",
    "mainline_frame_from_context": "condensed",
    "selected_regen": "condensed",
    "beat_video_prompt": "condensed",
    "seedance2_prompt": "condensed",
    "global_optimize_video": "condensed",
    "narration_audio": "condensed",
    "dialogue_audio": "condensed",
    "freezone_audio_speech": "condensed",
    "audio_generation_indextts2": "condensed",
    "audio_generation": "condensed",
    "compose_episode": "condensed",
    "compose_delivery": "condensed",
    "canvas_workflow_plan": "condensed",
}

#: 未登记阶段的默认注入形态。
DEFAULT_INJECTION_MODE = "condensed"

#: 份号 → 精简规范块文件（从对应正文提炼新写，随包只读分发）。
#:
#: 精简块用于图像/视频模型与高频调用：只保留直接影响画面内容的硬约束，
#: 长度上限见 :data:`DIRECTIVE_MAX_CHARS`。
DIRECTIVE_FILES: dict[str, str] = {
    "02": "directives/02_script_and_blocking.md",
    "03": "directives/03_storyboard_and_performance.md",
    "04": "directives/04_style_and_assets.md",
    "05": "directives/05_rhythm_and_video_prompts.md",
    "06": "directives/06_handoff_sound_and_delivery.md",
}

#: 单份精简规范块的字符上限；超限不注入并记日志（宁可少注入，也不撑爆画面提示词）。
DIRECTIVE_MAX_CHARS = 1200

#: 配音只发这些节：成片验收讲的是镜头衔接与成片校验，与 TTS 语气无关，不进语气提示。
AUDIO_DIRECTIVE_SECTIONS: tuple[str, ...] = ("配音与表演", "声音衔接")

#: 注入总开关（``DIRECTOR_PACK_INJECTION=0`` 关闭全部技能包注入）。
PACK_INJECTION_ENV = "DIRECTOR_PACK_INJECTION"


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


# ── 精简规范块 ────────────────────────────────────────────────────────────


@lru_cache(maxsize=None)
def load_directive(code: str) -> str:
    """读取单份精简规范块；该份号没有精简块时抛出 :class:`KeyError`。"""

    key = str(code or "").strip()
    filename = DIRECTIVE_FILES[key]
    return (PACK_DIR / filename).read_text(encoding="utf-8").strip()


def stage_directive(stage: str, scope: str = "") -> str:
    """返回阶段（含作用域）对应的精简规范块正文；没有对应份则返回空串。"""

    codes = stage_scope_parts(str(stage or "").strip(), scope)
    chunks: list[str] = []
    for code in codes:
        if code not in DIRECTIVE_FILES:
            continue
        try:
            text = load_directive(code)
        except (KeyError, OSError):
            continue
        if text:
            chunks.append(text)
    return "\n\n".join(chunks)


# ── 注入判定 ──────────────────────────────────────────────────────────────

#: 整份正文最多占配置窗口的比例；超过就降级为精简块，给用户内容留足余量。
PACK_SECTION_MAX_WINDOW_RATIO = 0.25

#: 覆盖上面的比例（``DIRECTOR_PACK_SECTION_WINDOW_RATIO``，取值 0–1）。
PACK_SECTION_WINDOW_RATIO_ENV = "DIRECTOR_PACK_SECTION_WINDOW_RATIO"


def section_window_ratio() -> float:
    """整份正文允许占用的窗口比例（``DIRECTOR_PACK_SECTION_WINDOW_RATIO``）。"""

    raw = os.environ.get(PACK_SECTION_WINDOW_RATIO_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return PACK_SECTION_MAX_WINDOW_RATIO
        if 0 < value <= 1:
            return value
    return PACK_SECTION_MAX_WINDOW_RATIO


def injection_enabled() -> bool:
    """技能包注入总开关（``DIRECTOR_PACK_INJECTION``，默认开）。

    取值 ``0``/``false``/``no``/``off`` 时关闭全部注入，其它情况视为开启。
    """

    raw = os.environ.get(PACK_INJECTION_ENV, "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def injection_mode(stage: str) -> str:
    """阶段采用的注入形态（``full`` 整份正文 / ``condensed`` 精简块）。"""

    return STAGE_INJECTION_MODE.get(str(stage or "").strip(), DEFAULT_INJECTION_MODE)


def window_limit_tokens(model_env: str | None = None) -> int | None:
    """技能包最多能占用多少 token（按配置窗口 × 比例）；``None`` 表示不限额。

    返回 ``0`` 表示窗口配置本身无效（此时按「放不下」处理，宁可不注入）。
    """

    try:
        from novelvideo.utils.model_context_budget import TextContextBudget

        budget = TextContextBudget.from_env(model_env)
    except ValueError:
        return 0
    if budget is None:
        return None
    return int(budget.context_tokens * section_window_ratio())


def fits_window(text: str, model_env: str | None = None) -> bool:
    """技能包文本能否在窗口额度内容纳。

    这是「注入不会把请求推到 ``MODEL_CONTEXT_BUDGET_EXCEEDED``」的守门条件：
    整份正文与精简规范块都不得占用超过 :func:`section_window_ratio` 的窗口。
    """

    if not text:
        return False
    limit = window_limit_tokens(model_env)
    if limit is None:
        return True
    if limit <= 0:
        return False
    try:
        from novelvideo.utils.model_context_budget import estimate_tokens

        return estimate_tokens(text) <= limit
    except Exception:  # noqa: BLE001 - 估算失败时按放不下处理，保守优先
        return False


def section_fits(text: str, model_env: str | None = None) -> bool:
    """整份正文是否放得下：按配置窗口留出余量，超了就降到精简块。"""

    return fits_window(text, model_env)


def apply_directive(stage: str, text: str, scope: str = "") -> str:
    """在画面提示词前插入精简规范块；没有可注入内容时原样返回。"""

    if not text:
        return text
    block = pack_directive(stage, scope)
    if not block:
        return text
    return f"{block}\n\n{text}"


def append_pack(
    stage: str, text: str, *, model_env: str | None = None, scope: str = ""
) -> str:
    """在 LLM 系统提示末尾追加技能包段落；没有可注入内容时原样返回。"""

    if not text:
        return text
    block = pack_block(stage, scope, model_env=model_env)
    if not block:
        return text
    return f"{text}\n\n{block}"


def _keep_directive_sections(block: str, sections: tuple[str, ...]) -> str:
    """只保留 ``## 标题`` 命中 ``sections`` 的段落（连同最前面的包装标题行）。

    一个都没命中时整块返回，避免因为标题改名而让配音拿不到规范。
    """

    head: list[str] = []
    keep: list[str] = []
    seen_section = False
    keeping = False
    for line in block.splitlines():
        if line.startswith("## "):
            seen_section = True
            keeping = line[3:].strip() in sections
        elif not seen_section:
            head.append(line)
            continue
        if keeping:
            keep.append(line)
    if not keep:
        return block
    return "\n".join([*head, *keep]).strip()


def audio_directive(stage: str, scope: str = "") -> str:
    """配音专用的精简规范块：第 06 份里只留与 TTS 有关的两节。"""

    block = pack_directive(stage, scope)
    if not block:
        return ""
    return _keep_directive_sections(block, AUDIO_DIRECTIVE_SECTIONS)


def append_audio_directive(stage: str, text: str = "", scope: str = "") -> str:
    """给 TTS 语气提示追加流程包规范（配音专用，允许空语气提示）。

    配音只能注入语气/表演提示，绝不改动要朗读的台词正文；注入失败一律退回原文，
    不允许影响配音生成链路。
    """

    try:
        block = audio_directive(stage, scope)
    except Exception:  # noqa: BLE001 - 注入只是增强，不能影响配音生成
        return text
    if not block:
        return text
    base = str(text or "").strip()
    return f"{base}\n\n{block}" if base else block


def pack_directive(
    stage: str, scope: str = "", *, model_env: str | None = None
) -> str:
    """给图像/视频画面提示词用的精简规范块（返回空串表示本次不注入）。"""

    if not injection_enabled():
        return ""
    key = str(stage or "").strip()
    body = stage_directive(key, scope)
    if not body:
        return ""
    if len(body) > DIRECTIVE_MAX_CHARS:
        _warn_skip_once(
            key,
            f"精简规范块 {len(body)} 字 > 上限 {DIRECTIVE_MAX_CHARS}",
        )
        return ""
    titles = "、".join(
        f"第 {code} 份：{PART_TITLES.get(code, code)}"
        for code in stage_scope_parts(key, scope)
        if code in DIRECTIVE_FILES
    )
    block = f"【制作流程包规范 · 版本 {PACK_VERSION} · 精简自 {titles}】\n{body}"
    if not fits_window(block, model_env):
        _warn_skip_once(
            key,
            f"精简规范块超过窗口额度（比例 {section_window_ratio():.2f}）",
        )
        return ""
    return block


def pack_block(stage: str, scope: str = "", *, model_env: str | None = None) -> str:
    """给 LLM 环节用的技能包段落（返回空串表示本次不注入）。

    ``full`` 形态注入整份正文，但超出窗口余量时自动降级为精简块；这样即使
    某个阶段的输入很大，也不会把请求推到 ``MODEL_CONTEXT_BUDGET_EXCEEDED``。
    """

    if not injection_enabled():
        return ""
    key = str(stage or "").strip()
    if injection_mode(key) == "full":
        section = stage_scope_pack(key, scope)
        if section and section_fits(section, model_env):
            titles = "、".join(
                f"第 {code} 份：{PART_TITLES.get(code, code)}"
                for code in stage_scope_parts(key, scope)
            )
            return (
                f"## 制作流程包正文（版本 {PACK_VERSION}；本次注入 {titles}）\n"
                f"{section}"
            )
    return pack_directive(key, scope, model_env=model_env)


__all__ = [
    "PACK_VERSION",
    "PART_FILES",
    "PART_TITLES",
    "STAGE_PARTS",
    "STAGE_SCOPE_PARTS",
    "STAGE_INJECTION_MODE",
    "DEFAULT_INJECTION_MODE",
    "DIRECTIVE_FILES",
    "DIRECTIVE_MAX_CHARS",
    "PACK_INJECTION_ENV",
    "PACK_DIR",
    "section_window_ratio",
    "window_limit_tokens",
    "fits_window",
    "load_part",
    "load_pack",
    "stage_pack",
    "stage_parts",
    "stage_scope_pack",
    "stage_scope_parts",
    "load_directive",
    "stage_directive",
    "apply_directive",
    "append_pack",
    "append_audio_directive",
    "audio_directive",
    "injection_enabled",
    "injection_mode",
    "section_fits",
    "pack_directive",
    "pack_block",
]
