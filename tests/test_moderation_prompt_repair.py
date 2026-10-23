"""内容审核补救：审核类失败必须先改写提示词，才允许重投一次。

覆盖 ``chat/moderation_prompt_repair.py`` 的确定性去敏、重新生成与留痕，
以及 ``chat/director_auto.py`` 里「审核失败 → 改词 → 重投一次 / 改不出就暂停」
的收口逻辑。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from novelvideo.chat.moderation_prompt_repair import (
    MODERATION_KIND_INPUT_IMAGE,
    MODERATION_KIND_INPUT_TEXT,
    MODERATION_KIND_OUTPUT_VIDEO,
    MODERATION_KIND_UNKNOWN,
    MODERATION_REPAIR_NOTE,
    MODERATION_REPAIR_PROMPT_SOURCE,
    classify_moderation_text,
    moderation_repair_recovery_key,
    repair_prompt_for_moderation,
)
from novelvideo.prompt_safety import (
    DESENSITIZE_ALREADY_TRIED_MARKERS,
    is_desensitize_exhausted,
)
from novelvideo.seedance2_i2v.models import parse_seedance2_config

SENSITIVE_PROMPT = "保安胸前工牌特写，编号030756清晰可见，湿透马尾，微微喘息"


class _FakeStore:
    """只实现补救路径用到的最小读写面。"""

    def __init__(self, beats: list[dict[str, Any]]) -> None:
        self.beats = beats
        self.updates: list[dict[str, Any]] = []

    async def get_script_as_dict(self, episode_number: int) -> dict[str, Any]:
        return {
            "episode_number": episode_number,
            "beats": self.beats,
            "prop_menu": [],
        }

    async def update_beat_asset(
        self,
        episode_number: int,
        beat_number: int | None = None,
        seedance2_config_json: str | None = None,
        **_kwargs: Any,
    ) -> bool:
        self.updates.append(
            {
                "episode": episode_number,
                "beat": beat_number,
                "config": seedance2_config_json,
            }
        )
        for beat in self.beats:
            if int(beat.get("beat_number") or 0) == int(beat_number or 0):
                beat["seedance2_config_json"] = seedance2_config_json
        return True


def _beat(number: int, prompt: str) -> dict[str, Any]:
    return {
        "beat_number": number,
        "visual_description": "保安在门口拦人",
        "seedance2_config_json": json.dumps(
            {
                "final_prompt": prompt,
                "prompt_source": "generated",
                "prompt_inputs_hash": "abc",
            },
            ensure_ascii=False,
        ),
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("InputTextSensitiveContentDetected", MODERATION_KIND_INPUT_TEXT),
        (
            "OutputVideoSensitiveContentDetected.PolicyViolation",
            MODERATION_KIND_OUTPUT_VIDEO,
        ),
        (
            "The output video may contain sensitive information",
            MODERATION_KIND_OUTPUT_VIDEO,
        ),
        ("copyright restrictions apply", MODERATION_KIND_OUTPUT_VIDEO),
        (
            "InputImageSensitiveContentDetected.PolicyViolation",
            MODERATION_KIND_INPUT_IMAGE,
        ),
        ("图片生成结果未通过内容审核，请调整提示词后重试", MODERATION_KIND_UNKNOWN),
    ],
)
def test_classify_moderation_text(text: str, expected: str) -> None:
    assert classify_moderation_text(text) == expected


async def test_input_text_repair_uses_deterministic_desensitize(tmp_path: Path) -> None:
    store = _FakeStore([_beat(12, SENSITIVE_PROMPT)])

    outcome = await repair_prompt_for_moderation(
        kind=MODERATION_KIND_INPUT_TEXT,
        store=store,
        state_dir=tmp_path,
        project_dir=tmp_path,
        episode=6,
        beat_num=12,
    )

    assert outcome.repaired is True
    assert outcome.new_prompt != outcome.previous_prompt
    assert "030756" not in outcome.new_prompt
    assert "喘息" not in outcome.new_prompt
    assert "湿透" not in outcome.new_prompt
    # 必须真的落库，否则重投拿到的还是旧提示词。
    assert store.updates and store.updates[-1]["beat"] == 12
    saved = parse_seedance2_config(store.updates[-1]["config"])
    assert saved.final_prompt == outcome.new_prompt
    assert saved.prompt_source == MODERATION_REPAIR_PROMPT_SOURCE
    ledger = (tmp_path / "moderation_repairs.jsonl").read_text(encoding="utf-8")
    record = json.loads(ledger.splitlines()[-1])
    assert record["beat_num"] == 12
    assert record["previous_prompt"] == SENSITIVE_PROMPT
    assert record["new_prompt"] == outcome.new_prompt


async def test_input_text_repair_escalates_when_nothing_to_desensitize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    """规则表没命中时不能「原样重投」，必须升级为重新生成。"""

    store = _FakeStore([_beat(3, "纯中性提示词，无高敏表述")])
    calls: list[str] = []

    async def _fake_prompt_for_panel(**kwargs: Any) -> str:
        calls.append(str(kwargs.get("moderation_repair_note") or ""))
        return json.dumps(
            {
                "final_prompt": "纯中性提示词，无高敏表述，且画面不出现可辨识文字",
                "prompt_source": "generated",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(
        "novelvideo.seedance2_i2v.panel_service.generate_seedance2_prompt_for_panel",
        _fake_prompt_for_panel,
    )

    outcome = await repair_prompt_for_moderation(
        kind=MODERATION_KIND_UNKNOWN,
        store=store,
        state_dir=tmp_path,
        project_dir=tmp_path,
        episode=1,
        beat_num=3,
    )

    assert outcome.repaired is True
    assert calls == [MODERATION_REPAIR_NOTE]
    assert outcome.new_prompt.endswith("画面不出现可辨识文字")


async def test_output_video_repair_regenerates_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _FakeStore([_beat(12, SENSITIVE_PROMPT)])

    async def _fake_prompt_for_panel(**kwargs: Any) -> str:
        assert kwargs["moderation_repair_note"] == MODERATION_REPAIR_NOTE
        return json.dumps(
            {
                "final_prompt": "保安在门口抬手拦人，胸牌文字模糊不可辨识",
                "prompt_source": "generated",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(
        "novelvideo.seedance2_i2v.panel_service.generate_seedance2_prompt_for_panel",
        _fake_prompt_for_panel,
    )

    outcome = await repair_prompt_for_moderation(
        kind=MODERATION_KIND_OUTPUT_VIDEO,
        store=store,
        state_dir=tmp_path,
        project_dir=tmp_path,
        episode=6,
        beat_num=12,
    )

    assert outcome.repaired is True
    assert outcome.detail == "已重新生成提示词"
    assert parse_seedance2_config(store.updates[-1]["config"]).final_prompt == (
        outcome.new_prompt
    )


async def test_output_video_repair_rejects_rule_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """composer 失败退回规则草稿时不算修复：那不是为规避审核做的改写。"""

    store = _FakeStore([_beat(12, SENSITIVE_PROMPT)])

    async def _fake_prompt_for_panel(**_kwargs: Any) -> str:
        return json.dumps(
            {"final_prompt": SENSITIVE_PROMPT, "prompt_source": "fallback"},
            ensure_ascii=False,
        )

    monkeypatch.setattr(
        "novelvideo.seedance2_i2v.panel_service.generate_seedance2_prompt_for_panel",
        _fake_prompt_for_panel,
    )

    outcome = await repair_prompt_for_moderation(
        kind=MODERATION_KIND_OUTPUT_VIDEO,
        store=store,
        state_dir=tmp_path,
        project_dir=tmp_path,
        episode=6,
        beat_num=12,
    )

    assert outcome.repaired is False
    assert store.updates == []


async def test_input_image_repair_is_refused(tmp_path: Path) -> None:
    store = _FakeStore([_beat(12, SENSITIVE_PROMPT)])

    outcome = await repair_prompt_for_moderation(
        kind=MODERATION_KIND_INPUT_IMAGE,
        store=store,
        state_dir=tmp_path,
        project_dir=tmp_path,
        episode=6,
        beat_num=12,
    )

    assert outcome.repaired is False
    assert "参考图" in outcome.detail
    assert store.updates == []


async def test_repair_reports_change_when_regeneration_is_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _FakeStore([_beat(12, SENSITIVE_PROMPT)])

    async def _fake_prompt_for_panel(**_kwargs: Any) -> str:
        return json.dumps(
            {"final_prompt": SENSITIVE_PROMPT, "prompt_source": "generated"},
            ensure_ascii=False,
        )

    monkeypatch.setattr(
        "novelvideo.seedance2_i2v.panel_service.generate_seedance2_prompt_for_panel",
        _fake_prompt_for_panel,
    )

    outcome = await repair_prompt_for_moderation(
        kind=MODERATION_KIND_OUTPUT_VIDEO,
        store=store,
        state_dir=tmp_path,
        project_dir=tmp_path,
        episode=6,
        beat_num=12,
    )

    assert outcome.repaired is False
    assert "与原来一致" in outcome.detail
    assert store.updates == []


async def test_repair_reports_missing_beat(tmp_path: Path) -> None:
    store = _FakeStore([_beat(1, "别的镜头")])

    outcome = await repair_prompt_for_moderation(
        kind=MODERATION_KIND_INPUT_TEXT,
        store=store,
        state_dir=tmp_path,
        project_dir=tmp_path,
        episode=6,
        beat_num=99,
    )

    assert outcome.repaired is False
    assert "找不到" in outcome.detail


def test_recovery_key_is_per_beat() -> None:
    assert moderation_repair_recovery_key(6, 12) == "moderation-prompt-repair:6:12"


def test_desensitize_exhausted_markers_match_video_generator_wording() -> None:
    """标记字面量与提交层的报错文案必须一致，否则这档优化会静默失效。"""

    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "novelvideo"
        / "generators"
        / "video_generator.py"
    ).read_text(encoding="utf-8")
    for marker in DESENSITIZE_ALREADY_TRIED_MARKERS:
        assert marker in source
    assert is_desensitize_exhausted("已尝试中性化改写仍被拒") is True
    assert is_desensitize_exhausted("InputTextSensitiveContentDetected") is False


async def test_input_text_repair_skips_a_doomed_desensitize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """提交层已用同一套规则抢救过仍被拒时，别再重放一次必然失败的改写。"""

    store = _FakeStore([_beat(12, SENSITIVE_PROMPT)])
    calls: list[str] = []

    async def _fake_prompt_for_panel(**kwargs: Any) -> str:
        calls.append(str(kwargs.get("moderation_repair_note") or ""))
        return json.dumps(
            {"final_prompt": "改写后的中性提示词", "prompt_source": "generated"},
            ensure_ascii=False,
        )

    monkeypatch.setattr(
        "novelvideo.seedance2_i2v.panel_service.generate_seedance2_prompt_for_panel",
        _fake_prompt_for_panel,
    )

    outcome = await repair_prompt_for_moderation(
        kind=MODERATION_KIND_INPUT_TEXT,
        store=store,
        state_dir=tmp_path,
        project_dir=tmp_path,
        episode=6,
        beat_num=12,
        raw_error=(
            "上游内容审核拒绝了本镜提示词（已尝试中性化改写仍被拒），"
            "请修改本 beat 的画面描述后重试。"
        ),
    )

    assert outcome.repaired is True
    assert calls == [MODERATION_REPAIR_NOTE]
    assert "030756" not in outcome.new_prompt
