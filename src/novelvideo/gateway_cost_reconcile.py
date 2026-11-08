"""网关（捏驿 / NewAPI）账单对账。

把应用侧 usage 表里的请求与账单 ``quota`` 对齐，从而得到"每部剧花在哪个模型
上的钱"的精确值。支持三种口径（``source``）：

- ``local``：**默认日常口径**。按「用量 × 本地单价表」计价，单价取自
  ``production-prices.json``（``actual_rules`` 由真账反推，实测优先），**不触网**、
  不受上游 429/409 影响；合计与上游真账一致。
- ``gateway``：本地捏驿账单库 ``one-api.db``。捏驿是我们自己跑的中继，对
  ``DC-*`` / 媒体模型别名没有价格条目，会落到默认倍率，金额明显偏高。
- ``relay``：真正收钱的上游中转站账单。先用捏驿库把本地单号桥接成上游单号
  （``logs.request_id → logs.upstream_request_id``、
  ``tasks.private_data.upstream_task_id``），再向中转站取真实 ``quota``。登录后
  **必须在 ``finally`` 里退出会话**（见 ``relay_billing.collect_quotas``），否则
  占满上游并发会话额度会收到 409 ``AUTH_SESSION_LIMIT``。

匹配关系：

- 视频：``video_request_usage.request_id`` == 捏驿 ``tasks.task_id``（精确）
- 图片 / 音频 / LLM：``*_usage.gateway_request_id`` /
  ``llm_request_usage.provider_request_id`` == 捏驿 ``logs.request_id``

修改日志
--------
- 未命中的行标 ``cost_source='unmatched'`` 并清空 ``cost_quota``，避免网关默认倍率
  的虚高值冒充真账；命中行的 ``cost_source`` 记录来源（``relay`` / ``gateway``）。

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
from typing import Any, Callable, Mapping, Sequence

from novelvideo.config import (
    NEWAPI_BILLING_DB_PATH,
    QUOTA_PER_USD,
    STATE_DIR,
    USD_CNY_RATE,
)
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

#: 本地计价（``source=local``）口径：表 -> (kind, 模型列)。
_LOCAL_KINDS: tuple[tuple[str, str, str], ...] = (
    ("video_request_usage", "video", "model_name"),
    ("image_request_usage", "image", "model_name"),
    ("audio_request_usage", "audio", "model_name"),
    ("llm_request_usage", "text", "model"),
)

#: 这些状态的行不计费（生成失败不该算钱）；其余按用量计价。
_NON_BILLABLE_STATUSES = {"failed", "error", "cancelled", "canceled", "timeout"}


def _as_number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


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
    source: str = "gateway",
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
    updates: list[tuple[int, str, str, int]] = []
    cleared: list[tuple[str, int]] = []
    for row in rows:
        report.rows += 1
        gid = str(row["gid"] or "").strip()
        quota = None
        if gid:
            quota = task_quota.get(gid)
            if quota is None:
                quota = log_quota.get(gid)
        if quota is None:
            if gid and len(report.unmatched) < 20:
                report.unmatched.append(gid)
            # 对不上账单的行不能留着旧口径（可能是网关默认倍率的虚高值）。
            # 标成 ``unmatched`` 并清空 quota，报表按未归因处理。
            cleared.append((now, int(row["rid"])))
            continue
        report.matched += 1
        report.quota += quota
        updates.append((quota, now, source, int(row["rid"])))

    if apply and (updates or cleared):
        # ``cost_quota``/``quota_checked_at``/``cost_source`` only exist once the
        # owning usage module has migrated the table (i.e. after the first write
        # since deploy). A project that has been idle since the upgrade still has
        # the legacy columns, so migrate here before the UPDATE instead of failing.
        add_column_if_missing(conn, table, "cost_quota", "INTEGER")
        add_column_if_missing(conn, table, "quota_checked_at", "TEXT")
        add_column_if_missing(conn, table, "cost_source", "TEXT")
        if updates:
            conn.executemany(
                f"UPDATE {table} SET cost_quota = ?, quota_checked_at = ?,"
                " cost_source = ? WHERE rowid = ?",
                updates,
            )
        if cleared:
            conn.executemany(
                f"UPDATE {table} SET cost_quota = NULL, quota_checked_at = ?,"
                " cost_source = 'unmatched' WHERE rowid = ?",
                cleared,
            )
        conn.commit()
    return report


def _local_action(kind: str, model: str, row: sqlite3.Row) -> Any:
    """把一行用量构造成计费动作（只用库里真实记下的量）。"""

    from novelvideo.production_prices import ActionRequest

    if kind == "video":
        metrics = {
            "duration_seconds": _as_number(row["duration_seconds"]),
            "call_count": 1,
        }
    elif kind == "text":
        prompt = _as_number(row["input_tokens"])
        completion = _as_number(row["output_tokens"])
        metrics = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "tokens": prompt + completion,
        }
    else:  # image / audio：每行一次调用
        metrics = {"call_count": 1}
    return ActionRequest(kind=kind, model=model, quantity=1, metrics=metrics)


def _reconcile_table_local(
    conn: sqlite3.Connection,
    *,
    table: str,
    kind: str,
    model_column: str,
    book: Any,
    apply: bool,
    now: str,
) -> TableReport:
    """按本地单价表逐行计价，回写 ``cost_quota``（不触网）。"""

    report = TableReport(table=table)
    if not _table_exists(conn, table):
        return report
    columns = _columns(conn, table)
    if model_column not in columns:
        return report
    status_expr = "status" if "status" in columns else "''"
    duration_expr = "duration_seconds" if "duration_seconds" in columns else "0"
    input_expr = "input_tokens" if "input_tokens" in columns else "0"
    output_expr = "output_tokens" if "output_tokens" in columns else "0"
    rows = list(
        conn.execute(
            f"SELECT rowid AS rid, {model_column} AS model, {status_expr} AS status, "
            f"{duration_expr} AS duration_seconds, {input_expr} AS input_tokens, "
            f"{output_expr} AS output_tokens FROM {table}"
        )
    )
    updates: list[tuple[int, str, str, int]] = []
    cleared: list[tuple[str, int]] = []
    for row in rows:
        report.rows += 1
        status = str(row["status"] or "").strip().lower()
        model = str(row["model"] or "").strip()
        if status in _NON_BILLABLE_STATUSES:
            cleared.append((now, int(row["rid"])))
            continue
        # 实测反推价 > 手工规则 > 同步价目；未定价则落 unmatched，绝不按 0 计。
        priced = book.price(_local_action(kind, model, row), prefer_measured=True)
        if priced.cny is None or priced.cny <= 0:
            if model and len(report.unmatched) < 20:
                report.unmatched.append(model)
            cleared.append((now, int(row["rid"])))
            continue
        quota = (
            int(round(priced.cny * QUOTA_PER_USD / USD_CNY_RATE))
            if USD_CNY_RATE
            else 0
        )
        report.matched += 1
        report.quota += quota
        updates.append((quota, now, "local", int(row["rid"])))

    if apply and (updates or cleared):
        add_column_if_missing(conn, table, "cost_quota", "INTEGER")
        add_column_if_missing(conn, table, "quota_checked_at", "TEXT")
        add_column_if_missing(conn, table, "cost_source", "TEXT")
        if updates:
            conn.executemany(
                f"UPDATE {table} SET cost_quota = ?, quota_checked_at = ?,"
                " cost_source = ? WHERE rowid = ?",
                updates,
            )
        if cleared:
            conn.executemany(
                f"UPDATE {table} SET cost_quota = NULL, quota_checked_at = ?,"
                " cost_source = 'unmatched' WHERE rowid = ?",
                cleared,
            )
        conn.commit()
    return report


def reconcile_local_project_db(
    db_path: str | Path,
    *,
    apply: bool = False,
    book: Any | None = None,
) -> ProjectReport:
    """按本地单价表（实测价优先）回填一个项目的 ``cost_quota``，全程不触网。"""

    from novelvideo.production_prices import load as load_prices

    db_path = Path(db_path)
    report = ProjectReport(label=db_path.parent.name, db_path=db_path)
    if not db_path.is_file():
        return report
    price_book = book or load_prices()
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        for table, kind, model_column in _LOCAL_KINDS:
            report.tables.append(
                _reconcile_table_local(
                    conn,
                    table=table,
                    kind=kind,
                    model_column=model_column,
                    book=price_book,
                    apply=apply,
                    now=now,
                )
            )
    finally:
        conn.close()
    return report


def reconcile_project_db(
    db_path: str | Path,
    *,
    task_quota: dict[str, int],
    log_quota: dict[str, int],
    apply: bool = False,
    source: str = "gateway",
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
                    source=source,
                )
            )
    finally:
        conn.close()
    return report


def episode_quota(
    db_path: str | Path,
    episode: int,
    *,
    task_quota: Mapping[str, int] | None = None,
    log_quota: Mapping[str, int] | None = None,
) -> dict[str, int]:
    """单集 ``quota`` 汇总（只读，不写库）。

    - 不传 ``task_quota`` / ``log_quota``：直接汇总库里已写入的 ``cost_quota``。
    - 传了 maps：**只统计能在中转站账单里对上的行**（``quota``/``matched``），
      ``rows`` 是这一集在四张表里的总行数——调用方据此判断口径是否完整
      （``matched < rows`` 说明还有行对不上，按集结算应当只按已知部分算，宁可
      少收也不能把网关侧虚高的默认倍率算给客户）。

    返回 ``{"quota", "rows", "matched", "priced"}``；``quota`` 是 quota 之和，
    换算成 ¥ 用 ``quota / QUOTA_PER_USD * USD_CNY_RATE``（线性，与
    :func:`novelvideo.production_prices.calibrate_from_project` 同口径）。
    """

    result = {"quota": 0, "rows": 0, "matched": 0, "priced": 0}
    path = Path(db_path)
    if not path.is_file():
        return result
    try:
        ep = int(episode)
    except (TypeError, ValueError):
        return result

    use_maps = task_quota is not None or log_quota is not None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return result
    conn.row_factory = sqlite3.Row
    try:
        for table, key_column, _space in _USAGE_TABLES:
            if not _table_exists(conn, table):
                continue
            columns = _columns(conn, table)
            if "episode" not in columns:
                continue
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
            quota_expr = "cost_quota" if "cost_quota" in columns else "NULL"
            query = (
                f"SELECT {key_expr} AS gid, {quota_expr} AS cost_quota "
                f"FROM {table} WHERE episode = ?"
            )
            for row in conn.execute(query, (ep,)):
                result["rows"] += 1
                if not use_maps:
                    quota = row["cost_quota"]
                    if quota is None:
                        continue
                    result["priced"] += 1
                    result["matched"] += 1
                    result["quota"] += int(quota)
                    continue
                gid = str(row["gid"] or "").strip()
                quota = None
                if gid:
                    if task_quota:
                        quota = task_quota.get(gid)
                    if quota is None and log_quota:
                        quota = log_quota.get(gid)
                if quota is None:
                    continue
                result["matched"] += 1
                result["quota"] += int(quota)
    except sqlite3.Error as exc:  # noqa: BLE001 - 汇总失败按未知处理
        logger.warning("单集 quota 汇总失败 %s: %s", path, exc)
    finally:
        conn.close()
    return result


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
    if source not in {"gateway", "relay", "local"}:
        raise ValueError(
            f"unknown source: {source!r} (expected gateway|relay|local)"
        )

    gateway_path = Path(gateway_db or NEWAPI_BILLING_DB_PATH)
    report = ReconcileReport(gateway_db=gateway_path, applied=apply, source=source)

    if project_db:
        targets = [Path(project_db)]
    else:
        targets = discover_project_dbs(state_dir)

    if source == "local":
        # 本地计价：只读项目用量 + 本地单价表，完全不触网（不登录任何上游）。
        from novelvideo.production_prices import load as load_prices

        book = load_prices()
        for target in targets:
            try:
                report.projects.append(
                    reconcile_local_project_db(target, apply=apply, book=book)
                )
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"{target}: {exc}")
        return report

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
                    source=source,
                )
            )
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"{target}: {exc}")
    return report


def reconcile_relay_first(
    *,
    gateway_db: str | Path | None = None,
    state_dir: str | Path | None = None,
    project_db: str | Path | None = None,
    apply: bool = False,
    relay_transport: Any | None = None,
    relay_workers: int = 4,
) -> ReconcileReport:
    """``relay`` 优先、``gateway`` 兜底的对账（自动回填口径走这里）。

    自动回填（费用报表按钮 / 成片后结算钩子）应当用**真正收钱的一方**口径：

    - 中转站账号已配置且能对上至少一条单据 → 用 relay 真实账单；
    - 账号缺失 / 一条都对不上 / 拉取全失败 → 回退本地捏驿账单库，并把原因
      记进 ``report.errors``；``report.source`` 如实反映实际用的口径，前端据此
      标注「结算口径」还是「估值口径」。
    """

    from novelvideo import relay_billing

    settings = relay_billing.load_settings()
    endpoints = relay_billing.configured_endpoints(settings=settings)
    if not endpoints:
        return reconcile(
            gateway_db=gateway_db,
            state_dir=state_dir,
            project_db=project_db,
            apply=apply,
            source="gateway",
        )

    relay_report = reconcile(
        gateway_db=gateway_db,
        state_dir=state_dir,
        project_db=project_db,
        apply=apply,
        source="relay",
        relay_endpoints=endpoints,
        relay_cache=relay_billing.billing_cache_path(settings),
        relay_transport=relay_transport,
        relay_workers=relay_workers,
    )
    matched = sum(table.matched for item in relay_report.projects for table in item.tables)
    if matched > 0:
        return relay_report

    note = "relay 未命中任何单据，回退 gateway 口径（金额偏高，仅供参考）"
    gateway_report = reconcile(
        gateway_db=gateway_db,
        state_dir=state_dir,
        project_db=project_db,
        apply=apply,
        source="gateway",
    )
    gateway_report.errors = [note, *relay_report.errors, *gateway_report.errors]
    return gateway_report


def reconcile_local(
    *,
    state_dir: str | Path | None = None,
    project_db: str | Path | None = None,
    apply: bool = False,
) -> ReconcileReport:
    """本地按「用量 × 单价（实测优先）」计价，**不登录任何上游**。

    日常口径（费用报表「重新结算」按钮 / 成片后结算钩子 / 伴点结算）都走这里：
    不触网、不受上游 429/409 影响。单价来自本地 ``production-prices.json``，其中
    ``actual_rules`` 由真账反推，因此合计与上游真账一致；未定价的行落 ``unmatched``。
    """

    return reconcile(
        state_dir=state_dir,
        project_db=project_db,
        apply=apply,
        source="local",
    )
