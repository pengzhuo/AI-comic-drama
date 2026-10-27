"""网关（捏驿 / NewAPI）账单对账。

把应用侧 usage 表里的请求与账单 ``quota`` 对齐，从而得到"每部剧花在哪个模型
上的钱"的精确值。支持两种口径（``source``）：

- ``gateway``：本地捏驿账单库 ``one-api.db``。捏驿是我们自己跑的中继，对
  ``DC-*`` / 媒体模型别名没有价格条目，会落到默认倍率，金额明显偏高。
- ``relay``：真正收钱的上游中转站账单。先用捏驿库把本地单号桥接成上游单号
  （``logs.request_id → logs.upstream_request_id``、
  ``tasks.private_data.upstream_task_id``），再向中转站取真实 ``quota``。

匹配关系：

- 视频：``video_request_usage.request_id`` == 捏驿 ``tasks.task_id``（精确）
- 图片 / 音频 / LLM：``*_usage.gateway_request_id`` /
  ``llm_request_usage.provider_request_id`` == 捏驿 ``logs.request_id``

设计约定
--------
- 捏驿库**只读**打开（``mode=ro``），本模块绝不写入网关侧数据。
- 默认只做预演（dry-run），``apply=True`` 才回写 ``cost_quota``。
- 未匹配的请求会汇总输出，用于发现采集缺口，而不是静默丢弃。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from novelvideo.config import NEWAPI_BILLING_DB_PATH, QUOTA_PER_USD, STATE_DIR
from novelvideo.relay_billing import (
    RelayBillingCache,
    RelayEndpoint,
    RelayStatus,
    collect_quotas,
)
from novelvideo.sqlite_schema import add_column_if_missing

logger = logging.getLogger("novelvideo.gateway_cost_reconcile")


@dataclass
class TableReport:
    table: str
    rows: int = 0
    matched: int = 0
    quota: int = 0
    unmatched: list[str] = field(default_factory=list)

    @property
    def usd(self) -> float:
        return self.quota / QUOTA_PER_USD if QUOTA_PER_USD else 0.0


@dataclass
class ProjectReport:
    label: str
    db_path: Path
    tables: list[TableReport] = field(default_factory=list)

    @property
    def quota(self) -> int:
        return sum(table.quota for table in self.tables)

    @property
    def usd(self) -> float:
        return sum(table.usd for table in self.tables)


@dataclass
class ReconcileReport:
    gateway_db: Path
    applied: bool
    #: ``gateway`` = 本地捏驿账单库；``relay`` = 上游中转站真实账单。
    source: str = "gateway"
    projects: list[ProjectReport] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: 中转站 ``/api/status`` 口径（source=relay 时有值）。
    relay_status: list[RelayStatus] = field(default_factory=list)

    @property
    def quota(self) -> int:
        return sum(project.quota for project in self.projects)

    @property
    def usd(self) -> float:
        return sum(project.usd for project in self.projects)


# Each entry: (table, key column used to match the gateway, gateway id space)
_USAGE_TABLES = (
    ("video_request_usage", None, "task"),
    ("image_request_usage", "gateway_request_id", "log"),
    ("audio_request_usage", "gateway_request_id", "log"),
    ("llm_request_usage", "provider_request_id", "log"),
)


def _open_gateway_ro(path: Path) -> sqlite3.Connection:
    """Open the gateway billing DB read-only; never creates or writes it."""
    if not path.is_file():
        raise FileNotFoundError(f"gateway billing db not found: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def load_gateway_index(
    gateway_db: str | Path,
) -> tuple[dict[str, int], dict[str, int], dict[str, str]]:
    """Return ``(task_quota, log_quota, model_by_id)`` from the gateway DB."""
    conn = _open_gateway_ro(Path(gateway_db))
    task_quota: dict[str, int] = {}
    log_quota: dict[str, int] = {}
    model_by_id: dict[str, str] = {}
    try:
        for row in conn.execute(
            "SELECT task_id, quota FROM tasks WHERE task_id IS NOT NULL"
        ):
            task_id = str(row["task_id"] or "").strip()
            if task_id:
                task_quota[task_id] = int(row["quota"] or 0)
        for row in conn.execute(
            "SELECT request_id, upstream_request_id, quota, model_name FROM logs"
        ):
            quota = int(row["quota"] or 0)
            model = str(row["model_name"] or "").strip()
            for key in (row["request_id"], row["upstream_request_id"]):
                clean = str(key or "").strip()
                if not clean:
                    continue
                # Keep the largest quota seen for a given id (retries log twice).
                log_quota[clean] = max(log_quota.get(clean, 0), quota)
                if model and clean not in model_by_id:
                    model_by_id[clean] = model
    finally:
        conn.close()
    return task_quota, log_quota, model_by_id


def _upstream_task_id(raw: Any) -> str:
    """Decode ``tasks.private_data`` (BLOB/JSON text) → ``upstream_task_id``."""
    if raw is None:
        return ""
    if isinstance(raw, (bytes, bytearray, memoryview)):
        try:
            raw = bytes(raw).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001 - 脏数据不该中断对账
            return ""
    text = str(raw).strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("upstream_task_id") or "").strip()


def load_upstream_id_maps(
    gateway_db: str | Path,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(task_id → upstream_task_id, request_id → upstream_request_id)``.

    中转站只认它自己发出的单号，所以要用捏驿库里记录的上游单号把本地单号
    桥接过去。
    """
    conn = _open_gateway_ro(Path(gateway_db))
    task_map: dict[str, str] = {}
    log_map: dict[str, str] = {}
    try:
        if _table_exists(conn, "tasks") and "private_data" in _columns(conn, "tasks"):
            for row in conn.execute(
                "SELECT task_id, private_data FROM tasks WHERE task_id IS NOT NULL"
            ):
                local_id = str(row["task_id"] or "").strip()
                upstream_id = _upstream_task_id(row["private_data"])
                if local_id and upstream_id:
                    task_map[local_id] = upstream_id
        if _table_exists(conn, "logs"):
            columns = _columns(conn, "logs")
            if "upstream_request_id" in columns:
                for row in conn.execute(
                    "SELECT request_id, upstream_request_id FROM logs"
                ):
                    local_id = str(row["request_id"] or "").strip()
                    upstream_id = str(row["upstream_request_id"] or "").strip()
                    if local_id and upstream_id:
                        log_map[local_id] = upstream_id
    finally:
        conn.close()
    return task_map, log_map


def collect_usage_gateway_ids(db_path: str | Path) -> set[str]:
    """Every gateway id referenced by a project's usage tables.

    Relay lookups are one HTTP request per id, so we must narrow them to the
    ids our own usage rows reference instead of everything the gateway logged.
    """
    ids: set[str] = set()
    path = Path(db_path)
    if not path.is_file():
        return ids
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        for table, key_column, _space in _USAGE_TABLES:
            if not _table_exists(conn, table):
                continue
            columns = _columns(conn, table)
            if key_column is None:
                key_expr = (
                    "COALESCE(NULLIF(gateway_request_id, ''), request_id)"
                    if "gateway_request_id" in columns
                    else "request_id"
                )
            else:
                if key_column not in columns:
                    continue
                key_expr = f"NULLIF({key_column}, '')"
            for row in conn.execute(
                f"SELECT DISTINCT {key_expr} AS gid FROM {table}"
            ):
                gid = str(row["gid"] or "").strip()
                if gid:
                    ids.add(gid)
    finally:
        conn.close()
    return ids


def build_relay_index(
    gateway_db: str | Path,
    *,
    endpoints: Sequence[RelayEndpoint],
    only_ids: set[str] | None = None,
    cache_path: str | Path | None = None,
    transport: Any | None = None,
    workers: int = 4,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[dict[str, int], dict[str, int], list[str], list[RelayStatus]]:
    """Query the relay and return quota maps keyed by **local** gateway ids.

    ``only_ids`` (local ids referenced by the usage rows) keeps the per-id
    lookups bounded; ``None`` means "every id the gateway logged".

    Returns ``(task_quota, log_quota, errors, statuses)``.
    """
    task_map, log_map = load_upstream_id_maps(gateway_db)
    if only_ids is not None:
        task_map = {k: v for k, v in task_map.items() if k in only_ids}
        log_map = {k: v for k, v in log_map.items() if k in only_ids}
    cache = RelayBillingCache.load(cache_path)
    snapshot = collect_quotas(
        endpoints,
        upstream_task_ids=set(task_map.values()),
        upstream_log_ids=set(log_map.values()),
        cache=cache,
        transport=transport,
        workers=workers,
        progress=progress,
    )

    task_quota: dict[str, int] = {}
    for local_id, upstream_id in task_map.items():
        if upstream_id in snapshot.tasks:
            task_quota[local_id] = snapshot.tasks[upstream_id]

    log_quota: dict[str, int] = {}
    for local_id, upstream_id in log_map.items():
        if upstream_id not in snapshot.logs:
            continue
        quota = snapshot.logs[upstream_id]
        log_quota[local_id] = quota
        # Also keep the upstream key: some rows record the upstream id directly.
        log_quota.setdefault(upstream_id, quota)

    return task_quota, log_quota, snapshot.errors, snapshot.statuses


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _reconcile_table(
    conn: sqlite3.Connection,
    *,
    table: str,
    key_column: str | None,
    task_quota: dict[str, int],
    log_quota: dict[str, int],
    apply: bool,
    now: str,
) -> TableReport:
    report = TableReport(table=table)
    if not _table_exists(conn, table):
        return report
    columns = _columns(conn, table)
    if key_column is None:
        # video: the recorded request id *is* the gateway task id; prefer the
        # explicit gateway id when present.
        if "gateway_request_id" in columns:
            key_expr = "COALESCE(NULLIF(gateway_request_id, ''), request_id)"
        else:
            key_expr = "request_id"
    else:
        if key_column not in columns:
            return report
        key_expr = f"NULLIF({key_column}, '')"

    rows = list(conn.execute(f"SELECT rowid AS rid, {key_expr} AS gid FROM {table}"))
    matched_keys: set[str] = set()
    updates: list[tuple[int, str, int]] = []
    for row in rows:
        report.rows += 1
        gid = str(row["gid"] or "").strip()
        if not gid:
            continue
        quota = task_quota.get(gid)
        if quota is None:
            quota = log_quota.get(gid)
        if quota is None:
            if len(report.unmatched) < 20:
                report.unmatched.append(gid)
            continue
        report.matched += 1
        report.quota += quota
        matched_keys.add(gid)
        updates.append((quota, now, int(row["rid"])))

    if apply and updates:
        # ``cost_quota``/``quota_checked_at`` only exist once the owning usage
        # module has migrated the table (i.e. after the first write since
        # deploy). A project that has been idle since the upgrade still has the
        # legacy columns, so migrate here before the UPDATE instead of failing.
        add_column_if_missing(conn, table, "cost_quota", "INTEGER")
        add_column_if_missing(conn, table, "quota_checked_at", "TEXT")
        conn.executemany(
            f"UPDATE {table} SET cost_quota = ?, quota_checked_at = ? WHERE rowid = ?",
            updates,
        )
        conn.commit()
    return report


def reconcile_project_db(
    db_path: str | Path,
    *,
    task_quota: dict[str, int],
    log_quota: dict[str, int],
    apply: bool = False,
) -> ProjectReport:
    db_path = Path(db_path)
    report = ProjectReport(label=db_path.parent.name, db_path=db_path)
    if not db_path.is_file():
        return report
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        for table, key_column, _space in _USAGE_TABLES:
            report.tables.append(
                _reconcile_table(
                    conn,
                    table=table,
                    key_column=key_column,
                    task_quota=task_quota,
                    log_quota=log_quota,
                    apply=apply,
                    now=now,
                )
            )
    finally:
        conn.close()
    return report


def discover_project_dbs(state_dir: str | Path | None = None) -> list[Path]:
    root = Path(state_dir or STATE_DIR)
    if not root.is_dir():
        return []
    return sorted(root.glob("*/*/data.db"))


def reconcile(
    *,
    gateway_db: str | Path | None = None,
    state_dir: str | Path | None = None,
    project_db: str | Path | None = None,
    apply: bool = False,
    source: str = "gateway",
    relay_endpoints: Sequence[RelayEndpoint] | None = None,
    relay_cache: str | Path | None = None,
    relay_transport: Any | None = None,
    relay_workers: int = 4,
    progress: Callable[[int, int], None] | None = None,
) -> ReconcileReport:
    if source not in {"gateway", "relay"}:
        raise ValueError(f"unknown source: {source!r} (expected gateway|relay)")

    gateway_path = Path(gateway_db or NEWAPI_BILLING_DB_PATH)
    report = ReconcileReport(gateway_db=gateway_path, applied=apply, source=source)

    if project_db:
        targets = [Path(project_db)]
    else:
        targets = discover_project_dbs(state_dir)

    try:
        if source == "relay":
            needed: set[str] = set()
            for target in targets:
                try:
                    needed |= collect_usage_gateway_ids(target)
                except Exception as exc:  # noqa: BLE001
                    report.errors.append(f"{target}: {exc}")
            task_quota, log_quota, relay_errors, statuses = build_relay_index(
                gateway_path,
                endpoints=list(relay_endpoints or []),
                only_ids=needed,
                cache_path=relay_cache,
                transport=relay_transport,
                workers=relay_workers,
                progress=progress,
            )
            report.errors.extend(relay_errors)
            report.relay_status = statuses
        else:
            task_quota, log_quota, _models = load_gateway_index(gateway_path)
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller/CLI
        report.errors.append(f"账单库读取失败: {exc}")
        return report

    for target in targets:
        try:
            report.projects.append(
                reconcile_project_db(
                    target,
                    task_quota=task_quota,
                    log_quota=log_quota,
                    apply=apply,
                )
            )
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"{target}: {exc}")
    return report
