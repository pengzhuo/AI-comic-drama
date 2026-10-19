from __future__ import annotations

import pytest

from novelvideo.prompt_safety import (
    sanitize_prompt_for_moderation,
    is_text_moderation_rejection,
)

#: 这些短语来自一次真实失败（「重生」项目第 1 集 Beat 8）：上游以
#: ``InputTextSensitiveContentDetected`` 拒绝整次提交。用「假 image_url」探针
#: 二分后确认，被拦的不是单个词，而是「年轻女性 + 湿身 + 喘息/身体起伏」的组合：
#: 单独回退湿身质感或肩胛滑移仍被拒，单独回退喘息簇或起伏簇即可放行。
TRIGGER_CLUSTERS = (
    "她微微喘息，把呼吸压匀",
    "身体一下一下地起伏，身体起伏由快到缓",
    "身体止不住地发抖",
    "喘息未匀，急促的喘息",
    "肩胛沿着砖面缓缓向下滑移一段，肩胛贴墙",
    "湿发与衣物表面形成细密镜面反光，贴在皮肤上的湿发",
)

BANNED_RESIDUALS = (
    "喘息",
    "一下一下",
    "发抖",
    "肩胛",
    "贴在皮肤",
    "湿发与衣物表面",
    "身体起伏",
)


@pytest.mark.parametrize("text", TRIGGER_CLUSTERS)
def test_trigger_clusters_are_neutralized(text: str) -> None:
    cleaned = sanitize_prompt_for_moderation(text)
    assert cleaned != text
    assert not [word for word in BANNED_RESIDUALS if word in cleaned]


@pytest.mark.parametrize("text", TRIGGER_CLUSTERS)
def test_sanitizer_is_idempotent(text: str) -> None:
    once = sanitize_prompt_for_moderation(text)
    assert sanitize_prompt_for_moderation(once) == once


def test_neutral_text_is_untouched() -> None:
    neutral = "雨夜旧巷中景，女孩背靠砖墙站立，双手抱着白色蛋糕盒。"
    assert sanitize_prompt_for_moderation(neutral) == neutral


def test_empty_text_is_safe() -> None:
    assert sanitize_prompt_for_moderation("") == ""


def test_moderation_rejection_detection() -> None:
    assert is_text_moderation_rejection(
        "HTTP 400 - InputTextSensitiveContentDetected: content[0]"
    )
    assert not is_text_moderation_rejection("HTTP 400 - InvalidParameter: ratio")
    assert not is_text_moderation_rejection("")
