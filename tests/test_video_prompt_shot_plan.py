"""单测：视频提示词的镜长/多镜计划与自然速度约束。"""

from __future__ import annotations

from novelvideo.agents.global_video_optimizer import (
    GLOBAL_VIDEO_OPTIMIZER_INSTRUCTIONS_EN,
    GLOBAL_VIDEO_REVIEWER_INSTRUCTIONS_EN,
    _beat_clip_seconds,
)
from novelvideo.agents import video_prompt_builder as vpb


def test_clip_seconds_prefers_explicit_beat_duration():
    assert _beat_clip_seconds({"duration_seconds": 12.0}, "") == 12.0


def test_clip_seconds_estimates_from_narration():
    # 60 字按默认 TTS 语速约 10 秒量级，且被夹在 5–15 秒。
    seconds = _beat_clip_seconds({"duration_seconds": None}, "字" * 60)
    assert 5.0 <= seconds <= 15.0


def test_optimizer_bans_slowness_and_requests_cuts():
    text = GLOBAL_VIDEO_OPTIMIZER_INSTRUCTIONS_EN
    assert "natural real-time speed" in text
    assert "HARD CUT" in text
    # 不应再写死「约 5 秒的连续动作链」。
    assert "clip is ~5 seconds" not in text
    assert "then slowly sinks" not in text
    # 表演优先 + 写实去卡通：必须有可见微表情与真人实拍约束。
    assert "Never leave a blank" in text
    assert "photoreal" in text


def test_reviewer_flags_slow_motion_and_frozen_faces():
    assert "Slow-motion / drag" in GLOBAL_VIDEO_REVIEWER_INSTRUCTIONS_EN
    assert "Blank / stiff face" in GLOBAL_VIDEO_REVIEWER_INSTRUCTIONS_EN
    assert "Cartoon / stylized look" in GLOBAL_VIDEO_REVIEWER_INSTRUCTIONS_EN


def test_prompt_builder_is_duration_aware():
    text = vpb.VIDEO_PROMPT_BUILDER_INSTRUCTIONS_EN
    assert "The clip is ~5 seconds" not in text
    assert "HARD CUT" in text
