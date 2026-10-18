"""渲染首帧的服装 / 配饰 / 妆容硬锁，以及视频提示词的妆容一致性。

美貌不是重点，「同一件衣服、同一个妆」才是：首帧是整条链路的视觉锚点，首帧的服装或
妆容一旦被模型「重新设计」，后面每个 beat 都会沿着错的那一版漂。这里回归两点：

1. Render 提示词里必须出现不可协商的服装/配饰/妆容锁定，并带上被锁定的文字描述；
2. 严格首帧模式的视频提示词规则里必须点名妆容（否则口播 beat 会悄悄换妆）。
"""

from __future__ import annotations

from novelvideo.generators.prompt_builder import (
    CharacterConfig,
    GridConfig,
    PromptComponents,
    PromptContext,
    PromptMode,
    StyleConfig,
)
from novelvideo.generators.prompt_strategies import RenderModeStrategy
from novelvideo.seedance2_i2v.prompt import build_seedance2_prompt_composer_task

LOCK_HEADER = "WARDROBE, ACCESSORY & MAKEUP LOCK"

APPEARANCE = "浅灰色薄款棉质外套被雨水浸透，内搭白色圆领薄棉T恤，裸妆"


def _context(*, characters: dict[str, CharacterConfig], detected: list[str]) -> PromptContext:
    return PromptContext(
        grid=GridConfig(rows=1, cols=1, aspect_ratio="9:16"),
        characters=characters,
        style=StyleConfig(style_name="realistic"),
        beats=[
            {
                "beat_number": 1,
                "detected_identities": detected,
                "visual_description": "雨中街道。",
            }
        ],
        mode=PromptMode.RENDER,
    )


def test_lock_lists_the_locked_wear_text_and_forbids_redesigning_it() -> None:
    characters = {
        "许知意": CharacterConfig(
            name="许知意",
            face_prompt="face",
            appearance_details=APPEARANCE,
            reference_path="/tmp/xzy.png",
            reference_mode="portrait_only",
            sketch_color="#4A90D9 ICE BLUE",
        )
    }
    ctx = _context(characters=characters, detected=["许知意_十八岁时期"])

    lock = PromptComponents.build_wardrobe_and_makeup_lock(ctx, ["许知意"])

    assert lock.startswith(LOCK_HEADER)
    assert APPEARANCE in lock
    # 增删配饰是穿帮的主要来源，必须显式禁止。
    assert "do NOT add or remove layers" in lock
    # 妆容与服装同源，必须在同一条硬约束里点名。
    assert "makeup" in lock
    # 冲突时以参考图与锁定文字为准（草图不能反向改服装）。
    assert "the identity reference and the locked text win" in lock


def test_lock_uses_per_identity_appearance_when_available() -> None:
    characters = {
        "许知意": CharacterConfig(
            name="许知意",
            face_prompt="face",
            appearance_details="默认造型",
            identity_appearances={"十八岁时期": "白色校服与双马尾，素颜"},
            reference_path="/tmp/xzy.png",
            reference_mode="portrait_only",
            sketch_color="#4A90D9 ICE BLUE",
        )
    }
    ctx = _context(characters=characters, detected=["许知意_十八岁时期"])

    lock = PromptComponents.build_wardrobe_and_makeup_lock(ctx, ["许知意"])

    assert "白色校服与双马尾，素颜" in lock
    # 有身份级描述时不该再退回角色级默认造型，避免两套衣服同时出现在提示词里。
    assert "默认造型" not in lock


def test_lock_is_empty_without_appearance_text() -> None:
    characters = {
        "无描述角色": CharacterConfig(name="无描述角色", face_prompt="face"),
    }
    ctx = _context(characters=characters, detected=["无描述角色_青年时期"])

    assert PromptComponents.build_wardrobe_and_makeup_lock(ctx, ["无描述角色"]) == ""
    assert PromptComponents.build_wardrobe_and_makeup_lock(
        _context(characters={}, detected=[]), []
    ) == ""


def test_render_strategy_includes_the_lock() -> None:
    characters = {
        "许知意": CharacterConfig(
            name="许知意",
            face_prompt="face",
            appearance_details=APPEARANCE,
            reference_path="/tmp/xzy.png",
            reference_mode="portrait_only",
            sketch_color="#4A90D9 ICE BLUE",
        )
    }
    ctx = _context(characters=characters, detected=["许知意_十八岁时期"])

    prompt = RenderModeStrategy().build(ctx, PromptComponents())

    assert LOCK_HEADER in prompt
    assert APPEARANCE in prompt


def test_strict_first_frame_video_rule_names_makeup() -> None:
    zh = build_seedance2_prompt_composer_task(
        mode="first_frame",
        beat={"dialogue": "今天是我的生日。"},
        assets=[],
        text_overlay=None,
        prompt_guidance="",
        draft_prompt="",
        language="zh",
    )
    en = build_seedance2_prompt_composer_task(
        mode="first_frame",
        beat={"dialogue": "今天是我的生日。"},
        assets=[],
        text_overlay=None,
        prompt_guidance="",
        draft_prompt="",
        language="en",
    )

    assert "妆容" in zh
    assert "makeup" in en
