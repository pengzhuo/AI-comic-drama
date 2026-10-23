from __future__ import annotations

import pytest

from novelvideo.prompt_safety import (
    is_output_video_moderation_rejection,
    neutralize_sensitive_elements,
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


#: 审核补救专用规则：可辨识证件/文字/编号、暴露、血腥这类**画面元素**。
SENSITIVE_ELEMENT_CASES = {
    "保安胸前工牌特写，编号030756清晰可见": ("胸前挂牌", "〔编号略〕"),
    "画面出现身份证与合同特写": ("本人卡片", "纸质材料特写"),
    "病历特写，屏幕上的文字清晰可读": ("纸质材料特写", "屏幕亮面"),
    "血迹与伤口特写": ("深色痕迹", "擦痕"),
    "两人在雨中接吻": ("靠近",),
    "她半裸着靠在墙上": ("衣着完整",),
}


@pytest.mark.parametrize(("text", "expected"), SENSITIVE_ELEMENT_CASES.items())
def test_sensitive_elements_are_neutralized(text: str, expected: tuple[str, ...]) -> None:
    cleaned = neutralize_sensitive_elements(text)
    assert cleaned != text
    for fragment in expected:
        assert fragment in cleaned


def test_sensitive_element_rules_do_not_cascade() -> None:
    """替换文本里不能出现更靠后的待替换词，否则会叠出「本人证件类道具（…）（…）」。"""

    cleaned = neutralize_sensitive_elements("画面出现身份证特写")
    assert cleaned.count("画面不出现可辨识文字与号码") == 1


def test_sensitive_elements_leave_neutral_text_alone() -> None:
    neutral = "雨夜旧巷中景，女孩背靠砖墙站立，双手抱着白色蛋糕盒。"
    assert neutralize_sensitive_elements(neutral) == neutral


def test_output_video_rejection_detection() -> None:
    assert is_output_video_moderation_rejection(
        "OutputVideoSensitiveContentDetected.PolicyViolation"
    )
    assert is_output_video_moderation_rejection(
        "The output video may contain sensitive information"
    )
    assert is_output_video_moderation_rejection("copyright restrictions apply")
    assert not is_output_video_moderation_rejection(
        "InputTextSensitiveContentDetected: content[0]"
    )
