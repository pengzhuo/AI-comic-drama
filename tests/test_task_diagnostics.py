"""Tests for task-scoped failure diagnostics."""

from __future__ import annotations

import contextvars
import json

from novelvideo import task_diagnostics as td


def test_off_task_calls_are_noops() -> None:
    assert td.current_task_diagnostics() is None
    # These must not raise outside a task execution scope.
    td.note_operation("should be ignored")
    td.note_model_call(model="gpt-x")
    assert td.current_task_diagnostics() is None


def test_scope_captures_operations_and_model_calls() -> None:
    with td.task_diagnostics_scope(
        task_type="global_optimize_video",
        episode=2,
        beat_num=3,
        scope="ep2",
        project_id="proj",
        task_id="tid",
    ):
        td.note_operation("正在全局优化镜头 1/5")
        td.note_operation("正在全局优化镜头 2/5")
        td.note_model_call(
            model="openai/gemini-2.5-pro",
            capability="render",
            request_id="req_abc",
            source="pydantic_ai",
            status="ok",
        )
        record = td.current_task_diagnostics()
        assert record is not None
        snapshot = record.snapshot()

    assert snapshot["operation"] == "正在全局优化镜头 2/5"
    assert snapshot["recent_operations"] == [
        "正在全局优化镜头 1/5",
        "正在全局优化镜头 2/5",
    ]
    assert snapshot["task_type"] == "global_optimize_video"
    assert snapshot["episode"] == 2
    assert snapshot["beat_num"] == 3
    assert snapshot["task_id"] == "tid"
    assert len(snapshot["model_calls"]) == 1
    call = snapshot["model_calls"][0]
    assert call["model"] == "openai/gemini-2.5-pro"
    assert call["request_id"] == "req_abc"
    assert call["status"] == "ok"


def test_state_is_shared_across_copied_context() -> None:
    """Async code runs in a copied context; the record object must still be shared."""

    with td.task_diagnostics_scope(task_type="video", episode=1, task_id="tid"):
        contextvars.copy_context().run(td.note_operation, "done in copied context")
        record = td.current_task_diagnostics()
        assert record is not None
        assert record.operation == "done in copied context"


def test_failure_diagnostics_redacts_secrets_and_keeps_evidence() -> None:
    with td.task_diagnostics_scope(task_type="video", episode=1, task_id="tid"):
        td.note_operation("上传结果")
        try:
            raise RuntimeError(
                "provider 500: api_key=sk-supersecret123 token: abcdefghij"
            )
        except RuntimeError as exc:
            payload = td.build_failure_diagnostics(exc, task_id="tid")

    blob = json.dumps(payload, ensure_ascii=False)
    assert "supersecret" not in blob
    assert "abcdefghij" not in blob
    assert payload["exception_type"] == "RuntimeError"
    assert payload["operation"] == "上传结果"
    assert "RuntimeError" in payload["traceback"]
    assert payload["task_id"] == "tid"


def test_failure_diagnostics_without_scope() -> None:
    exc = ValueError("boom")
    payload = td.build_failure_diagnostics(exc, task_id="abc")
    assert payload["exception_type"] == "ValueError"
    assert payload["message"] == "boom"
    assert payload["task_id"] == "abc"
    assert "operation" not in payload


def test_model_calls_and_operations_are_bounded() -> None:
    with td.task_diagnostics_scope(task_type="video", episode=1, task_id="tid"):
        for index in range(td._MAX_MODEL_CALLS + 5):
            td.note_model_call(model=f"model-{index}")
        for index in range(td._MAX_OPERATIONS + 5):
            td.note_operation(f"step {index}")
        record = td.current_task_diagnostics()
        assert record is not None
        assert len(record.model_calls) == td._MAX_MODEL_CALLS
        assert record.model_calls[-1].model == f"model-{td._MAX_MODEL_CALLS + 4}"
        assert len(record.operations) == td._MAX_OPERATIONS
        assert record.operations[-1] == f"step {td._MAX_OPERATIONS + 4}"


def test_scope_is_popped_on_exit() -> None:
    with td.task_diagnostics_scope(task_type="video", episode=1, task_id="tid"):
        assert td.current_task_diagnostics() is not None
    assert td.current_task_diagnostics() is None


def test_summarize_failure_reports_operation_and_model() -> None:
    summary = td.summarize_failure(
        {
            "operation": "正在全局优化镜头 3/5",
            "model_calls": [
                {"model": "gemini-2.5-pro", "request_id": "req_9", "status": "ok"}
            ],
        }
    )
    assert "正在全局优化镜头 3/5" in summary
    assert "gemini-2.5-pro" in summary
    assert "req_9" in summary


def test_summarize_failure_marks_failed_model_call() -> None:
    summary = td.summarize_failure(
        {"model_calls": [{"model": "index-tts-2", "status": "error"}]}
    )
    assert "index-tts-2" in summary
    assert "失败" in summary


def test_summarize_failure_falls_back_to_message() -> None:
    assert td.summarize_failure({"message": "provider 500"}) == "provider 500"
    assert td.summarize_failure(None) == ""
    assert td.summarize_failure({}) == ""


def test_failure_reason_with_detail_appends_summary() -> None:
    from novelvideo.chat.director_auto import _failure_reason_with_detail

    class _Task:
        metadata = {
            "failure": {
                "operation": "全局优化",
                "model_calls": [{"model": "gemini-2.5-pro", "status": "ok"}],
            }
        }
        result = None

    reason = _failure_reason_with_detail(_Task(), "任务失败")
    assert reason.startswith("任务失败（")
    assert "全局优化" in reason
    assert "gemini-2.5-pro" in reason

    class _Plain:
        metadata = {}
        result = None

    assert _failure_reason_with_detail(_Plain(), "任务失败") == "任务失败"
