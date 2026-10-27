"""项目成本报表：按上游中转站账单口径的消耗明细与制作时长。

数据来源是项目 ``data.db`` 里的四张用量表：

- ``video_request_usage`` / ``image_request_usage`` / ``audio_request_usage``
- ``llm_request_usage``

费用字段 ``cost_quota`` 由 ``novelvideo cost-reconcile`` 回填：默认 ``--source
relay`` 取上游中转站（真正收钱的一方）的真实账单 quota，``--source gateway``
则取本地捏驿网关账单库。USD = ``cost_quota / QUOTA_PER_USD``（捏驿默认
500000）；中转站按 ¥ 展示，故同时给出 ``cny = usd * usd_cny_rate``。报告只读，
不触发任何网关调用；``cost_quota`` 为空的行归入 ``unattributed``。

时长口径：单条视频用应用侧 ``accepted_at``（提交网关、拿到 task id 的时刻）到
``completed_at``（观察到完成的时刻）；每集取该集视频的 ``min(accepted_at)`` 到
``max(completed_at)``。轮询间隔会给完成时刻带来秒级滞后，非端到端精确排队时长。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query

from novelvideo.api.auth import get_api_user
from novelvideo.api.deps import resolve_project_scope
from novelvideo.audio_request_usage import get_audio_request_usage_db_path
from novelvideo.config import QUOTA_PER_USD, REPORT_CURRENCY, USD_CNY_RATE
from novelvideo.image_request_usage import get_image_request_usage_db_path
from novelvideo.llm_request_usage import get_llm_request_usage_db_path
from novelvideo.video_request_usage import get_video_request_usage_db_path

logger = logging.getLogger("novelvideo.api.cost_report")

router = APIRouter()


@dataclass
class UsageRow:
    """One request row normalized across the four usage tables."""

    kind: str
    request_id: str
    model: str
    episode: int | None
    accepted_at: str | None
    completed_at: str | None
    quota: int | None
    status: str


@dataclass
class _Bucket:
    requests: int = 0
    quota: int = 0
    unattributed_requests: int = 0
    duration_seconds: float = 0.0
    started_at: str | None = None
    finished_at: str | None = None


@dataclass
class _Accumulator:
    video: list[UsageRow] = field(default_factory=list)
    image: list[UsageRow] = field(default_factory=list)
    audio: list[UsageRow] = field(default_factory=list)
    llm: list[UsageRow] = field(default_factory=list)


_KIND_LABELS = {
    "video": "视频",
    "image": "图片",
    "audio": "音频",
    "llm": "文本",
}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _read_rows(
    db_path: Path,
    *,
    kind: str,
    table: str,
    model_column: str,
    has_completed_at: bool,
) -> list[UsageRow]:
    if not db_path.is_file():
        return []
    rows: list[UsageRow] = []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, table):
            return []
        columns = _columns(conn, table)
        model_expr = model_column if model_column in columns else "''"
        episode_expr = "episode" if "episode" in columns else "NULL"
        quota_expr = "cost_quota" if "cost_quota" in columns else "NULL"
        completed_expr = "completed_at" if has_completed_at else "NULL"
        status_expr = "status" if "status" in columns else "''"
        query = f"""
            SELECT request_id, {model_expr} AS model_name, {episode_expr} AS episode,
                   accepted_at, {completed_expr} AS completed_at,
                   {quota_expr} AS cost_quota, {status_expr} AS status
            FROM {table}
        """
        for row in conn.execute(query):
            quota_raw = row["cost_quota"]
            rows.append(
                UsageRow(
                    kind=kind,
                    request_id=str(row["request_id"] or ""),
                    model=str(row["model_name"] or ""),
                    episode=row["episode"] if isinstance(row["episode"], int) else None,
                    accepted_at=row["accepted_at"],
                    completed_at=row["completed_at"],
                    quota=int(quota_raw) if quota_raw is not None else None,
                    status=str(row["status"] or ""),
                )
            )
    finally:
        conn.close()
    return rows


def _load_usage(project_output_dir: str | Path) -> _Accumulator:
    acc = _Accumulator()
    acc.video = _read_rows(
        get_video_request_usage_db_path(project_output_dir),
        kind="video",
        table="video_request_usage",
        model_column="model_name",
        has_completed_at=True,
    )
    acc.image = _read_rows(
        get_image_request_usage_db_path(project_output_dir),
        kind="image",
        table="image_request_usage",
        model_column="model_name",
        has_completed_at=False,
    )
    acc.audio = _read_rows(
        get_audio_request_usage_db_path(project_output_dir),
        kind="audio",
        table="audio_request_usage",
        model_column="model_name",
        has_completed_at=False,
    )
    acc.llm = _read_rows(
        get_llm_request_usage_db_path(project_output_dir),
        kind="llm",
        table="llm_request_usage",
        model_column="model",
        has_completed_at=False,
    )
    return acc


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _row_duration_seconds(row: UsageRow) -> float:
    if row.kind != "video":
        return 0.0
    started = _parse_ts(row.accepted_at)
    finished = _parse_ts(row.completed_at)
    if started is None or finished is None:
        return 0.0
    delta = (finished - started).total_seconds()
    return delta if delta > 0 else 0.0


def _merge_span(bucket: _Bucket, row: UsageRow) -> None:
    if row.kind != "video":
        return
    started = _parse_ts(row.accepted_at)
    finished = _parse_ts(row.completed_at)
    if started is not None:
        if bucket.started_at is None or str(started) < bucket.started_at:
            bucket.started_at = str(started)
    if finished is not None:
        if bucket.finished_at is None or str(finished) > bucket.finished_at:
            bucket.finished_at = str(finished)
    bucket.duration_seconds += _row_duration_seconds(row)


def _span_seconds(started_at: str | None, finished_at: str | None) -> float:
    started = _parse_ts(started_at)
    finished = _parse_ts(finished_at)
    if started is None or finished is None:
        return 0.0
    delta = (finished - started).total_seconds()
    return delta if delta > 0 else 0.0


def _as_quota_row(bucket: _Bucket) -> dict[str, Any]:
    span = _span_seconds(bucket.started_at, bucket.finished_at) if bucket.started_at else 0.0
    duration = span or bucket.duration_seconds
    usd = bucket.quota / QUOTA_PER_USD if QUOTA_PER_USD else 0.0
    return {
        "requests": bucket.requests,
        "unattributed_requests": bucket.unattributed_requests,
        "quota": bucket.quota,
        "usd": usd,
        "cny": usd * USD_CNY_RATE,
        "duration_seconds": round(duration, 3),
        "duration_hours": round(duration / 3600, 3) if duration else 0.0,
        "started_at": bucket.started_at,
        "finished_at": bucket.finished_at,
    }


def build_cost_report(
    *,
    project_output_dir: str | Path,
    episode: int | None = None,
) -> dict[str, Any]:
    acc = _load_usage(project_output_dir)
    all_rows: list[UsageRow] = [*acc.video, *acc.image, *acc.audio, *acc.llm]
    if episode is not None:
        all_rows = [row for row in all_rows if row.episode == episode]

    totals = _Bucket()
    by_kind: dict[str, _Bucket] = {}
    by_model: dict[tuple[str, str], _Bucket] = {}
    by_episode: dict[int | None, _Bucket] = {}
    by_day: dict[str, _Bucket] = {}
    unattributed: dict[str, int] = {kind: 0 for kind in _KIND_LABELS}

    for row in all_rows:
        kind_bucket = by_kind.setdefault(row.kind, _Bucket())
        model_bucket = by_model.setdefault((row.kind, row.model or "unknown"), _Bucket())
        episode_bucket = by_episode.setdefault(row.episode, _Bucket())
        day = str(row.accepted_at or "")[:10] or "unknown"
        day_bucket = by_day.setdefault(day, _Bucket())

        attributed = row.quota is not None
        for bucket in (totals, kind_bucket, model_bucket, episode_bucket, day_bucket):
            bucket.requests += 1
            if attributed:
                bucket.quota += int(row.quota or 0)
            else:
                bucket.unattributed_requests += 1
            _merge_span(bucket, row)
        if not attributed:
            unattributed[row.kind] = unattributed.get(row.kind, 0) + 1

    models = [
        {
            "kind": kind,
            "kind_label": _KIND_LABELS.get(kind, kind),
            "model": model,
            **_as_quota_row(bucket),
        }
        for (kind, model), bucket in by_model.items()
    ]
    models.sort(key=lambda item: (item["quota"], item["requests"]), reverse=True)

    episodes = [
        {"episode": episode_num, **_as_quota_row(bucket)}
        for episode_num, bucket in by_episode.items()
    ]
    episodes.sort(key=lambda item: (item["episode"] is None, item["episode"]))

    days = [
        {"date": day, **_as_quota_row(bucket)} for day, bucket in by_day.items()
    ]
    days.sort(key=lambda item: item["date"])

    kinds = [
        {"kind": kind, "kind_label": _KIND_LABELS.get(kind, kind), **_as_quota_row(bucket)}
        for kind, bucket in by_kind.items()
    ]
    kinds.sort(key=lambda item: item["quota"], reverse=True)

    return {
        "generated_at": datetime.now().isoformat(),
        "episode": episode,
        "quota_per_usd": QUOTA_PER_USD,
        "currency": REPORT_CURRENCY,
        "usd_cny_rate": USD_CNY_RATE,
        "totals": _as_quota_row(totals),
        "by_kind": kinds,
        "by_model": models,
        "by_episode": episodes,
        "by_day": days,
        "unattributed": {
            "requests": sum(unattributed.values()),
            "by_kind": unattributed,
        },
    }


@router.get("/projects/{project}/cost-report")
async def get_project_cost_report(
    project: str,
    episode: int | None = Query(None, ge=1, description="只看某一集；缺省为全剧"),
    user: dict = Depends(get_api_user),
):
    """按上游账单口径返回项目消耗明细、每集时长与合计。"""
    resolution = await resolve_project_scope(project, user, required_role="viewer")
    data = build_cost_report(
        project_output_dir=resolution.output_dir,
        episode=episode,
    )
    data["project"] = {
        "id": resolution.ctx.project_id if resolution.ctx else project,
        "name": resolution.project_name,
        "owner": resolution.username,
    }
    return {"ok": True, "data": data}
