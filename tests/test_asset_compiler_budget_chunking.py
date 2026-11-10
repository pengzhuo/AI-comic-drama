"""Regression tests: oversized scene blocks must be chunked, not truncated.

A headerless novel imports as a *single* scene block. Prop / derived-scene
analysis used to send that whole block in one model request, which overflows the
context window and fails fatally with ``MODEL_CONTEXT_BUDGET_EXCEEDED``. These
tests pin the fix: when a block does not fit, split it within the budget, run
the per-chunk request, and merge the results (no content dropped).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from novelvideo.utils.model_context_budget import TextContextBudget


# ~6.3 KB per paragraph: comfortably below the forced chunk budget below, but a
# pair of paragraphs is over it, so each paragraph becomes its own chunk.
FILLER = "情节推进描写。" * 300


class _FakeSQLiteStore:
    async def list_props(self) -> list:
        return []

    async def get_prop(self, name: str):
        return None


class _FakeCogneeStore:
    def __init__(self) -> None:
        self.sqlite_store = _FakeSQLiteStore()

    async def update_episode(self, episode_number: int, **updates) -> None:
        raise AssertionError("chunked analysis must not write menus on its own")


def _force_small_budget(
    monkeypatch, *, context_tokens: int = 40_000, output: int = 1024, framing: int = 256
) -> TextContextBudget:
    """Force ``TextContextBudget.from_env`` to a small window so blocks split."""

    budget = TextContextBudget(
        context_tokens=context_tokens,
        default_output_tokens=output,
        framing_tokens=framing,
    )
    monkeypatch.setattr(
        TextContextBudget,
        "from_env",
        classmethod(lambda cls, *args, **kwargs: budget),
    )
    return budget


def _patch_model(monkeypatch, asset_compiler, agent_cls) -> None:
    monkeypatch.setattr(asset_compiler, "Agent", agent_cls)
    monkeypatch.setattr(
        asset_compiler, "get_newapi_text_pydantic_model", lambda *a, **k: "test-model"
    )
    monkeypatch.setattr(
        asset_compiler,
        "get_newapi_structured_output_model_settings",
        lambda: {"openai_reasoning_effort": "none"},
    )


@pytest.mark.asyncio
async def test_oversized_prop_block_is_chunked_and_merged(monkeypatch):
    import novelvideo.agents.asset_compiler as asset_compiler

    _force_small_budget(monkeypatch)
    markers = ["甲道具", "乙道具", "丙道具", "丁道具"]
    calls: list[str] = []

    class FakeAgent:
        def __init__(self, model, **kwargs):
            self.kwargs = kwargs

        async def run(self, task: str):
            calls.append(task)
            reqs: list[dict] = []
            if "通用道具" in task:
                reqs.append({"prop_name": "通用道具", "visual_prompt": "通用外形"})
            for marker in markers:
                if marker in task:
                    reqs.append(
                        {"prop_name": marker, "visual_prompt": f"{marker}外形"}
                    )
            output_type = self.kwargs["output_type"]
            return SimpleNamespace(
                output=output_type.model_validate(
                    {"requirements": reqs},
                    context=self.kwargs["validation_context"],
                )
            )

    _patch_model(monkeypatch, asset_compiler, FakeAgent)

    lines = [
        f"第{i}段 {marker}。通用道具。{FILLER}"
        for i, marker in enumerate(markers, start=1)
    ]
    block = SimpleNamespace(header_line="第一幕 深夜 内", lines=lines)

    compiler = asset_compiler.AssetCompiler(_FakeCogneeStore())
    requirements = await compiler._analyze_block_props(block, [], [])

    assert len(calls) > 1, "an oversized block must be split into several requests"
    names = [req.prop_name for req in requirements]
    assert set(names) == {"通用道具", *markers}
    assert names.count("通用道具") == 1, "duplicate props must be merged"
    assert len(names) == len(set(names))


@pytest.mark.asyncio
async def test_small_prop_block_uses_a_single_request(monkeypatch):
    import novelvideo.agents.asset_compiler as asset_compiler

    calls: list[str] = []

    class FakeAgent:
        def __init__(self, model, **kwargs):
            self.kwargs = kwargs

        async def run(self, task: str):
            calls.append(task)
            output_type = self.kwargs["output_type"]
            return SimpleNamespace(
                output=output_type.model_validate(
                    {
                        "requirements": [
                            {"prop_name": "羊皮笔记本", "visual_prompt": "泛黄硬皮"}
                        ]
                    },
                    context=self.kwargs["validation_context"],
                )
            )

    _patch_model(monkeypatch, asset_compiler, FakeAgent)

    lines = ["△昏暗地下室里尘埃翻涌。", "陆辰翻开羊皮笔记本。"]
    block = SimpleNamespace(header_line="1-1、地下室 深夜 内", lines=lines)

    compiler = asset_compiler.AssetCompiler(_FakeCogneeStore())
    requirements = await compiler._analyze_block_props(block, [], [])

    assert len(calls) == 1, "a block that fits must stay a single request"
    assert [req.prop_name for req in requirements] == ["羊皮笔记本"]


@pytest.mark.asyncio
async def test_oversized_derived_scene_block_is_chunked_and_merged(monkeypatch):
    import novelvideo.agents.asset_compiler as asset_compiler

    _force_small_budget(monkeypatch)
    markers = ["雪景", "战后版", "雨夜"]
    calls: list[str] = []

    class FakeAgent:
        def __init__(self, model, **kwargs):
            self.kwargs = kwargs

        async def run(self, task: str):
            calls.append(task)
            scenes: list[dict] = []
            if "黄昏" in task:
                scenes.append({"label": "黄昏", "description": "暖色斜阳"})
            for marker in markers:
                if marker in task:
                    scenes.append({"label": marker, "description": f"{marker}状态"})
            output_type = self.kwargs["output_type"]
            return SimpleNamespace(
                output=output_type.model_validate({"derived_scenes": scenes})
            )

    _patch_model(monkeypatch, asset_compiler, FakeAgent)

    lines = [f"第{i}段 {marker}。黄昏。{FILLER}" for i, marker in enumerate(markers, 1)]
    block = SimpleNamespace(header_line="第一幕 深夜 内", lines=lines)

    compiler = asset_compiler.AssetCompiler(_FakeCogneeStore())
    scenes = await compiler._analyze_derived_scenes("地下室", block)

    assert len(calls) > 1
    labels = [scene.label for scene in scenes]
    assert set(labels) == {"黄昏", *markers}
    assert labels.count("黄昏") == 1
    assert len(labels) == len(set(labels))


def test_prompt_budget_reserve_accounts_for_system_prompt_and_schema():
    from pydantic import BaseModel

    from novelvideo.agents.asset_compiler import _prompt_budget_reserve

    class _Schema(BaseModel):
        some_long_field_name: str = "x" * 200

    budget = TextContextBudget(
        context_tokens=131_072, default_output_tokens=8192, framing_tokens=1024
    )
    base = budget.framing_tokens + budget.default_output_tokens

    reserve = _prompt_budget_reserve(budget, "SYSTEM" * 100, _Schema)
    assert reserve > base
    assert reserve > _prompt_budget_reserve(budget, "SYSTEM", _Schema)
    assert _prompt_budget_reserve(budget, "SYSTEM", _Schema) > _prompt_budget_reserve(
        budget, "SYSTEM", None
    )
