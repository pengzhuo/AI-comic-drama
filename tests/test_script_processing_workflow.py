"""剧本处理工作流：分段、作用域注入、超窗恢复、归并与落盘。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import (
    ContentFilterError,
    ModelHTTPError,
    UnexpectedModelBehavior,
)
from pydantic_ai.models.test import TestModel

from novelvideo import script_process as script_process_store
from novelvideo.skills import ai_film_process
from novelvideo.utils.model_context_budget import (
    ModelContextBudgetExceeded,
    TextContextBudget,
    estimate_tokens,
)
from novelvideo.workflows import script_processing


class _FakeAgent:
    def __init__(self, outputs: list) -> None:
        self._outputs = list(outputs)
        self.prompts: list[str] = []

    async def run(self, prompt: str):
        self.prompts.append(prompt)
        output = self._outputs.pop(0)
        if isinstance(output, BaseException):
            raise output
        return SimpleNamespace(output=output)


class _AlwaysFails:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.prompts: list[str] = []

    async def run(self, prompt: str):
        self.prompts.append(prompt)
        raise self._exc


class _AlwaysSucceeds:
    def __init__(self, output) -> None:
        self._output = output
        self.prompts: list[str] = []

    async def run(self, prompt: str):
        self.prompts.append(prompt)
        return SimpleNamespace(output=self._output)


def _install_agents(
    monkeypatch: pytest.MonkeyPatch,
    segment_outputs: list,
    digest_outputs: list | None = None,
) -> dict[str, _FakeAgent]:
    agents = {
        "ScriptProcessSegmentOutput": _FakeAgent(segment_outputs),
        "ScriptProcessDigestOutput": _FakeAgent(digest_outputs or []),
    }

    def fake_build_agent(self, output_type, name):
        return agents[output_type.__name__]

    monkeypatch.setattr(
        script_processing.ScriptProcessWorkflow, "_build_agent", fake_build_agent
    )
    return agents


def _install_split_retry_agents(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failures: int,
    exc: BaseException,
) -> _FakeAgent:
    """前 ``failures`` 次分段调用失败，之后每次成功返回一段结果。"""

    agent = _RetryThenSucceed(failures=failures, exc=exc)

    def fake_build_agent(self, output_type, name):
        if output_type is script_processing.ScriptProcessSegmentOutput:
            return agent
        return _AlwaysSucceeds(
            script_processing.ScriptProcessDigestOutput(understanding="归并账")
        )

    monkeypatch.setattr(
        script_processing.ScriptProcessWorkflow, "_build_agent", fake_build_agent
    )
    return agent


class _RetryThenSucceed:
    def __init__(self, *, failures: int, exc: BaseException) -> None:
        self._remaining_failures = failures
        self._exc = exc
        self.prompts: list[str] = []

    async def run(self, prompt: str):
        self.prompts.append(prompt)
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise self._exc
        return SimpleNamespace(
            output=script_processing.ScriptProcessSegmentOutput(
                understanding_delta=f"理解#{len(self.prompts)}",
                production_script=f"剧本#{len(self.prompts)}",
            )
        )


def _segment(understanding: str, script: str, *, name: str = "林建军") -> object:
    return script_processing.ScriptProcessSegmentOutput(
        understanding_delta=understanding,
        production_script=script,
        characters=[script_processing.CharacterFact(name=name)],
        scenes=[script_processing.SceneFact(name="老宅")],
        gaps=[f"{name}的动机不明确"],
    )


#: 在 ``chunk_max_chars=10`` 下正好切成两段的原文。
_TWO_CHUNK_TEXT = "第一段内容一\n第二段内容二\n"

#: 在 ``chunk_max_chars=10`` 下正好切成三段的原文。
_THREE_CHUNK_TEXT = "第一段内容一\n第二段内容二\n第三段内容三\n"

#: 在 ``chunk_max_chars=10`` 下正好切成四段的原文。
_FOUR_CHUNK_TEXT = "第一段内容一\n第二段内容二\n第三段内容三\n第四段内容四\n"

_LONG_TEXT = "\n".join(
    f"第{i}行：这是用来测试预算规划的较长中文内容，需要按上下文预算切分。" for i in range(1, 201)
)


# ── 分段 ──────────────────────────────────────────────────────────────────────


def test_chunk_source_text_keeps_every_line_in_order() -> None:
    text = "\n".join(f"第{i}行" for i in range(1, 101))

    chunks = script_processing.chunk_source_text(text, max_chars=50)

    assert len(chunks) > 1
    recovered = [line for chunk in chunks for line in chunk.split("\n")]
    assert recovered == text.split("\n")
    assert all(len(chunk) <= 50 for chunk in chunks)


def test_chunk_source_text_hard_splits_an_overlong_line() -> None:
    assert script_processing.chunk_source_text("x" * 25, max_chars=10) == [
        "x" * 10,
        "x" * 10,
        "x" * 5,
    ]


def test_chunk_source_text_ignores_blank_input() -> None:
    assert script_processing.chunk_source_text("   \n\n  ") == []


def test_split_chunk_in_half_keeps_order_and_content() -> None:
    text = "第一行\n第二行\n第三行\n"

    left, right = script_processing.split_chunk_in_half(text)

    assert left.split("\n") + right.split("\n") == ["第一行", "第二行", "第三行"]
    assert left and right


def test_split_chunk_in_half_hard_splits_a_single_line() -> None:
    left, right = script_processing.split_chunk_in_half("一二三四五六")

    assert left + right == "一二三四五六"
    assert left and right


# ── 流程包注入（按作用域裁剪） ────────────────────────────────────────────────


def test_script_process_stage_pack_covers_master_control_and_script_parts() -> None:
    parts = ai_film_process.stage_parts("script_process")
    assert parts == ("01", "02")

    pack = ai_film_process.stage_pack("script_process")
    assert "总控与选择" in pack
    assert "剧本与基础调度" in pack
    # 未登记的阶段不注入正文。
    assert ai_film_process.stage_pack("tts") == ""


def test_segment_scope_injects_only_the_script_part_and_digest_gets_master_control() -> None:
    assert ai_film_process.stage_scope_parts("script_process", "segment") == ("02",)
    assert ai_film_process.stage_scope_parts("script_process", "digest") == ("01",)

    segment_pack = ai_film_process.stage_scope_pack("script_process", "segment")
    assert "===== 流程包第02份" in segment_pack
    assert "===== 流程包第01份" not in segment_pack

    digest_pack = ai_film_process.stage_scope_pack("script_process", "digest")
    assert "===== 流程包第01份" in digest_pack
    assert "===== 流程包第02份" not in digest_pack

    # 未登记的作用域退回该阶段的完整正文。
    assert ai_film_process.stage_scope_parts("script_process", "unknown") == ("01", "02")
    assert ai_film_process.stage_scope_parts("tts", "segment") == ()


def test_all_six_parts_are_bundled() -> None:
    for code in ai_film_process.PART_FILES:
        assert ai_film_process.load_part(code).strip()
    assert len(ai_film_process.load_pack()) > len(
        ai_film_process.stage_pack("script_process")
    )


def test_segment_system_prompt_is_much_smaller_than_the_full_pack() -> None:
    workflow = script_processing.ScriptProcessWorkflow(output_dir=".")

    segment_prompt = workflow._system_prompt("segment")
    digest_prompt = workflow._system_prompt("digest")
    full_pack = ai_film_process.stage_pack("script_process")

    assert "剧本与基础调度" in segment_prompt
    assert "总控与选择" in digest_prompt
    assert estimate_tokens(segment_prompt) < estimate_tokens(full_pack) / 2


def test_built_agents_disable_pydantic_output_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        script_processing, "get_newapi_text_pydantic_model", lambda *a, **k: TestModel()
    )
    workflow = script_processing.ScriptProcessWorkflow(
        output_dir=".", max_output_tokens=12_288
    )

    segment_agent = workflow._build_agent(
        script_processing.ScriptProcessSegmentOutput, "分段"
    )
    digest_agent = workflow._build_agent(
        script_processing.ScriptProcessDigestOutput, "归并"
    )

    # 内建输出重试会把上一次的完整响应重新塞进请求，流程包存在时必须关闭。
    assert segment_agent._max_output_retries == 0
    assert digest_agent._max_output_retries == 0
    assert segment_agent.model_settings["max_tokens"] == 12_288
    assert "剧本与基础调度" in segment_agent._system_prompts[0]
    assert "总控与选择" in digest_agent._system_prompts[0]


# ── 预算规划 ──────────────────────────────────────────────────────────────────


def _install_tight_budget(monkeypatch: pytest.MonkeyPatch, context_tokens: int) -> None:
    budget = TextContextBudget(
        context_tokens=context_tokens, default_output_tokens=4096, framing_tokens=1024
    )
    monkeypatch.setattr(script_processing, "context_budget", lambda: budget)


def test_plan_segments_splits_by_the_context_budget_not_only_by_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_tight_budget(monkeypatch, 43_000)
    workflow = script_processing.ScriptProcessWorkflow(
        output_dir=".", chunk_max_chars=100_000, max_output_tokens=4096
    )

    pieces = workflow.plan_segments(_LONG_TEXT, spine_label="剧本")

    assert len(pieces) > 1
    scaffold = workflow._segment_prompt(
        body="", index=1, total=1, spine_label="剧本"
    )
    limit = workflow._body_limit_bytes(
        scope="segment",
        output_type=script_processing.ScriptProcessSegmentOutput,
        scaffold_bytes=estimate_tokens(scaffold),
    )
    assert limit is not None
    assert all(estimate_tokens(piece) <= limit for piece in pieces)
    # 不丢原文：拼回去能覆盖全部行。
    recovered = [line for piece in pieces for line in piece.split("\n") if line]
    assert recovered == [line for line in _LONG_TEXT.split("\n") if line]


def test_plan_segments_rejects_a_budget_that_the_pack_already_exhausts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_tight_budget(monkeypatch, 12_000)
    workflow = script_processing.ScriptProcessWorkflow(
        output_dir=".", chunk_max_chars=100_000, max_output_tokens=4096
    )

    with pytest.raises(ValueError, match="上下文预算不足"):
        workflow.plan_segments(_LONG_TEXT, spine_label="剧本")


# ── 运行 ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_segment_run_writes_the_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agents = _install_agents(
        monkeypatch,
        [_segment("题材：复仇短剧", "第一场：林建军推门进老宅。")],
    )
    workflow = script_processing.ScriptProcessWorkflow(
        output_dir=str(tmp_path), chunk_max_chars=100_000
    )

    result = await workflow.run(source_text="林建军：你回来晚了。\n他推开门。")

    assert result.chunks == 1
    assert result.degraded_chunks == 0
    assert result.understanding == "题材：复仇短剧"
    # 单段不必再花一次归并调用。
    assert agents["ScriptProcessDigestOutput"].prompts == []

    segment_prompt = agents["ScriptProcessSegmentOutput"].prompts[0]
    assert "林建军：你回来晚了。" in segment_prompt

    stored = script_process_store.load_script_process_result(tmp_path)
    assert stored is not None
    assert stored["pack_version"] == ai_film_process.PACK_VERSION
    assert stored["parts"] == ["01", "02"]
    assert stored["injection"]["segment_parts"] == ["02"]
    assert stored["injection"]["digest_parts"] == ["01"]
    assert stored["understanding"] == "题材：复仇短剧"
    assert stored["characters"][0]["name"] == "林建军"
    assert stored["scenes"][0]["name"] == "老宅"

    production_script = script_process_store.read_production_script(tmp_path)
    assert production_script.startswith("# 结构化制作剧本")
    assert "题材：复仇短剧" in production_script
    assert "林建军推门进老宅" in production_script


@pytest.mark.asyncio
async def test_multiple_segments_are_merged_by_the_digest_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    digest = script_processing.ScriptProcessDigestOutput(
        understanding="全剧理解账",
        characters=[script_processing.CharacterFact(name="林建军", aliases=["建军"])],
        scenes=[script_processing.SceneFact(name="老宅")],
        gaps=["全剧未决项"],
    )
    agents = _install_agents(
        monkeypatch,
        [_segment("第一段理解", "第一段剧本"), _segment("第二段理解", "第二段剧本")],
        [digest],
    )
    workflow = script_processing.ScriptProcessWorkflow(
        output_dir=str(tmp_path), chunk_max_chars=10, concurrency=1
    )

    result = await workflow.run(source_text=_TWO_CHUNK_TEXT)

    assert result.chunks == 2
    assert result.understanding == "全剧理解账"
    assert [item.name for item in result.characters] == ["林建军"]
    assert result.gaps == ["全剧未决项"]
    assert len(agents["ScriptProcessDigestOutput"].prompts) == 1
    # 制作剧本全文由各段结果按顺序拼接，两段都在。
    assert "第一段剧本" in result.production_script
    assert "第二段剧本" in result.production_script


@pytest.mark.asyncio
async def test_content_filtered_segment_is_recorded_as_a_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agents = _install_agents(
        monkeypatch,
        [ContentFilterError("blocked"), _segment("理解", "剧本")],
        [
            script_processing.ScriptProcessDigestOutput(
                understanding="全剧理解账", gaps=["内容审核段需人工复核"]
            )
        ],
    )
    workflow = script_processing.ScriptProcessWorkflow(
        output_dir=str(tmp_path), chunk_max_chars=10, concurrency=1
    )

    result = await workflow.run(source_text=_TWO_CHUNK_TEXT)

    assert result.chunks == 2
    assert result.degraded_chunks == 1
    assert any("内容审核" in gap for gap in result.gaps)
    # 其余段照常处理，任务不因此中断。
    assert len(agents["ScriptProcessSegmentOutput"].prompts) == result.chunks
    assert "第一段内容一" in agents["ScriptProcessSegmentOutput"].prompts[0]


@pytest.mark.asyncio
async def test_empty_source_is_rejected(tmp_path: Path) -> None:
    workflow = script_processing.ScriptProcessWorkflow(output_dir=str(tmp_path))

    with pytest.raises(ValueError):
        await workflow.run(source_text="   ")


@pytest.mark.asyncio
async def test_concurrent_segments_are_assembled_in_source_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """并发完成顺序被打乱时，结果仍按原文顺序组装。"""

    class _SlowFirstAgent:
        def __init__(self, calls: int) -> None:
            self._calls = 0
            self._total = calls
            self.prompts: list[str] = []

        async def run(self, prompt: str):
            ordinal = self._calls
            self._calls += 1
            self.prompts.append(prompt)
            # 先发出的请求反而最慢，确保完成顺序与原文顺序相反。
            await asyncio.sleep((self._total - ordinal) * 0.02)
            return SimpleNamespace(
                output=script_processing.ScriptProcessSegmentOutput(
                    understanding_delta=f"段{ordinal + 1}",
                    production_script=f"剧本{ordinal + 1}",
                )
            )

    agents = {
        "ScriptProcessSegmentOutput": _SlowFirstAgent(calls=3),
        "ScriptProcessDigestOutput": _FakeAgent(
            [script_processing.ScriptProcessDigestOutput(understanding="全剧账")]
        ),
    }

    def fake_build_agent(self, output_type, name):
        return agents[output_type.__name__]

    monkeypatch.setattr(
        script_processing.ScriptProcessWorkflow, "_build_agent", fake_build_agent
    )
    workflow = script_processing.ScriptProcessWorkflow(
        output_dir=str(tmp_path), chunk_max_chars=10, concurrency=3
    )

    result = await workflow.run(source_text=_THREE_CHUNK_TEXT)

    assert result.chunks == 3
    script = result.production_script
    assert script.index("剧本1") < script.index("剧本2") < script.index("剧本3")
    assert result.understanding == "全剧账"
    # 归并输入的批次顺序同样按原文顺序。
    digest_prompt = agents["ScriptProcessDigestOutput"].prompts[0]
    assert digest_prompt.index("段1") < digest_prompt.index("段2") < digest_prompt.index("段3")


@pytest.mark.asyncio
async def test_fatal_segment_error_cancels_the_other_in_flight_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """致命错误（如计费）不该等其余段落跑完才抛出。"""

    class _BillingThenHang:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, prompt: str):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("INSUFFICIENT_CREDITS")
            await asyncio.sleep(30)
            raise AssertionError("不应该等到这一步")

    agent = _BillingThenHang()

    def fake_build_agent(self, output_type, name):
        return agent

    monkeypatch.setattr(
        script_processing.ScriptProcessWorkflow, "_build_agent", fake_build_agent
    )
    workflow = script_processing.ScriptProcessWorkflow(
        output_dir=str(tmp_path), chunk_max_chars=10, concurrency=3
    )

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="INSUFFICIENT_CREDITS"):
        await workflow.run(source_text=_THREE_CHUNK_TEXT)
    # 在飞的调用被取消，而不是等它跑满 30 秒。
    assert time.monotonic() - started < 5


# ── 超窗恢复 ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_context_overflow_splits_the_segment_and_retries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    text = "第一行内容甲\n第二行内容乙\n第三行内容丙\n第四行内容丁\n"
    agent = _install_split_retry_agents(
        monkeypatch, failures=1, exc=ModelContextBudgetExceeded("超窗")
    )
    workflow = script_processing.ScriptProcessWorkflow(
        output_dir=str(tmp_path), chunk_max_chars=100_000, min_chunk_chars=4
    )

    result = await workflow.run(source_text=text)

    # 第一次整段失败 → 拆成两半各跑一次。
    assert len(agent.prompts) == 3
    assert result.chunks == 2
    assert "第四行内容丁" in agent.prompts[0]
    assert text.split("\n")[0] in agent.prompts[1]
    assert text.split("\n")[2] in agent.prompts[2]
    # 两半按顺序拼回去，不丢原文。
    assert "剧本#2" in result.production_script
    assert "剧本#3" in result.production_script


@pytest.mark.asyncio
async def test_structured_output_failure_is_recoverable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    text = "第一行内容甲\n第二行内容乙\n第三行内容丙\n第四行内容丁\n"
    agent = _install_split_retry_agents(
        monkeypatch,
        failures=1,
        exc=UnexpectedModelBehavior("Exceeded maximum output retries (0)"),
    )
    workflow = script_processing.ScriptProcessWorkflow(
        output_dir=str(tmp_path), chunk_max_chars=100_000, min_chunk_chars=4
    )

    result = await workflow.run(source_text=text)

    assert len(agent.prompts) == 3
    assert result.chunks == 2


@pytest.mark.asyncio
async def test_overflow_at_the_chunk_floor_shrinks_the_output_reservation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    text = "第一行内容甲\n第二行内容乙\n"
    agent = _install_split_retry_agents(
        monkeypatch, failures=1, exc=ModelContextBudgetExceeded("超窗")
    )
    workflow = script_processing.ScriptProcessWorkflow(
        output_dir=str(tmp_path),
        chunk_max_chars=100_000,
        min_chunk_chars=100_000,  # 不允许拆分 → 只能降输出预留
        max_output_tokens=16_384,
    )

    result = await workflow.run(source_text=text)

    assert len(agent.prompts) == 2
    assert workflow.output_tokens == 8_192
    assert result.chunks == 1


@pytest.mark.asyncio
async def test_overflow_fails_only_after_both_recovery_paths_are_exhausted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    text = "第一行内容甲\n第二行内容乙\n"
    _install_split_retry_agents(
        monkeypatch, failures=99, exc=ModelContextBudgetExceeded("超窗")
    )
    workflow = script_processing.ScriptProcessWorkflow(
        output_dir=str(tmp_path),
        chunk_max_chars=100_000,
        min_chunk_chars=100_000,
        max_output_tokens=script_processing.SCRIPT_PROCESS_MIN_OUTPUT_TOKENS,
    )

    with pytest.raises(ModelContextBudgetExceeded) as excinfo:
        await workflow.run(source_text=text)

    # 报错要能直接指导排障（窗口/预算），而不是把守卫的原始信息丢给用户。
    assert "SCRIPT_PROCESS_CHUNK_MAX_CHARS" in str(excinfo.value)
    assert "第 1/" in str(excinfo.value)


@pytest.mark.asyncio
async def test_billing_style_errors_are_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_build_agent(self, output_type, name):
        return _AlwaysFails(RuntimeError("INSUFFICIENT_CREDITS"))

    monkeypatch.setattr(
        script_processing.ScriptProcessWorkflow, "_build_agent", fake_build_agent
    )
    workflow = script_processing.ScriptProcessWorkflow(
        output_dir=str(tmp_path), chunk_max_chars=100_000
    )

    with pytest.raises(RuntimeError, match="INSUFFICIENT_CREDITS"):
        await workflow.run(source_text="第一行\n第二行\n")


# ── 上游瞬时故障（网关过载） ──────────────────────────────────────────────────


def _http_503() -> Exception:
    """网关自身的过载保护，与线上日志同形。"""

    return ModelHTTPError(
        503, "DC-literal-beat-meta-LLM", {"code": "system_cpu_overloaded"}
    )


class _TransientThenSucceed:
    def __init__(self, *, failures: int, exc: BaseException) -> None:
        self._remaining = failures
        self._exc = exc
        self.calls = 0
        self.prompts: list[str] = []

    async def run(self, prompt: str):
        self.calls += 1
        self.prompts.append(prompt)
        if self._remaining > 0:
            self._remaining -= 1
            raise self._exc
        return SimpleNamespace(
            output=script_processing.ScriptProcessSegmentOutput(
                understanding_delta="上游恢复后的理解",
                production_script="上游恢复后的剧本",
            )
        )


def _install_transient_agent(
    monkeypatch: pytest.MonkeyPatch,
    agent,
) -> None:
    def fake_build_agent(self, output_type, name):
        return agent

    monkeypatch.setattr(
        script_processing.ScriptProcessWorkflow, "_build_agent", fake_build_agent
    )


def test_transient_defaults_are_overload_friendly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "SCRIPT_PROCESS_TRANSIENT_ATTEMPTS",
        "SCRIPT_PROCESS_TRANSIENT_BACKOFF_SECONDS",
        "SCRIPT_PROCESS_OVERLOAD_STREAK",
        "SCRIPT_PROCESS_OVERLOAD_COOLDOWN_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    workflow = script_processing.ScriptProcessWorkflow(output_dir=".")

    # 退避比叶子默认（5s/20s）长：CPU 过载需要时间回落。
    assert workflow.transient_attempts == 3
    assert workflow.transient_backoff == (20.0, 60.0, 120.0)
    assert workflow.overload_streak == 3
    assert workflow.overload_cooldown_seconds == 60.0


@pytest.mark.asyncio
async def test_transient_upstream_failure_retries_the_same_chunk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """503（system_cpu_overloaded）原地重试，绝不把片段折半。"""

    agent = _TransientThenSucceed(failures=1, exc=_http_503())
    _install_transient_agent(monkeypatch, agent)
    workflow = script_processing.ScriptProcessWorkflow(
        output_dir=str(tmp_path),
        chunk_max_chars=100_000,
        transient_backoff=(0.0,),
    )
    logs: list[str] = []

    result = await workflow.run(
        source_text="第一行内容甲\n第二行内容乙\n", on_log=logs.append
    )

    assert agent.calls == 2
    # 两次请求完全一致：没有拆小片段，也没有缩输出预留。
    assert agent.prompts[0] == agent.prompts[1]
    assert "第二行内容乙" in agent.prompts[1]
    assert result.chunks == 1
    assert result.degraded_chunks == 0
    assert "上游恢复后的剧本" in result.production_script
    assert any("HTTP 503" in message for message in logs)
    assert any("保持原片段大小重试" in message for message in logs)


@pytest.mark.asyncio
async def test_transient_retries_follow_the_backoff_then_give_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """退避序列按配置走；次数用尽后原样上抛（交给阶段级兜底）。"""

    delays: list[tuple[int, tuple[float, ...]]] = []

    def fake_delay(attempt: int, backoff=()) -> float:
        delays.append((attempt, tuple(backoff)))
        return 0.0

    monkeypatch.setattr(script_processing, "transient_retry_delay", fake_delay)
    agent = _TransientThenSucceed(failures=99, exc=_http_503())
    _install_transient_agent(monkeypatch, agent)
    workflow = script_processing.ScriptProcessWorkflow(
        output_dir=str(tmp_path),
        chunk_max_chars=100_000,
        transient_attempts=3,
        transient_backoff=(1.0, 2.0, 3.0),
    )

    with pytest.raises(ModelHTTPError):
        await workflow.run(source_text="第一行内容甲\n")

    assert agent.calls == 3
    assert delays == [(1, (1.0, 2.0, 3.0)), (2, (1.0, 2.0, 3.0))]


@pytest.mark.asyncio
async def test_transient_exhaustion_does_not_split_the_chunk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """瞬时故障用尽后也不进拆分分支（拆分只会给过载的网关加压）。"""

    agent = _TransientThenSucceed(failures=99, exc=_http_503())
    _install_transient_agent(monkeypatch, agent)
    workflow = script_processing.ScriptProcessWorkflow(
        output_dir=str(tmp_path),
        chunk_max_chars=100_000,
        transient_attempts=2,
        transient_backoff=(0.0,),
    )
    logs: list[str] = []

    with pytest.raises(ModelHTTPError):
        await workflow.run(source_text="第一行内容甲\n第二行内容乙\n", on_log=logs.append)

    # 2 次尝试后直接上抛，没有「已拆分为 … 字重试」这类日志。
    assert agent.calls == 2
    assert not any("拆分为" in message for message in logs)


@pytest.mark.asyncio
async def test_repeated_transient_failures_trigger_an_overload_cooldown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = _TransientThenSucceed(failures=2, exc=_http_503())
    _install_transient_agent(monkeypatch, agent)
    workflow = script_processing.ScriptProcessWorkflow(
        output_dir=str(tmp_path),
        chunk_max_chars=100_000,
        concurrency=2,
        transient_attempts=4,
        transient_backoff=(0.0, 0.0, 0.0, 0.0),
        overload_streak=2,
        overload_cooldown_seconds=60.0,
    )
    logs: list[str] = []

    result = await workflow.run(source_text="第一行内容甲\n", on_log=logs.append)

    assert result.chunks == 1
    assert any("冷却" in message for message in logs)
    # 冷却期内有效并发降为 1。
    assert workflow._limiter.cooling_down is True


@pytest.mark.asyncio
async def test_overload_limiter_serializes_new_calls_while_cooling_down() -> None:
    limiter = script_processing._SegmentLimiter(2, streak=2, cooldown_seconds=5.0)
    assert limiter.note_transient_failure() is False
    assert limiter.note_transient_failure() is True
    assert limiter.cooling_down is True

    first = await limiter.acquire()
    assert first is True
    blocked = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0.05)
    # 串行闸门只放行一个：第二个新调用还在等。
    assert not blocked.done()
    limiter.release(first)
    second = await asyncio.wait_for(blocked, timeout=1)
    assert second is True
    limiter.release(second)

    # 冷却结束后恢复配置并发，成功一次即清零计数。
    short = script_processing._SegmentLimiter(2, streak=1, cooldown_seconds=0.05)
    assert short.note_transient_failure() is True
    await asyncio.sleep(0.1)
    assert short.cooling_down is False
    assert (await short.acquire()) is False
    short.release(False)
    short.note_success()
    assert short._consecutive == 0


# ── 归并降级 ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_digest_failure_falls_back_to_a_deterministic_merge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_agents(
        monkeypatch,
        [
            _segment("第一段理解", "第一段剧本", name="林建军"),
            _segment("第二段理解", "第二段剧本", name="林建军"),
        ],
        [UnexpectedModelBehavior("Exceeded maximum output retries (0)")],
    )
    workflow = script_processing.ScriptProcessWorkflow(
        output_dir=str(tmp_path), chunk_max_chars=10, concurrency=1
    )

    result = await workflow.run(source_text=_TWO_CHUNK_TEXT)

    # 归并失败不该毁掉已经跑完的分段。
    assert result.understanding == "第一段理解\n\n第二段理解"
    assert [item.name for item in result.characters] == ["林建军"]
    assert result.gaps == ["林建军的动机不明确"]
    assert "第一段剧本" in result.production_script
    assert "第二段剧本" in result.production_script


@pytest.mark.asyncio
async def test_digest_batches_when_the_summaries_exceed_the_input_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    segments = [
        _segment(f"第{index}段理解" + "详" * 200, f"第{index}段剧本")
        for index in range(1, 4)
    ]
    digest_agent = _FakeAgent(
        [
            script_processing.ScriptProcessDigestOutput(understanding="第一批账"),
            script_processing.ScriptProcessDigestOutput(understanding="第二批账"),
            script_processing.ScriptProcessDigestOutput(understanding="第三批账"),
            script_processing.ScriptProcessDigestOutput(understanding="全剧账"),
        ]
    )

    def fake_build_agent(self, output_type, name):
        if output_type is script_processing.ScriptProcessDigestOutput:
            return digest_agent
        return _FakeAgent(segments)

    monkeypatch.setattr(
        script_processing.ScriptProcessWorkflow, "_build_agent", fake_build_agent
    )
    workflow = script_processing.ScriptProcessWorkflow(
        output_dir=str(tmp_path),
        chunk_max_chars=10,
        digest_max_input_chars=700,
        concurrency=1,
    )

    result = await workflow.run(source_text=_THREE_CHUNK_TEXT)

    assert len(digest_agent.prompts) > 2
    assert "本批范围内归并" in digest_agent.prompts[0]
    assert "分批归并结果" in digest_agent.prompts[-1]
    assert result.understanding == "全剧账"


@pytest.mark.asyncio
async def test_digest_partial_merge_falls_back_when_the_merge_input_is_too_large(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    segments = [
        _segment(f"第{index}段理解" + "详" * 400, f"第{index}段剧本")
        for index in range(1, 4)
    ]
    partials = ["账甲" * 100, "账乙" * 100, "账丙" * 100]
    digest_agent = _FakeAgent(
        [
            script_processing.ScriptProcessDigestOutput(understanding=partials[0]),
            script_processing.ScriptProcessDigestOutput(understanding=partials[1]),
            script_processing.ScriptProcessDigestOutput(understanding=partials[2]),
        ]
    )

    def fake_build_agent(self, output_type, name):
        if output_type is script_processing.ScriptProcessDigestOutput:
            return digest_agent
        return _FakeAgent(segments)

    monkeypatch.setattr(
        script_processing.ScriptProcessWorkflow, "_build_agent", fake_build_agent
    )
    workflow = script_processing.ScriptProcessWorkflow(
        output_dir=str(tmp_path),
        chunk_max_chars=10,
        digest_max_input_chars=700,
        concurrency=1,
    )

    result = await workflow.run(source_text=_THREE_CHUNK_TEXT)

    # 三批都归并过，但合并输入仍超预算 → 确定性合并（不再多花一次调用）。
    assert len(digest_agent.prompts) == 3
    assert result.understanding == "\n\n".join(partials)


@pytest.mark.asyncio
async def test_failed_digest_batch_is_split_and_other_batches_are_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """某批归并失败时拆小重试，而不是丢掉所有批的结果。"""

    segments = [
        _segment(f"第{index}段理解" + "详" * 420, f"第{index}段剧本")
        for index in range(1, 4)
    ]

    class _FailFirstDigest:
        def __init__(self) -> None:
            self.calls = 0
            self.prompts: list[str] = []

        async def run(self, prompt: str):
            self.calls += 1
            self.prompts.append(prompt)
            if self.calls == 1:
                # 第一批把输出撑爆 → 工作流应该拆批重试。
                raise UnexpectedModelBehavior("Exceeded maximum output retries (0)")
            return SimpleNamespace(
                output=script_processing.ScriptProcessDigestOutput(
                    understanding=f"账{self.calls}"
                )
            )

    digest_agent = _FailFirstDigest()

    def fake_build_agent(self, output_type, name):
        if output_type is script_processing.ScriptProcessDigestOutput:
            return digest_agent
        return _FakeAgent(segments)

    monkeypatch.setattr(
        script_processing.ScriptProcessWorkflow, "_build_agent", fake_build_agent
    )
    workflow = script_processing.ScriptProcessWorkflow(
        output_dir=str(tmp_path),
        chunk_max_chars=10,
        digest_max_input_chars=3200,
        concurrency=1,
    )

    result = await workflow.run(source_text=_THREE_CHUNK_TEXT)

    # 失败批拆成两个子批 + 第二批 + 最后合并。
    assert len(digest_agent.prompts) == 5
    # 拆出来的两个子批各自只覆盖一部分段落，而不是重复整批。
    assert "第 1 段理解" in digest_agent.prompts[1]
    assert "第 1 段理解" not in digest_agent.prompts[2]
    assert result.understanding == "账5"


# ── 运行期热调参 ──────────────────────────────────────────────────────────────


def _clear_script_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SCRIPT_PROCESS_CONCURRENCY",
        "SCRIPT_PROCESS_CHUNK_MAX_CHARS",
        "SCRIPT_PROCESS_SKILL_INJECTION",
        "SCRIPT_PROCESS_PARALLEL_DIGEST",
        "SCRIPT_PROCESS_TUNING_FILE",
        "SCRIPT_PROCESS_DIGEST_SEGMENT_CHARS",
        "SCRIPT_PROCESS_DIGEST_FACTS_MAX_CHARS",
        "NOVELVIDEO_RUNTIME_DIR",
        "NOVELVIDEO_STATE_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


def test_runtime_tuning_file_fills_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """热调参文件在 env 未设时生效，并被工作流读取。"""

    _clear_script_process_env(monkeypatch)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / script_processing.SCRIPT_PROCESS_TUNING_FILENAME).write_text(
        json.dumps(
            {
                "concurrency": 6,
                "chunk_max_chars": 6000,
                "skill_injection": "condensed",
                "parallel_digest": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NOVELVIDEO_RUNTIME_DIR", str(runtime))

    assert script_processing.default_concurrency() == 6
    assert script_processing.default_chunk_max_chars() == 6000
    assert script_processing.default_skill_injection() == "condensed"
    assert script_processing.default_parallel_digest() is False

    workflow = script_processing.ScriptProcessWorkflow(output_dir=str(tmp_path))
    assert workflow.concurrency == 6
    assert workflow.chunk_max_chars == 6000
    assert workflow.skill_injection == "condensed"
    assert workflow.parallel_digest is False


def test_env_wins_over_the_runtime_tuning_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_script_process_env(monkeypatch)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / script_processing.SCRIPT_PROCESS_TUNING_FILENAME).write_text(
        json.dumps({"concurrency": 9, "skill_injection": "condensed"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("NOVELVIDEO_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("SCRIPT_PROCESS_CONCURRENCY", "3")
    monkeypatch.setenv("SCRIPT_PROCESS_SKILL_INJECTION", "full")

    assert script_processing.default_concurrency() == 3
    assert script_processing.default_skill_injection() == "full"


def test_missing_or_corrupt_tuning_file_falls_back_to_constants(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_script_process_env(monkeypatch)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("NOVELVIDEO_RUNTIME_DIR", str(runtime))

    # 缺文件 → 代码常量。
    assert (
        script_processing.default_concurrency()
        == script_processing.SCRIPT_PROCESS_DEFAULT_CONCURRENCY
    )
    assert script_processing.default_parallel_digest() is True

    # 坏文件也不该抛错。
    (runtime / script_processing.SCRIPT_PROCESS_TUNING_FILENAME).write_text(
        "{not json", encoding="utf-8"
    )
    assert (
        script_processing.default_concurrency()
        == script_processing.SCRIPT_PROCESS_DEFAULT_CONCURRENCY
    )
    assert (
        script_processing.default_skill_injection()
        == script_processing.SCRIPT_PROCESS_SKILL_INJECTION_DEFAULT
    )


def test_condensed_injection_uses_the_directive_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``condensed`` 分段注入精简块；归并没有精简块时回落整份 01。"""

    monkeypatch.setattr(ai_film_process, "fits_window", lambda *a, **k: True)
    monkeypatch.delenv("DIRECTOR_PACK_INJECTION", raising=False)

    full = script_processing.ScriptProcessWorkflow(
        output_dir=".", skill_injection="full"
    )
    condensed = script_processing.ScriptProcessWorkflow(
        output_dir=".", skill_injection="condensed"
    )

    full_prompt = full._system_prompt("segment")
    condensed_prompt = condensed._system_prompt("segment")

    assert "===== 流程包第02份" in full_prompt
    assert "===== 流程包第02份" not in condensed_prompt
    assert "【制作流程包规范" in condensed_prompt
    assert "## 事实边界" in condensed_prompt
    assert estimate_tokens(condensed_prompt) < estimate_tokens(full_prompt) / 2

    # 归并作用域没有精简块 → 回落整份第 01 份。
    assert "===== 流程包第01份" in condensed._system_prompt("digest")


def test_classify_digest_failure_splits_skips_or_raises(tmp_path: Path) -> None:
    workflow = script_processing.ScriptProcessWorkflow(output_dir=str(tmp_path))
    blocks = "甲" * 600 + "\n" + "乙" * 600
    logs: list[str] = []

    # 可恢复（结构化输出失败）→ 拆成两个子批。
    children = workflow._classify_digest_failure(
        UnexpectedModelBehavior("Exceeded maximum output retries (0)"),
        group_index=1,
        blocks=blocks,
        log=logs.append,
    )
    assert len(children) == 2
    assert "甲" in children[0]
    assert "乙" in children[1]

    # 内容审核 → 跳过该批。
    assert (
        workflow._classify_digest_failure(
            ContentFilterError("拦截"),
            group_index=2,
            blocks=blocks,
            log=logs.append,
        )
        == []
    )

    # 不可恢复（计费）→ 直接抛出。
    with pytest.raises(RuntimeError, match="INSUFFICIENT_CREDITS"):
        workflow._classify_digest_failure(
            RuntimeError("INSUFFICIENT_CREDITS"),
            group_index=3,
            blocks=blocks,
            log=logs.append,
        )

    # 已经细到不能再拆 → 跳过。
    assert (
        workflow._classify_digest_failure(
            UnexpectedModelBehavior("boom"),
            group_index=4,
            blocks="甲" * 10,
            log=logs.append,
        )
        == []
    )


# ── 归并并发 ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parallel_digest_batches_overlap_and_keep_source_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """并发 > 1 时归并各批并行，但最终顺序与串行版本完全一致。"""

    segments = [
        _segment(f"第{index}段理解" + "详" * 200, f"第{index}段剧本")
        for index in range(1, 5)
    ]
    holder: dict[str, object] = {}

    class _ProbeDigest:
        def __init__(self) -> None:
            self.prompts: list[str] = []
            self.in_flight = 0
            self.max_in_flight = 0

        async def run(self, prompt: str):
            group_match = re.search(r"第 (\d+)/\d+ 批", prompt)
            group_index = int(group_match.group(1)) if group_match else 0
            segment_match = re.search(r"第 (\d+) 段理解", prompt)
            segment_index = int(segment_match.group(1)) if segment_match else 0
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.prompts.append(prompt)
            try:
                await asyncio.sleep(0.05)
            finally:
                self.in_flight -= 1
            return SimpleNamespace(
                output=script_processing.ScriptProcessDigestOutput(
                    understanding=f"账{group_index}-{segment_index}" + "归" * 300
                )
            )

    def fake_build_agent(self, output_type, name):
        if output_type is script_processing.ScriptProcessDigestOutput:
            return holder["probe"]
        return _FakeAgent(list(segments))

    monkeypatch.setattr(
        script_processing.ScriptProcessWorkflow, "_build_agent", fake_build_agent
    )

    async def _exercise(parallel: bool) -> tuple[str, int]:
        probe = _ProbeDigest()
        holder["probe"] = probe
        workflow = script_processing.ScriptProcessWorkflow(
            output_dir=str(tmp_path),
            chunk_max_chars=10,
            digest_max_input_chars=700,
            concurrency=2,
            parallel_digest=parallel,
        )
        result = await workflow.run(source_text=_FOUR_CHUNK_TEXT)
        return result.understanding, probe.max_in_flight

    parallel_understanding, max_in_flight = await _exercise(True)
    serial_understanding, _ = await _exercise(False)

    # 两个批次同时在飞（并发闸门放行 2）。
    assert max_in_flight >= 2
    # 并行与串行的合并结果逐字一致（顺序被还原）。
    assert parallel_understanding == serial_understanding
    prefixes = [
        part.split("归", 1)[0] for part in parallel_understanding.split("\n\n")
    ]
    groups = [int(prefix[1:].split("-", 1)[0]) for prefix in prefixes]
    assert len(groups) >= 2
    assert groups == sorted(groups)


# ── 归并输入瘦身 ──────────────────────────────────────────────────────────


def test_default_digest_segment_chars_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_script_process_env(monkeypatch)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("NOVELVIDEO_RUNTIME_DIR", str(runtime))

    assert (
        script_processing.default_digest_segment_chars()
        == script_processing.SCRIPT_PROCESS_DIGEST_SEGMENT_CHARS
    )
    (runtime / script_processing.SCRIPT_PROCESS_TUNING_FILENAME).write_text(
        json.dumps({"digest_segment_chars": 900}), encoding="utf-8"
    )
    assert script_processing.default_digest_segment_chars() == 900
    monkeypatch.setenv("SCRIPT_PROCESS_DIGEST_SEGMENT_CHARS", "1500")
    assert script_processing.default_digest_segment_chars() == 1500


def test_default_digest_facts_max_chars_allows_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_script_process_env(monkeypatch)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("NOVELVIDEO_RUNTIME_DIR", str(runtime))

    assert script_processing.default_digest_facts_max_chars() == 0
    (runtime / script_processing.SCRIPT_PROCESS_TUNING_FILENAME).write_text(
        json.dumps({"digest_facts_max_chars": 4000}), encoding="utf-8"
    )
    assert script_processing.default_digest_facts_max_chars() == 4000
    (runtime / script_processing.SCRIPT_PROCESS_TUNING_FILENAME).write_text(
        json.dumps({"digest_facts_max_chars": 0}), encoding="utf-8"
    )
    assert script_processing.default_digest_facts_max_chars() == 0
    monkeypatch.setenv("SCRIPT_PROCESS_DIGEST_FACTS_MAX_CHARS", "2500")
    assert script_processing.default_digest_facts_max_chars() == 2500


def test_slim_facts_keeps_valid_json_and_shrinks() -> None:
    payload = {
        "characters": [
            {
                "name": "萧忆情",
                "aliases": [f"别名{i}" for i in range(20)],
                "role": "主" * 400,
                "first_appearance": "第 1 段",
                "notes": "备注" * 400,
            }
            for _ in range(6)
        ],
        "scenes": [
            {"name": "听雪楼", "time_of_day": "夜", "description": "描述" * 400}
            for _ in range(6)
        ],
        "gaps": [f"未决问题 {i} " * 20 for i in range(30)],
    }
    big = len(json.dumps(payload, ensure_ascii=False))
    slim = script_processing.ScriptProcessWorkflow._slim_facts(payload, 3000)
    small = len(json.dumps(slim, ensure_ascii=False))
    assert small <= 3000 < big
    assert isinstance(json.loads(json.dumps(slim, ensure_ascii=False)), dict)
    assert set(slim) == {"characters", "scenes", "gaps"}
    assert all(len(item["aliases"]) <= 3 for item in slim["characters"])
    assert len(slim["characters"][0]["notes"]) <= 121

    # 0 = 原样返回（历史行为）。
    assert script_processing.ScriptProcessWorkflow._slim_facts(payload, 0) is payload


def test_digest_groups_shrink_with_slimming() -> None:
    workflow = script_processing.ScriptProcessWorkflow(output_dir=".")
    segments = [
        script_processing.ScriptProcessSegmentOutput(
            understanding_delta="理解" * 800,
            characters=[
                script_processing.CharacterFact(
                    name=f"角色{i}",
                    aliases=["a", "b"],
                    role="主" * 300,
                    notes="备注" * 300,
                )
            ],
            scenes=[
                script_processing.SceneFact(name=f"场景{i}", description="描述" * 300)
            ],
            gaps=["未决"],
        )
        for i in range(8)
    ]
    limit = 24000
    fat = workflow._digest_groups(segments, limit)
    workflow.digest_segment_chars = 600
    workflow.digest_facts_max_chars = 800
    slim = workflow._digest_groups(segments, limit)
    assert slim
    assert len(slim) < len(fat)
