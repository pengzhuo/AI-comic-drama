"""流水线阶段耗时记录（append-only JSONL）。

提速不能靠感觉。整条链路此前最反常的一幕是：首帧阶段的 49 次生成累计只花了
1.5 小时计算，却铺成了 10.2 小时墙钟——绝大部分时间花在「等这一阶段彻底结束才
开始下一阶段」上。这类问题只有把每段的「何时派出、何时清空、是否与其它阶段重叠」
落成文件，才能在下一跑里验证提速真的生效、以及还剩哪一段在空转。

设计口径：

* **只追加、不重写**：并发派发的情况下追加写最不容易写坏，日志格式也便于直接
  用 ``jq``/pandas 消费；
* **绝不抛异常**：计时是观测手段，任何 IO 问题都不该影响产线；
* **记录墙钟事件，而不是聚合值**：跑完再去求和/求重叠，避免在线聚合的误差。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 相对项目输出目录的落盘位置。
TIMING_RELATIVE_PATH = Path("runtime") / "pipeline_timing.jsonl"


def timing_path(output_dir: str | os.PathLike[str]) -> Path:
    """Return the timing log path for a project output directory."""

    return Path(str(output_dir)) / TIMING_RELATIVE_PATH


def record_event(
    output_dir: str | os.PathLike[str] | None,
    event: str,
    **fields: Any,
) -> None:
    """Append one timing event; never raises.

    ``ts`` 用 epoch 秒（便于直接做差），同时带一个可读的 ``iso`` 便于肉眼看日志。
    """

    if not output_dir:
        return
    now = time.time()
    line = {
        "ts": round(now, 3),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)),
        "event": str(event),
    }
    for key, value in fields.items():
        if value is None:
            continue
        line[key] = value if isinstance(value, (int, float, bool)) else str(value)
    try:
        path = timing_path(output_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - 观测失败不能影响产线
        logger.debug("pipeline timing write failed", exc_info=True)


def load_events(output_dir: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Read back the recorded events (tolerant of a partially written line)."""

    path = timing_path(output_dir)
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
    except OSError:
        return []
    return events


def summarize(output_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Aggregate ``busy``/``idle``/``dispatch`` events into a compact report.

    ``utilization`` 是判断调度优劣最直接的一个数：繁忙墙钟占整体墙钟的比例。
    串行阶段的旧链路在这个数上会明显偏低。
    """

    events = load_events(output_dir)
    if not events:
        return {"events": 0}

    dispatches: list[dict[str, Any]] = []
    busy_seconds = 0.0
    idle_seconds = 0.0
    busy_started: float | None = None
    first_ts = events[0].get("ts")
    last_ts = events[-1].get("ts")

    for event in events:
        name = str(event.get("event") or "")
        ts = event.get("ts")
        if name == "dispatch":
            dispatches.append(event)
        elif name == "busy_start" and busy_started is None and isinstance(ts, (int, float)):
            busy_started = float(ts)
        elif name == "busy_end" and busy_started is not None:
            duration = event.get("duration_seconds")
            if isinstance(duration, (int, float)):
                busy_seconds += float(duration)
            elif isinstance(ts, (int, float)):
                busy_seconds += float(ts) - busy_started
            busy_started = None
        elif name == "idle" and isinstance(event.get("duration_seconds"), (int, float)):
            idle_seconds += float(event["duration_seconds"])

    stages: dict[str, dict[str, Any]] = {}
    for event in dispatches:
        stage = str(event.get("stage") or "unknown")
        entry = stages.setdefault(
            stage, {"dispatches": 0, "overlapped_dispatches": 0, "first_ts": None, "last_ts": None}
        )
        entry["dispatches"] += 1
        if event.get("overlapped"):
            entry["overlapped_dispatches"] += 1
        ts = event.get("ts")
        if isinstance(ts, (int, float)):
            if entry["first_ts"] is None or ts < entry["first_ts"]:
                entry["first_ts"] = ts
            if entry["last_ts"] is None or ts > entry["last_ts"]:
                entry["last_ts"] = ts

    wall = (
        float(last_ts) - float(first_ts)
        if isinstance(first_ts, (int, float)) and isinstance(last_ts, (int, float))
        else 0.0
    )
    return {
        "events": len(events),
        "wall_seconds": round(wall, 1),
        "busy_seconds": round(busy_seconds, 1),
        "idle_seconds": round(idle_seconds, 1),
        "utilization": round(busy_seconds / wall, 3) if wall > 0 else None,
        "stages": stages,
    }


__all__ = [
    "TIMING_RELATIVE_PATH",
    "load_events",
    "record_event",
    "summarize",
    "timing_path",
]
