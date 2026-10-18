"""统一提示词模板构建器。

所有网格生成模式共享核心组件，通过模式参数调整差异。

模式:
- RENDER: Sketch + Render 模式，基于草图渲染
- SKETCH: 草图模式，专业分镜线稿风格，简化面部与服装轮廓

共同组件:
- 比例指令: Generate a {aspect_ratio} aspect ratio image.
- 网格声明: ASCII 布局 + panel 尺寸
- 角色定义: Character names + reference mode
- Identity Lock: 同一角色 = 同一人
- 风格约束: style_keywords + avoid_keywords
- Panel 描述: 视觉描述 + temporal framing
"""

from typing import Any, Dict, List

from novelvideo.config import IMAGE_DEFAULT_STYLE

# Re-exported for existing importers (``StyleRef`` and
# ``default_ethnicity_instruction`` are part of this module's public surface).
from novelvideo.generators.prompt_components import (  # noqa: E402, F401
    CharacterConfig,
    GridConfig,
    PromptComponents,
    PromptContext,
    PromptMode,
    StyleConfig,
    StyleRef,
    default_ethnicity_instruction,
)


class UnifiedPromptBuilder:
    """统一提示词构建器。"""

    def __init__(self, ctx: PromptContext):
        self.ctx = ctx
        self.components = PromptComponents()
        self._strategies = {
            PromptMode.RENDER: RenderModeStrategy(),
            PromptMode.SKETCH: SketchModeStrategy(),
            PromptMode.ACTION_STORYBOARD: ActionStoryboardStrategy(),
        }

    def build(self) -> str:
        """构建完整提示词（根据模式选择策略）。"""
        strategy = self._strategies[self.ctx.mode]
        prompt = strategy.build(self.ctx, self.components)
        return _apply_pack_directive(self.ctx.mode, prompt)


#: 画面提示词注入的精简规范块按模式取阶段：草图与动作分镜归「分镜与人物表演」，
#: 渲染首帧归「首帧生成」。二者当前都落在第 03 份，但保持映射显式便于后续拆分。
_PACK_STAGE_BY_MODE: dict[PromptMode, str] = {
    PromptMode.RENDER: "selected_regen",
    PromptMode.SKETCH: "sketch_generation",
    PromptMode.ACTION_STORYBOARD: "sketch_generation",
}


def _apply_pack_directive(mode: PromptMode, prompt: str) -> str:
    """本次出图前置的精简规范块（失败时原样返回，绝不挡住出图）。"""

    from novelvideo.skills.ai_film_process import apply_directive

    try:
        return apply_directive(_PACK_STAGE_BY_MODE.get(mode, "sketch_generation"), prompt)
    except Exception:  # noqa: BLE001 - 注入只是增强，不能影响生成链路
        return prompt


# =============================================================================
# 辅助函数
# =============================================================================


def _uses_gpt_image_sketch_profile(ctx: PromptContext) -> bool:
    """Whether this sketch should use the rough GPT-image prompt profile."""
    provider = (ctx.image_provider or "").strip().lower()
    model = (ctx.image_model or "").strip().lower()
    if not provider or not model:
        return False
    if provider in {"openai", "huimeng"} and model == "image-2":
        return True
    return provider == "openrouter" and "gpt" in model and "image" in model


def create_prompt_context(
    mode: PromptMode,
    beats: List[dict],
    rows: int,
    cols: int,
    character_map: Dict[str, dict] = None,
    style: str = None,
    ethnicity: str = "Chinese",
    aspect_ratio: str = None,
    is_portrait_panel: bool = None,
    image_aspect_ratio: str = "",
    panel_detected_keys: Dict[int, set] = None,
    scene_refs: Dict[int, List[Any]] = None,
    prop_asset_refs: Dict[int, List[Any]] = None,
    sketch_colors: Dict[str, str] = None,
    prop_marker_colors: Dict[str, str] = None,
    style_family: str = "",
    animation_subtype: str = "",
    project_dir: str = "",
    image_provider: str = "",
    image_model: str = "",
) -> PromptContext:
    """创建提示词上下文的便捷函数。

    Args:
        mode: 提示词模式
        beats: Beat 数据列表
        rows: 网格行数
        cols: 网格列数
        character_map: 角色映射 {角色名: {...}}
        style: 风格名称
        ethnicity: 种族
        aspect_ratio: 宽高比（可选，自动推断）
        is_portrait_panel: 是否竖屏 panel（可选，自动推断）

    Returns:
        PromptContext 实例
    """
    # 自动推断 aspect_ratio 和 is_portrait_panel
    from novelvideo.generators.nanobanana_grid import REGEN_MODE_CONFIGS, SKETCH_GRID_CONFIG

    # Sketch 模式：使用独立配置
    if mode == PromptMode.SKETCH:
        if aspect_ratio is None:
            aspect_ratio = SKETCH_GRID_CONFIG["aspect_ratio"]
        if is_portrait_panel is None:
            w_ratio, h_ratio = map(int, aspect_ratio.split(":"))
            panel_ar = (w_ratio / cols) / (h_ratio / rows)
            is_portrait_panel = panel_ar < 0.9
    else:
        if aspect_ratio is None:
            # 从 REGEN_MODE_CONFIGS 查找匹配 (rows, cols) 的配置
            for _mk, _cfg in REGEN_MODE_CONFIGS.items():
                if _cfg["rows"] == rows and _cfg["cols"] == cols:
                    aspect_ratio = _cfg["aspect_ratio"]
                    break
            else:
                aspect_ratio = "1:1"

        if is_portrait_panel is None:
            w_ratio, h_ratio = map(int, aspect_ratio.split(":"))
            panel_ar = (w_ratio / cols) / (h_ratio / rows)
            is_portrait_panel = panel_ar < 0.9

    grid_config = GridConfig(
        rows=rows,
        cols=cols,
        aspect_ratio=aspect_ratio,
        image_aspect_ratio=image_aspect_ratio,
        is_portrait_panel=is_portrait_panel,
    )

    # 转换 character_map 为 CharacterConfig
    characters = {}
    if character_map:
        for char_name, info in character_map.items():
            input_mode = info.get("reference_mode", "prompt_only")
            characters[char_name] = CharacterConfig(
                name=char_name,
                face_prompt=info.get("face_prompt", ""),
                base_prompt=info.get("base_prompt", char_name),
                appearance_details=info.get("appearance_details", ""),
                gender=info.get("gender", ""),
                body_type=info.get("body_type", ""),
                reference_path=info.get("reference_path")
                or info.get("portrait_path")
                or info.get("ref_path"),
                reference_mode=input_mode,
                identity_appearances=info.get("identity_appearances", {}),
                sketch_color=info.get("sketch_color", ""),
                identity_sketch_colors=info.get("identity_sketch_colors", {}),
                identity_ref_images=info.get("identity_ref_images", {}),
                identity_face_prompts=info.get("identity_face_prompts", {}),
                identity_body_types=info.get("identity_body_types", {}),
            )

    # 创建风格配置
    style_config = StyleConfig(
        style_name=style or IMAGE_DEFAULT_STYLE,
        project_dir=project_dir,
        style_family=style_family,
        animation_subtype=animation_subtype,
    )

    return PromptContext(
        grid=grid_config,
        characters=characters,
        style=style_config,
        beats=beats,
        mode=mode,
        ethnicity=ethnicity,
        panel_detected_keys=panel_detected_keys,
        scene_refs=scene_refs or {},
        prop_asset_refs=prop_asset_refs or {},
        sketch_colors=sketch_colors or {},
        prop_marker_colors=prop_marker_colors or {},
        image_provider=image_provider,
        image_model=image_model,
    )


# Imported last: the strategies depend on this module's ``PromptContext``/helpers
# and ``_uses_gpt_image_sketch_profile``, so the import must run after they are
# defined. ``UnifiedPromptBuilder`` only touches these at call time.
from novelvideo.generators.prompt_strategies import (  # noqa: E402
    ActionStoryboardStrategy,
    RenderModeStrategy,
    SketchModeStrategy,
)
