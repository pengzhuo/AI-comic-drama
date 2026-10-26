"""项目级 LLM 请求归因记录（CE）。

CE 的 usage meter 只做 no-op（不记账），因此"哪次模型调用属于哪个项目"从未
落库。这里在项目 data.db 中单开一张 ``llm_request_usage`` 表，按网关
request id 记录每次模型调用，供成本报表与网关 quota 对账使用。

与 ``image/video/audio_request_usage`` 保持同样的库路径解析与迁移约定。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from novelvideo.config import OUTPUT_DIR, STATE_DIR
from novelvideo.sqlite_pragmas import configure_sqlite_connection
from novelvideo.sqlite_schema import ensure_sqlite_schema


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS llm_request_usage (
    request_id TEXT PRIMARY KEY,
    provider_request_id TEXT,
    provider_task_id TEXT,
    provider_response_id TEXT,
    model TEXT,
    resource_kind TEXT,
    episode INTEGER,
    user_id TEXT,
    status TEXT NOT NULL DEFAULT 'accepted',
    input_tokens INTEGER,
    output_tokens INTEGER,
    accepted_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    cost_quota INTEGER,
    quota_checked_at TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_request_usage_model
ON llm_request_usage(model, accepted_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_request_usage_gateway
ON llm_request_usage(provider_request_id);
"""

_SCHEMA_COMPONENT = "llm_request_usage"
# MIGRATION CONTRACT: increment this whenever _SCHEMA_SQL changes.
_SCHEMA_VERSION = 1


def get_llm_request_usage_db_path(project_output_dir: str | Path) -> Path:
    project_output_dir = Path(project_output_dir).resolve()
    output_root = Path(OUTPUT_DIR).resolve()
    state_root = Path(STATE_DIR).resolve()
    try:
        rel = project_output_dir.relative_to(output_root)
    except ValueError:
        return (project_output_dir / "data.db").resolve()
    if len(rel.parts) >= 2:
        from novelvideo.utils.project_paths import ProjectPaths

        user, project = rel.parts[0], rel.parts[1]
        ProjectPaths(user, project).bootstrap_from_legacy_output()
        return (state_root / user / project / "data.db").resolve()
    return (project_output_dir / "data.db").resolve()


@contextmanager
def _connect(project_output_dir: str | Path):
    db_path = get_llm_request_usage_db_path(project_output_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ensure_sqlite_schema(
        db_path,
        component=_SCHEMA_COMPONENT,
        version=_SCHEMA_VERSION,
        initialize=lambda schema_conn: schema_conn.executescript(_SCHEMA_SQL),
    )
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    configure_sqlite_connection(conn, set_journal_mode=False)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_llm_request(
    *,
    project_output_dir: str | Path,
    request_id: str,
    model: str,
    provider_request_id: str = "",
    provider_task_id: str = "",
    provider_response_id: str = "",
    resource_kind: str = "",
    episode: int | None = None,
    user_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    status: str = "completed",
) -> None:
    now = datetime.now().isoformat()
    with _connect(project_output_dir) as conn:
        conn.execute(
            """
            INSERT INTO llm_request_usage (
                request_id, provider_request_id, provider_task_id,
                provider_response_id, model, resource_kind, episode, user_id,
                status, input_tokens, output_tokens, accepted_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(request_id) DO NOTHING
            """,
            (
                request_id,
                provider_request_id or None,
                provider_task_id or None,
                provider_response_id or None,
                model or None,
                resource_kind or None,
                episode,
                user_id,
                status,
                input_tokens,
                output_tokens,
                now,
                now,
            ),
        )


def update_llm_request_cost(
    *,
    project_output_dir: str | Path,
    request_id: str,
    cost_quota: int | None,
    quota_checked_at: str | None = None,
) -> None:
    """Write the gateway quota snapshot back onto a recorded model call."""
    now = datetime.now().isoformat()
    with _connect(project_output_dir) as conn:
        conn.execute(
            """
            UPDATE llm_request_usage
            SET cost_quota = ?, quota_checked_at = ?, updated_at = ?
            WHERE request_id = ?
            """,
            (cost_quota, quota_checked_at or now, now, request_id),
        )


def get_llm_usage_summary(
    *,
    project_output_dir: str | Path,
    episode: int | None = None,
) -> dict:
    today = datetime.now().date().isoformat()
    where = []
    params: list[object] = []
    if episode is not None:
        where.append("episode = ?")
        params.append(episode)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    today_sql = f"{where_sql} {'AND' if where_sql else 'WHERE'} substr(accepted_at, 1, 10) = ?"

    with _connect(project_output_dir) as conn:
        total_row = conn.execute(
            f"SELECT COUNT(*) FROM llm_request_usage {where_sql}",
            tuple(params),
        ).fetchone()
        today_row = conn.execute(
            f"SELECT COUNT(*) FROM llm_request_usage {today_sql}",
            (*params, today),
        ).fetchone()
    return {
        "total_requests": int(total_row[0] or 0) if total_row else 0,
        "today_requests": int(today_row[0] or 0) if today_row else 0,
    }
