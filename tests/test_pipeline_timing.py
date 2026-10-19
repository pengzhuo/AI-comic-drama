"""流水线耗时记录。

存在的理由很具体：实测一集里首帧阶段的 49 次生成累计只花了 1.5 小时计算，却铺成
10.2 小时墙钟——瓶颈是阶段之间的串行等待，而不是模型速度。这类问题必须能被量出来，
否则「提速」永远只是感觉。
"""

from pathlib import Path

import pytest

from novelvideo import pipeline_timing as timing


def _events(tmp_path: Path) -> list[dict]:
    return timing.load_events(tmp_path)


def test_record_event_appends_one_json_line_per_event(tmp_path: Path) -> None:
    timing.record_event(tmp_path, "dispatch", stage="single_video", signature="s1")
    timing.record_event(tmp_path, "busy_start")

    events = _events(tmp_path)

    assert [event["event"] for event in events] == ["dispatch", "busy_start"]
    assert events[0]["stage"] == "single_video"
    assert events[0]["signature"] == "s1"
    assert isinstance(events[0]["ts"], float)
    assert events[0]["iso"]


def test_record_event_never_raises_on_an_unwritable_path(tmp_path: Path) -> None:
    # 计时是观测手段，不能因为 IO 问题把产线带崩。
    timing.record_event(tmp_path / "nul" / "x", "dispatch")
    timing.record_event(None, "dispatch")

    assert _events(tmp_path) == []


def test_summarize_reports_busy_share_and_overlapped_dispatches(tmp_path: Path) -> None:
    path = timing.timing_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                '{"ts": 1000.0, "event": "busy_start"}',
                '{"ts": 1000.0, "event": "dispatch", "stage": "selected_regen"}',
                (
                    '{"ts": 1100.0, "event": "dispatch", "stage": "single_video",'
                    ' "overlapped": true}'
                ),
                '{"ts": 1300.0, "event": "busy_end", "duration_seconds": 300.0}',
                '{"ts": 1400.0, "event": "dispatch", "stage": "compose_episode"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = timing.summarize(tmp_path)

    assert summary["events"] == 5
    assert summary["wall_seconds"] == 400.0
    assert summary["busy_seconds"] == 300.0
    # 繁忙占比就是判断调度优劣最直接的那个数：旧链路的串行等待会把它压得很低。
    assert summary["utilization"] == pytest.approx(0.75)
    assert summary["stages"]["single_video"]["dispatches"] == 1
    assert summary["stages"]["single_video"]["overlapped_dispatches"] == 1
    assert summary["stages"]["selected_regen"]["overlapped_dispatches"] == 0


def test_summarize_is_empty_but_total_for_a_missing_log(tmp_path: Path) -> None:
    assert timing.summarize(tmp_path) == {"events": 0}


def test_load_events_tolerates_a_truncated_line(tmp_path: Path) -> None:
    path = timing.timing_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"ts": 1.0, "event": "busy_start"}\n{"ts": 2.0, "event": "bu',
        encoding="utf-8",
    )

    events = _events(tmp_path)

    assert [event["event"] for event in events] == ["busy_start"]


def test_timing_path_lives_beside_the_project_runtime_state(tmp_path: Path) -> None:
    assert timing.timing_path(tmp_path) == (
        tmp_path / "runtime" / "pipeline_timing.jsonl"
    )
