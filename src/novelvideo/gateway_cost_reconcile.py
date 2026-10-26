"""网关（捏驿 / NewAPI）账单对账。

把应用侧 usage 表里的请求与捏驿账单库 ``one-api.db`` 的 ``quota`` 对齐，
从而得到"每部剧花在哪个模型上的钱"的精确值：

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

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from novelvideo.config import NEWAPI_BILLING_DB_PATH, QUOTA_PER_USD, STATE_DIR
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
    projects: list[ProjectReport] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

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
) -> ReconcileReport:
    gateway_path = Path(gateway_db or NEWAPI_BILLING_DB_PATH)
    report = ReconcileReport(gateway_db=gateway_path, applied=apply)
    try:
        task_quota, log_quota, _models = load_gateway_index(gateway_path)
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller/CLI
        report.errors.append(f"网关库读取失败: {exc}")
        return report

    if project_db:
        targets = [Path(project_db)]
    else:
        targets = discover_project_dbs(state_dir)

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
