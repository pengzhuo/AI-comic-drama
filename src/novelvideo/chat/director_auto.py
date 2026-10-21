"""Durable server-side coordinator for outer 捏导 episode auto mode."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from novelvideo.chat import service as chat_service
from novelvideo.chat.live_events import broadcast_project_chat_event
from novelvideo.pipeline_timing import record_event
from novelvideo.project_context import ProjectContext, resolve_project_context
from novelvideo.shared.billing_errors import (
    BILLING_RULE_NOT_CONFIGURED_CODE,
    BILLING_RULE_NOT_CONFIGURED_MESSAGE,
    FEATURE_BILLING_IDENTITY_INVALID_CODE,
    FEATURE_BILLING_IDENTITY_INVALID_MESSAGE,
    INSUFFICIENT_CREDITS_CODE,
    INSUFFICIENT_CREDITS_MESSAGE,
)
from novelvideo.sqlite_pragmas import configure_sqlite_connection
from novelvideo.task_diagnostics import summarize_failure
from novelvideo.task_state import TaskState, get_task_manager


logger = logging.getLogger(__name__)

AUTO_TASK_TYPES = frozenset({
    "ingest_fast",
    "screenplay_preprocess",
    "script_process",
    "build_characters",
    "build_scenes",
    "build_props",
    "build_episodes",
    "identity_planner",
    "character_portrait",
    "identity_image",
    "content_rewriter",
    "script_writer",
    "literal_script_writer",
    "director_notes",
    "episode_scene_planner",
    "episode_prop_planner",
    "scene_reference_asset",
    "scene_pano_generation",
    "prop_reference_asset",
    "beat_video_prompt",
    "seedance2_prompt",
    "sketch_grid_generation",
    "sketch_generation",
    "sketch_regen",
    "grid_regenerate",
    "mainline_sketch_from_context",
    "mainline_frame_from_context",
    "ai_identity_detection",
    "global_optimize_video",
    "selected_regen",
    "render_plan",
    "system_voice_setup",
    "audio_generation",
    "indextts2_audio_generation",
    "audio_generation_indextts2",
    "single_video",
    "compose_episode",
})
ACTIVE_STATUSES = frozenset({"submitting", "queued", "pending", "running"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
POLL_SECONDS = 1.5
#: 重叠派发的节流：在已有任务在飞时，最多每这么久试一次「下一批就绪阶段」。
#: 太密只是白白重算 pipeline status，太疏又会留下成片的空转窗口。
OVERLAP_DISPATCH_SECONDS = 5.0
INITIAL_GRACE_SECONDS = 5.0
LEASE_SECONDS = 45.0
HEARTBEAT_SECONDS = 10.0
_MISSING_CHARACTER_PORTRAIT_RE = re.compile(
    r"请先为角色[「“\"](?P<character>.+?)[」”\"]生成\s*Portrait（面部特写）"
)
#: Task error codes that mean "the task went silent" rather than "the task hit a
#: real error".  A stalled task is paused and offered to the user as a decision
#: instead of being treated as an unrecoverable failure.
STALL_ERROR_CODES = frozenset({"TASK_TIMEOUT", "TASK_STALLED"})
#: Stable marker prepended to a stall pause reason so the pending decision
#: survives a page reload (the reason is persisted on the durable run).
STALL_REASON_TAG = "[stall]"
#: Marker for the failure a granted retry is chasing. It keeps ``last_error``
#: pointing at the *cause* while the run is retrying, so a later pause on a
#: different path (e.g. no task in several turns) can still report it.
RETRY_REASON_PREFIX = "[retry "
#: Meta/bridge tools the agent uses to inspect its tool surface instead of
#: operating on the project. Introspection must never count as a write: the
#: bridge echoes the *described* tool name, so a turn that only described a
#: generator used to look like it had already started the task.
_META_TOOL_NAMES = frozenset(
    {
        "tool_call",
        "tool_describe",
        "tool_search",
        "skills_list",
        "skill_view",
        "dramaclaw_tool_call",
        "dramaclaw_tool_describe",
        "dramaclaw_tool_search",
    }
)
#: DramaClaw tools that only read state; they never enqueue a generation task.
_READ_TOOL_PREFIXES = (
    "dramaclaw_get",
    "dramaclaw_list",
    "dramaclaw_pipeline_status",
)
#: Consecutive continuations without a new task that are tolerated before the
#: durable run pauses and hands the decision back to the user.
MAX_TASKLESS_CONTINUATIONS = 3
#: Bounded auto-retry budget for one mainline stage. A transient task failure no
#: longer pauses the whole run: the coordinator re-drives the same stage up to
#: this many times and only then hands the decision back to the user. The budget
#: is per stage (keyed on the task type the run is driving) and resets as soon as
#: that stage makes progress, so unrelated later stages keep their own budget.
MAX_STAGE_ATTEMPTS = 3
#: Backoff before retry N (1-based). Indexing beyond the tuple repeats the last
#: value, so a smaller budget than the tuple length stays cheap to reason about.
STAGE_RETRY_BACKOFF_SECONDS = (10.0, 30.0, 60.0)
#: ``DIRECTOR_AUTO_STAGE_RETRY=0`` restores the previous pause-on-first-failure
#: behaviour for an operator without a code change.
STAGE_RETRY_ENV = "DIRECTOR_AUTO_STAGE_RETRY"
#: Error codes where an automatic retry is pointless or unsafe: retrying cannot
#: create credits, grant a missing permission, or cross a denied egress.
_FATAL_ERROR_CODES = frozenset(
    {
        INSUFFICIENT_CREDITS_CODE,
        BILLING_RULE_NOT_CONFIGURED_CODE,
        FEATURE_BILLING_IDENTITY_INVALID_CODE,
        "FEATURE_SETTLEMENT_RESOLUTION_FAILED",
        "ORG_EGRESS_DENIED",
        "ORG_SERVICE_EGRESS_DENIED",
        "EGRESS_OPERATION_REPLAYED",
        # The request is rebuilt deterministically on every attempt, so a
        # configured-window overflow cannot resolve itself by retrying; the
        # per-stage workflow already spent its own split/retry budget first.
        "MODEL_CONTEXT_BUDGET_EXCEEDED",
    }
)
_FATAL_ERROR_LABELS = {
    INSUFFICIENT_CREDITS_CODE: INSUFFICIENT_CREDITS_MESSAGE,
    BILLING_RULE_NOT_CONFIGURED_CODE: BILLING_RULE_NOT_CONFIGURED_MESSAGE,
    FEATURE_BILLING_IDENTITY_INVALID_CODE: FEATURE_BILLING_IDENTITY_INVALID_MESSAGE,
    "MODEL_CONTEXT_BUDGET_EXCEEDED": "模型上下文预算不足（自动拆分与重试已用尽）",
}
#: Free-text fallback for failures that carry no structured code. Kept narrow on
#: purpose: a false positive only costs the retry we would not have spent, but a
#: false negative retries a doomed request three times.
_FATAL_ERROR_TEXT_RE = re.compile(
    r"insufficient credits|unauthorized|forbidden|积分不足|计费规则未配置|"
    r"无权限|未授权|egress is denied|EGRESS_DENIED",
    re.IGNORECASE,
)


def _stage_retry_enabled() -> bool:
    """Whether the bounded auto-retry is active (operator kill switch)."""

    raw = os.environ.get(STAGE_RETRY_ENV, "1").strip().lower()
    return raw not in {"0", "off", "false", "no"}


def _stage_retry_delay(attempt: int) -> float:
    """Seconds to wait before retry ``attempt`` (1-based)."""

    if not STAGE_RETRY_BACKOFF_SECONDS:
        return 0.0
    index = min(max(int(attempt), 1), len(STAGE_RETRY_BACKOFF_SECONDS)) - 1
    return float(STAGE_RETRY_BACKOFF_SECONDS[index])


def _is_fatal_failure(task: TaskState) -> str | None:
    """Return a human-facing reason when retrying this failure cannot help.

    Billing, egress and user cancellation are terminal by construction: the same
    request will fail again, so spending the retry budget only delays the
    decision the user has to make.
    """

    code = _task_error_code(task)
    if code in _FATAL_ERROR_CODES:
        label = _FATAL_ERROR_LABELS.get(code, code)
        detail = str(task.error or "").strip()
        if detail and label not in detail:
            return f"{label}（{detail[:160]}）"
        return label
    if task.status == "cancelled":
        return "任务已取消"
    text = f"{task.error or ''} {task.current_task or ''}"[:600]
    if _FATAL_ERROR_TEXT_RE.search(text):
        return "上游拒绝且重试无法解决，请检查账号额度或权限"
    return None


def _stage_key_for(task: TaskState | None, run: DirectorAutoRun) -> str:
    """Budget key of the stage the run is currently driving."""

    task_type = str(getattr(task, "task_type", "") or "")
    if task_type:
        return task_type
    return run.stage_key or "unknown"


def _film_pack_delivery_block() -> str:
    """成片合成/交付指令附带的流程包段落（第 06 份：衔接声音与成片验收）。

    主线与画布的合成本身是纯音视频编排，没有发往模型的提示词，所以第 06 份
    只能沿合成周边的 agent 指令送达：这里接管「成片已完成，去展示并收口」这一步。
    任何异常都退回空串，注入只是增强，绝不打断自动成片。
    """

    try:
        from novelvideo.skills.ai_film_process import pack_block

        return pack_block("compose_delivery")
    except Exception:  # noqa: BLE001 - 注入只是增强，不能影响自动成片
        return ""


def _retry_pause_detail(run: DirectorAutoRun) -> str:
    """The failure that armed the retry, for a pause reached on another path."""

    if not run.last_error.startswith(RETRY_REASON_PREFIX):
        return ""
    return run.last_error.split("] ", 1)[-1].strip()


def _event_tool_name(event: dict[str, Any]) -> str:
    """Effective tool name of a lifecycle event (``server.tool`` collapsed)."""

    return str(event.get("name") or "").rsplit(".", 1)[-1].strip()


def _event_inner_tool_name(event: dict[str, Any]) -> str:
    """Inner tool name carried by a ``tool_call`` bridge invocation."""

    payload = event.get("input")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("name") or "").strip()


def _event_is_write_tool(event: dict[str, Any]) -> bool:
    """Whether a lifecycle event is a real DramaClaw write/generation call.

    ``tool_describe``/``tool_search`` only inspect the tool surface, while
    ``tool_call`` is the bridge wrapper around a real invocation — only the
    latter is unwrapped to its inner tool name. Everything else keeps the
    original rule: a ``dramaclaw_`` tool that is not a read-only lookup.
    """

    name = _event_tool_name(event)
    if not name:
        return False
    if name in _META_TOOL_NAMES:
        if not name.endswith("tool_call"):
            return False
        name = _event_inner_tool_name(event)
    if not name.startswith("dramaclaw_"):
        return False
    return not name.startswith(_READ_TOOL_PREFIXES)


def _event_failure_detail(event: dict[str, Any]) -> str:
    """Best-effort ``status_code``/error text from a tool event payload."""

    def scan(value: Any, depth: int = 0) -> str:
        if depth > 4:
            return ""
        if isinstance(value, dict):
            if value.get("ok") is False or value.get("error"):
                code = value.get("status_code") or value.get("code")
                message = (
                    value.get("error") or value.get("detail") or value.get("message")
                )
                if isinstance(message, dict):
                    message = message.get("detail") or message.get("message")
                parts = [str(part) for part in (code, message) if part]
                if parts:
                    return " ".join(parts)
            for nested in value.values():
                found = scan(nested, depth + 1)
                if found:
                    return found
        elif isinstance(value, (list, tuple)):
            for nested in value:
                found = scan(nested, depth + 1)
                if found:
                    return found
        elif isinstance(value, str) and value.strip()[:1] in {"{", "["}:
            try:
                return scan(json.loads(value), depth + 1)
            except (TypeError, ValueError):
                return ""
        return ""

    return scan(
        {
            "error": event.get("error"),
            "structured": event.get("structured"),
            "output": event.get("output"),
        }
    )


def _task_error_code(task) -> str:
    metadata = getattr(task, "metadata", None)
    if isinstance(metadata, dict) and metadata.get("error_code"):
        return str(metadata["error_code"])
    result = getattr(task, "result", None)
    if isinstance(result, dict):
        nested = result.get("task_metadata")
        if isinstance(nested, dict) and nested.get("error_code"):
            return str(nested["error_code"])
    return ""


def _task_is_stalled(task) -> bool:
    return _task_error_code(task) in STALL_ERROR_CODES


def _task_failure_payload(task) -> dict | None:
    """Structured failure diagnostics attached by the task runner, if any."""

    metadata = getattr(task, "metadata", None)
    if isinstance(metadata, dict) and isinstance(metadata.get("failure"), dict):
        return metadata["failure"]
    result = getattr(task, "result", None)
    if isinstance(result, dict):
        nested = result.get("task_metadata")
        if isinstance(nested, dict) and isinstance(nested.get("failure"), dict):
            return nested["failure"]
    return None


def _failure_reason_with_detail(task, reason: str) -> str:
    """Append the operation/model summary to a human-facing failure reason."""

    detail = summarize_failure(_task_failure_payload(task))
    if not detail:
        return reason
    base = (reason or "").strip()
    if detail in base:
        return base
    return f"{base}（{detail}）" if base else detail


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value: str | None) -> float:
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _state_root() -> Path:
    configured = os.environ.get("NOVELVIDEO_STATE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[3] / "state"


def _serialize_context(ctx: ProjectContext) -> str:
    data = asdict(ctx)
    for key in ("output_dir", "state_dir", "runtime_dir"):
        data[key] = str(data[key])
    data["requester_principals"] = [list(item) for item in ctx.requester_principals]
    return json.dumps(data, ensure_ascii=False)


def _deserialize_context(raw: str) -> ProjectContext:
    data = json.loads(raw)
    for key in ("output_dir", "state_dir", "runtime_dir"):
        data[key] = Path(data[key])
    data["requester_principals"] = tuple(tuple(item) for item in data["requester_principals"])
    return ProjectContext(**data)


@dataclass(frozen=True)
class DirectorAutoRun:
    run_id: str
    username: str
    project_id: str
    episode: int
    status: str
    activated_at: str
    updated_at: str
    context_json: str
    baseline_task_ids: tuple[str, ...]
    handled_task_ids: tuple[str, ...]
    recovery_keys: tuple[str, ...] = ()
    voice_policy: str = ""
    last_error: str = ""
    scope: str = "episode"
    target_episodes: int = 0
    config_json: str = "{}"
    driver: str = "agent"
    #: Stage the coordinator is currently driving and how many automatic retries
    #: it has already spent on it. ``next_attempt_at`` is the epoch gate that
    #: keeps a retry from hammering a provider that just failed.
    stage_key: str = ""
    stage_attempts: int = 0
    next_attempt_at: float = 0.0

    @property
    def context(self) -> ProjectContext:
        return _deserialize_context(self.context_json)

    @property
    def is_project_scope(self) -> bool:
        return self.scope == "project"

    @property
    def is_deterministic(self) -> bool:
        return self.driver == "deterministic"


class DirectorAutoStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or (_state_root() / "director_auto.db")

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        configure_sqlite_connection(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS director_auto_runs (
              run_id TEXT PRIMARY KEY,
              username TEXT NOT NULL,
              project_id TEXT NOT NULL,
              episode INTEGER NOT NULL,
              status TEXT NOT NULL,
              activated_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              context_json TEXT NOT NULL,
              baseline_task_ids_json TEXT NOT NULL,
              handled_task_ids_json TEXT NOT NULL,
              recovery_keys_json TEXT NOT NULL DEFAULT '[]',
              voice_policy TEXT NOT NULL DEFAULT '',
              last_error TEXT NOT NULL DEFAULT '',
              scope TEXT NOT NULL DEFAULT 'episode',
              target_episodes INTEGER NOT NULL DEFAULT 0,
              config_json TEXT NOT NULL DEFAULT '{}',
              driver TEXT NOT NULL DEFAULT 'agent',
              stage_key TEXT NOT NULL DEFAULT '',
              stage_attempts INTEGER NOT NULL DEFAULT 0,
              next_attempt_at REAL NOT NULL DEFAULT 0,
              owner_token TEXT NOT NULL DEFAULT '',
              lease_expires_at REAL NOT NULL DEFAULT 0,
              UNIQUE(username, project_id)
            )
            """
        )
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(director_auto_runs)").fetchall()
        }
        if "recovery_keys_json" not in columns:
            conn.execute(
                "ALTER TABLE director_auto_runs "
                "ADD COLUMN recovery_keys_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "voice_policy" not in columns:
            conn.execute(
                "ALTER TABLE director_auto_runs "
                "ADD COLUMN voice_policy TEXT NOT NULL DEFAULT ''"
            )
        if "owner_token" not in columns:
            conn.execute(
                "ALTER TABLE director_auto_runs "
                "ADD COLUMN owner_token TEXT NOT NULL DEFAULT ''"
            )
        if "lease_expires_at" not in columns:
            conn.execute(
                "ALTER TABLE director_auto_runs "
                "ADD COLUMN lease_expires_at REAL NOT NULL DEFAULT 0"
            )
        if "scope" not in columns:
            conn.execute(
                "ALTER TABLE director_auto_runs "
                "ADD COLUMN scope TEXT NOT NULL DEFAULT 'episode'"
            )
        if "target_episodes" not in columns:
            conn.execute(
                "ALTER TABLE director_auto_runs "
                "ADD COLUMN target_episodes INTEGER NOT NULL DEFAULT 0"
            )
        if "config_json" not in columns:
            conn.execute(
                "ALTER TABLE director_auto_runs "
                "ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "driver" not in columns:
            conn.execute(
                "ALTER TABLE director_auto_runs "
                "ADD COLUMN driver TEXT NOT NULL DEFAULT 'agent'"
            )
        if "stage_key" not in columns:
            conn.execute(
                "ALTER TABLE director_auto_runs "
                "ADD COLUMN stage_key TEXT NOT NULL DEFAULT ''"
            )
        if "stage_attempts" not in columns:
            conn.execute(
                "ALTER TABLE director_auto_runs "
                "ADD COLUMN stage_attempts INTEGER NOT NULL DEFAULT 0"
            )
        if "next_attempt_at" not in columns:
            conn.execute(
                "ALTER TABLE director_auto_runs "
                "ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0"
            )
        conn.commit()
        return conn

    @staticmethod
    def _row(row: sqlite3.Row | None) -> DirectorAutoRun | None:
        if row is None:
            return None
        return DirectorAutoRun(
            run_id=str(row["run_id"]),
            username=str(row["username"]),
            project_id=str(row["project_id"]),
            episode=int(row["episode"]),
            status=str(row["status"]),
            activated_at=str(row["activated_at"]),
            updated_at=str(row["updated_at"]),
            context_json=str(row["context_json"]),
            baseline_task_ids=tuple(json.loads(row["baseline_task_ids_json"] or "[]")),
            handled_task_ids=tuple(json.loads(row["handled_task_ids_json"] or "[]")),
            recovery_keys=tuple(json.loads(row["recovery_keys_json"] or "[]")),
            voice_policy=str(row["voice_policy"] or ""),
            last_error=str(row["last_error"] or ""),
            scope=str(row["scope"] or "episode"),
            target_episodes=int(row["target_episodes"] or 0),
            config_json=str(row["config_json"] or "{}"),
            driver=str(row["driver"] or "agent"),
            stage_key=str(row["stage_key"] or ""),
            stage_attempts=int(row["stage_attempts"] or 0),
            next_attempt_at=float(row["next_attempt_at"] or 0.0),
        )

    def upsert(self, run: DirectorAutoRun) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO director_auto_runs(
                  run_id, username, project_id, episode, status, activated_at,
                  updated_at, context_json, baseline_task_ids_json,
                  handled_task_ids_json, recovery_keys_json, voice_policy, last_error,
                  scope, target_episodes, config_json, driver,
                  stage_key, stage_attempts, next_attempt_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username, project_id) DO UPDATE SET
                  run_id=excluded.run_id,
                  episode=excluded.episode,
                  status=excluded.status,
                  activated_at=excluded.activated_at,
                  updated_at=excluded.updated_at,
                  context_json=excluded.context_json,
                  baseline_task_ids_json=excluded.baseline_task_ids_json,
                  handled_task_ids_json=excluded.handled_task_ids_json,
                  recovery_keys_json=excluded.recovery_keys_json,
                  voice_policy=excluded.voice_policy,
                  last_error=excluded.last_error,
                  scope=excluded.scope,
                  target_episodes=excluded.target_episodes,
                  config_json=excluded.config_json,
                  driver=excluded.driver,
                  stage_key=excluded.stage_key,
                  stage_attempts=excluded.stage_attempts,
                  next_attempt_at=excluded.next_attempt_at,
                  owner_token=CASE
                    WHEN director_auto_runs.run_id != excluded.run_id THEN ''
                    ELSE director_auto_runs.owner_token
                  END,
                  lease_expires_at=CASE
                    WHEN director_auto_runs.run_id != excluded.run_id THEN 0
                    ELSE director_auto_runs.lease_expires_at
                  END
                """,
                (
                    run.run_id,
                    run.username,
                    run.project_id,
                    run.episode,
                    run.status,
                    run.activated_at,
                    run.updated_at,
                    run.context_json,
                    json.dumps(list(run.baseline_task_ids)),
                    json.dumps(list(run.handled_task_ids)),
                    json.dumps(list(run.recovery_keys)),
                    run.voice_policy,
                    run.last_error,
                    run.scope,
                    int(run.target_episodes or 0),
                    run.config_json or "{}",
                    run.driver,
                    run.stage_key,
                    int(run.stage_attempts or 0),
                    float(run.next_attempt_at or 0.0),
                ),
            )

    def get(self, username: str, project_id: str) -> DirectorAutoRun | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM director_auto_runs WHERE username=? AND project_id=?",
                (username, project_id),
            ).fetchone()
        return self._row(row)

    def active(self) -> list[DirectorAutoRun]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM director_auto_runs WHERE status='running'"
            ).fetchall()
        return [run for row in rows if (run := self._row(row)) is not None]

    def claim_lease(
        self,
        run_id: str,
        owner_token: str,
        *,
        lease_seconds: float = LEASE_SECONDS,
        now: float | None = None,
    ) -> bool:
        """Atomically claim one running coordinator lease across API processes."""

        current = datetime.now(timezone.utc).timestamp() if now is None else float(now)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE director_auto_runs
                SET owner_token=?, lease_expires_at=?
                WHERE run_id=? AND status='running'
                  AND (owner_token='' OR owner_token=? OR lease_expires_at<=?)
                """,
                (owner_token, current + lease_seconds, run_id, owner_token, current),
            )
            conn.commit()
            return cursor.rowcount == 1

    def renew_lease(
        self,
        run_id: str,
        owner_token: str,
        *,
        lease_seconds: float = LEASE_SECONDS,
        now: float | None = None,
    ) -> bool:
        current = datetime.now(timezone.utc).timestamp() if now is None else float(now)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE director_auto_runs
                SET lease_expires_at=?
                WHERE run_id=? AND status='running' AND owner_token=?
                  AND lease_expires_at>?
                """,
                (current + lease_seconds, run_id, owner_token, current),
            )
            conn.commit()
            return cursor.rowcount == 1

    def owns_lease(
        self,
        run_id: str,
        owner_token: str,
        *,
        now: float | None = None,
    ) -> bool:
        current = datetime.now(timezone.utc).timestamp() if now is None else float(now)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM director_auto_runs
                WHERE run_id=? AND status='running' AND owner_token=?
                  AND lease_expires_at>?
                """,
                (run_id, owner_token, current),
            ).fetchone()
        return row is not None

    def update_if_owned(
        self,
        run: DirectorAutoRun,
        owner_token: str,
        *,
        now: float | None = None,
    ) -> bool:
        """Persist a worker transition only while its lease is still current."""

        current = datetime.now(timezone.utc).timestamp() if now is None else float(now)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE director_auto_runs
                SET episode=?, status=?, activated_at=?, updated_at=?, context_json=?,
                    baseline_task_ids_json=?, handled_task_ids_json=?,
                    recovery_keys_json=?, voice_policy=?, last_error=?,
                    scope=?, target_episodes=?, config_json=?, driver=?,
                    stage_key=?, stage_attempts=?, next_attempt_at=?
                WHERE run_id=? AND status='running' AND owner_token=?
                  AND lease_expires_at>?
                """,
                (
                    run.episode,
                    run.status,
                    run.activated_at,
                    run.updated_at,
                    run.context_json,
                    json.dumps(list(run.baseline_task_ids)),
                    json.dumps(list(run.handled_task_ids)),
                    json.dumps(list(run.recovery_keys)),
                    run.voice_policy,
                    run.last_error,
                    run.scope,
                    int(run.target_episodes or 0),
                    run.config_json or "{}",
                    run.driver,
                    run.stage_key,
                    int(run.stage_attempts or 0),
                    float(run.next_attempt_at or 0.0),
                    run.run_id,
                    owner_token,
                    current,
                ),
            )
            conn.commit()
            return cursor.rowcount == 1

    def release_lease(self, run_id: str, owner_token: str) -> bool:
        """Release only the caller's lease; stale workers cannot release a new owner."""

        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE director_auto_runs
                SET owner_token='', lease_expires_at=0
                WHERE run_id=? AND owner_token=?
                """,
                (run_id, owner_token),
            )
            conn.commit()
            return cursor.rowcount == 1

    def force_release_lease(self, run_id: str) -> None:
        """User/control-plane path for intentionally stopping any current owner."""

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE director_auto_runs
                SET owner_token='', lease_expires_at=0
                WHERE run_id=?
                """,
                (run_id,),
            )
            conn.commit()


def _relevant_tasks(run: DirectorAutoRun, tasks: Iterable[TaskState]) -> list[TaskState]:
    if run.is_project_scope:
        # A project-scope run spans global prep (episode 0) and every episode it
        # is producing, so it watches all mainline task types.
        return [task for task in tasks if task.task_type in AUTO_TASK_TYPES]
    return [
        task
        for task in tasks
        if task.task_type in AUTO_TASK_TYPES
        and (task.episode in {0, run.episode})
    ]


def _task_label(task: TaskState) -> str:
    result = task.result if isinstance(task.result, dict) else {}
    scope_parts = [part.strip() for part in str(task.scope or "").split(":") if part.strip()]
    if task.task_type == "character_portrait":
        character = str(result.get("character_name") or "").strip()
        if not character and len(scope_parts) > 1:
            character = scope_parts[1]
        return f"{character}肖像" if character else "角色肖像"
    if task.task_type == "identity_image":
        character = str(result.get("character_name") or "").strip()
        identity = str(result.get("identity_name") or "").strip()
        if not character and len(scope_parts) > 1:
            character = scope_parts[1]
        if not identity and len(scope_parts) > 3:
            identity = scope_parts[3]
        if character and identity:
            return f"{character}「{identity}」身份图"
        return f"{character or identity or '角色'}身份图"
    if task.task_type == "single_video" and task.beat_num:
        return f"第 {task.episode} 集 Beat {task.beat_num} 视频"
    if task.task_type == "seedance2_prompt" and task.beat_num:
        return f"第 {task.episode} 集 Beat {task.beat_num} 提示词"
    labels = {
        "ingest_fast": "项目摄入",
        "screenplay_preprocess": "剧本预处理",
        "script_process": "剧本处理",
        "build_characters": "角色构建",
        "build_props": "道具构建",
        "build_episodes": "剧集规划",
        "identity_planner": "身份规划",
        "character_portrait": "角色肖像",
        "identity_image": "身份图生成",
        "script_writer": "剧本生成",
        "content_rewriter": "解说改写",
        "literal_script_writer": "解说稿生成",
        "director_notes": "导演说明",
        "build_scenes": "场景规划",
        "episode_scene_planner": "本集场景规划",
        "episode_prop_planner": "本集道具规划",
        "scene_reference_asset": "场景参考图",
        "scene_pano_generation": "场景 360 全景图",
        "prop_reference_asset": "道具参考图",
        "sketch_grid_generation": "草图网格",
        "sketch_generation": "草图生成",
        "sketch_regen": "草图重生成",
        "grid_regenerate": "草图网格重生成",
        "mainline_sketch_from_context": "草图生成",
        "mainline_frame_from_context": "首帧生成",
        "ai_identity_detection": "身份检测",
        "global_optimize_video": "全局优化",
        "selected_regen": "首帧生成",
        "render_plan": "渲染规划",
        "system_voice_setup": "系统声线准备",
        "audio_generation_indextts2": "音频生成",
        "indextts2_audio_generation": "音频生成",
        "audio_generation": "音频生成",
        "single_video": "视频生成",
        "seedance2_prompt": "2.0 提示词",
        "compose_episode": "成片合成",
    }
    return labels.get(task.task_type, task.task_type)


def _task_instruction(task: TaskState) -> str:
    """The step's own command, bracketed for the chat timeline.

    Automatic production narrates itself into the 捏导 conversation, and the
    user reads those lines as step instructions rather than as prose.  Wrapping
    the stage name in ``[]`` — ``[剧本处理] 已完成。`` — keeps every one of them
    visually identical, so the bracketed text is always "this is what the run is
    executing" and never part of a sentence about something else.
    """

    return f"[{_task_label(task)}]"


def _missing_character_portrait(task: TaskState) -> str | None:
    """Return the exact missing character for the one safe auto-repair case."""

    if task.task_type != "identity_image" or task.status != "failed":
        return None
    match = _MISSING_CHARACTER_PORTRAIT_RE.search(str(task.error or ""))
    if match is None:
        return None
    character = match.group("character").strip()
    return character or None


class DirectorAutoCoordinator:
    def __init__(self, store: DirectorAutoStore | None = None) -> None:
        self.store = store or DirectorAutoStore()
        self.owner_token = uuid.uuid4().hex
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._stopping = False
        #: Last continuation breadcrumb per run: ``(saw_write, last_tool_note)``.
        #: Used to tell "the agent only did reads" apart from "the agent wrote
        #: but produced no task" when the durable run has to pause.
        self._continue_notes: dict[str, tuple[bool, str]] = {}

    def _remember_continuation(self, run_id: str, *, wrote: bool, note: str) -> None:
        self._continue_notes[run_id] = (bool(wrote), str(note or ""))

    def _last_continuation(self, run_id: str) -> tuple[bool, str]:
        info = self._continue_notes.pop(run_id, None)
        if info is None:
            # No agent turn was recorded (e.g. the deterministic fast path
            # dispatched a stage): do not blame the agent for the pause.
            return True, ""
        return info

    async def start(
        self,
        *,
        username: str,
        ctx: ProjectContext,
        episode: int,
        voice_policy: str = "",
        scope: str = "episode",
        target_episodes: int = 0,
        config: dict[str, Any] | None = None,
        driver: str = "agent",
    ) -> DirectorAutoRun:
        tasks = await asyncio.to_thread(get_task_manager().list_tasks_for_project, ctx)
        now = _utc_now()
        run = DirectorAutoRun(
            run_id=uuid.uuid4().hex,
            username=username,
            project_id=ctx.project_id,
            episode=max(int(episode), 0),
            status="running",
            activated_at=now,
            updated_at=now,
            context_json=_serialize_context(ctx),
            # Adopt tasks already running when the user confirms auto mode;
            # only terminal history is baseline noise.
            baseline_task_ids=tuple(
                task.task_id for task in tasks if task.status in TERMINAL_STATUSES
            ),
            handled_task_ids=(),
            voice_policy=voice_policy if voice_policy in {"system", "custom"} else "",
            scope="project" if scope == "project" else "episode",
            target_episodes=max(int(target_episodes or 0), 0),
            config_json=json.dumps(config or {}, ensure_ascii=False),
            driver="deterministic" if driver == "deterministic" else "agent",
        )
        await asyncio.to_thread(self.store.upsert, run)
        self._ensure_worker(run)
        await self._broadcast_status(run)
        return run

    async def pause(
        self,
        *,
        username: str,
        project_id: str,
        reason: str = "",
        terminal_task_id: str | None = None,
    ) -> DirectorAutoRun | None:
        run = await asyncio.to_thread(self.store.get, username, project_id)
        if run is None:
            return None
        updated = DirectorAutoRun(
            **{
                **asdict(run),
                "status": "paused",
                "updated_at": _utc_now(),
                "last_error": reason,
            }
        )
        await asyncio.to_thread(self.store.upsert, updated)
        await asyncio.to_thread(self.store.force_release_lease, run.run_id)
        await self._broadcast_status(updated, terminal_task_id=terminal_task_id)
        worker = self._workers.pop(run.run_id, None)
        if worker is not None and worker is not asyncio.current_task():
            worker.cancel()
        return updated

    async def suspend_for_confirmation(
        self,
        *,
        username: str,
        project_id: str,
        reason: str = "等待用户确认是否修改",
    ) -> DirectorAutoRun | None:
        """Suspend future auto steps without cancelling any queued/running task."""

        run = await asyncio.to_thread(self.store.get, username, project_id)
        if run is None:
            return None
        if run.status == "awaiting_confirmation":
            return run
        if run.status != "running":
            raise ValueError("本集自动当前未在运行，不能进入修改确认状态")
        updated = DirectorAutoRun(
            **{
                **asdict(run),
                "status": "awaiting_confirmation",
                "updated_at": _utc_now(),
                "last_error": reason,
            }
        )
        await asyncio.to_thread(self.store.upsert, updated)
        await asyncio.to_thread(self.store.force_release_lease, run.run_id)
        await self._broadcast_status(updated)
        worker = self._workers.pop(run.run_id, None)
        if worker is not None and worker is not asyncio.current_task():
            worker.cancel()
        return updated

    async def resume_suspended(
        self,
        *,
        username: str,
        project_id: str,
    ) -> DirectorAutoRun | None:
        """Resume the same durable run after the user declines a proposed change."""

        run = await asyncio.to_thread(self.store.get, username, project_id)
        if run is None:
            return None
        if run.status == "running":
            self._ensure_worker(run)
            return run
        if run.status != "awaiting_confirmation":
            raise ValueError("只有等待修改确认的本集自动任务可以恢复")
        updated = DirectorAutoRun(
            **{
                **asdict(run),
                "status": "running",
                "updated_at": _utc_now(),
                "last_error": "",
                # A user decision is a fresh start: do not hold the attempts
                # spent before the pause against the next failure.
                "stage_key": "",
                "stage_attempts": 0,
                "next_attempt_at": 0.0,
            }
        )
        await asyncio.to_thread(self.store.upsert, updated)
        self._ensure_worker(updated)
        await self._broadcast_status(updated)
        return updated

    async def get(self, *, username: str, project_id: str) -> DirectorAutoRun | None:
        return await asyncio.to_thread(self.store.get, username, project_id)

    async def resume(self) -> None:
        self._stopping = False
        for run in await asyncio.to_thread(self.store.active):
            self._ensure_worker(run)

    async def shutdown(self) -> None:
        self._stopping = True
        workers = list(self._workers.values())
        self._workers.clear()
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    def _ensure_worker(self, run: DirectorAutoRun) -> None:
        existing = self._workers.get(run.run_id)
        if existing is not None and not existing.done():
            return
        worker = asyncio.create_task(
            self._run_with_lease(run.run_id, run.username, run.project_id),
            name=f"director-auto:{run.project_id}:{run.episode}",
        )
        self._workers[run.run_id] = worker
        worker.add_done_callback(lambda _done, run_id=run.run_id: self._workers.pop(run_id, None))

    async def _run_with_lease(self, run_id: str, username: str, project_id: str) -> None:
        while not self._stopping:
            claimed = await asyncio.to_thread(
                self.store.claim_lease,
                run_id,
                self.owner_token,
            )
            if claimed:
                break
            run = await asyncio.to_thread(self.store.get, username, project_id)
            if run is None or run.run_id != run_id or run.status != "running":
                return
            await asyncio.sleep(HEARTBEAT_SECONDS)
        else:
            return
        parent = asyncio.current_task()
        heartbeat = asyncio.create_task(
            self._lease_heartbeat(run_id, parent),
            name=f"director-auto-heartbeat:{project_id}",
        )
        try:
            await self._run(run_id, username, project_id)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await asyncio.to_thread(
                self.store.release_lease,
                run_id,
                self.owner_token,
            )

    async def _lease_heartbeat(
        self,
        run_id: str,
        parent: asyncio.Task[None] | None,
    ) -> None:
        while not self._stopping:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            renewed = await asyncio.to_thread(
                self.store.renew_lease,
                run_id,
                self.owner_token,
            )
            if renewed:
                continue
            if parent is not None and not parent.done():
                parent.cancel()
            return

    async def _owns_run(self, run_id: str) -> bool:
        return await asyncio.to_thread(
            self.store.owns_lease,
            run_id,
            self.owner_token,
        )

    async def _update_owned(self, run: DirectorAutoRun) -> bool:
        return await asyncio.to_thread(
            self.store.update_if_owned,
            run,
            self.owner_token,
        )

    async def _pause_owned(
        self,
        *,
        run_id: str,
        username: str,
        project_id: str,
        reason: str = "",
        terminal_task_id: str | None = None,
    ) -> DirectorAutoRun | None:
        """Pause from a worker only if this coordinator still owns the run."""

        run = await asyncio.to_thread(self.store.get, username, project_id)
        if run is None or run.run_id != run_id:
            return None
        updated = DirectorAutoRun(
            **{
                **asdict(run),
                "status": "paused",
                "updated_at": _utc_now(),
                "last_error": reason,
            }
        )
        if not await self._update_owned(updated):
            return None
        await asyncio.to_thread(
            self.store.release_lease,
            run.run_id,
            self.owner_token,
        )
        await self._broadcast_status(updated, terminal_task_id=terminal_task_id)
        worker = self._workers.pop(run.run_id, None)
        if worker is not None and worker is not asyncio.current_task():
            worker.cancel()
        return updated

    async def _suspend_owned(
        self,
        *,
        run_id: str,
        username: str,
        project_id: str,
        reason: str = "",
        terminal_task_id: str | None = None,
    ) -> DirectorAutoRun | None:
        """Pause a stalled run for a user decision, keeping it resumable.

        Unlike :meth:`_pause_owned`, the run enters ``awaiting_confirmation`` so
        the frontend can offer the user a choice (keep waiting / stop) instead of
        silently abandoning automatic production.
        """

        run = await asyncio.to_thread(self.store.get, username, project_id)
        if run is None or run.run_id != run_id:
            return None
        updated = DirectorAutoRun(
            **{
                **asdict(run),
                "status": "awaiting_confirmation",
                "updated_at": _utc_now(),
                "last_error": reason,
            }
        )
        if not await self._update_owned(updated):
            return None
        await asyncio.to_thread(
            self.store.release_lease,
            run.run_id,
            self.owner_token,
        )
        await self._broadcast_status(updated, terminal_task_id=terminal_task_id)
        worker = self._workers.pop(run.run_id, None)
        if worker is not None and worker is not asyncio.current_task():
            worker.cancel()
        return updated

    def _clear_stage_signatures(self, run: DirectorAutoRun) -> tuple[str, ...]:
        """Drop the dispatcher's per-stage dedupe keys so a retry can re-enqueue.

        ``recovery_keys`` mixes two families: ``missing-character-portrait:*``
        repairs, which must survive, and ``dispatch:*`` stage signatures, which
        must not, or the deterministic fast path would refuse to re-enqueue the
        very stage being retried. Every dispatch signature is dropped rather than
        only the failing stage's prefix, because the task type a stage executes
        as does not always match its signature segment (sketches run as
        ``sketch_grid_generation`` but sign as ``sketch_generation``).
        """

        return tuple(key for key in run.recovery_keys if not key.startswith("dispatch:"))

    async def _grant_stage_retry(
        self,
        *,
        run: DirectorAutoRun,
        task: TaskState | None,
        reason: str,
    ) -> tuple[bool, int]:
        """Spend one bounded retry on the stage the durable run is driving.

        Returns ``(granted, attempt)``. When granted, the run is updated with the
        new budget, the stage dedupe signatures are cleared and a backoff gate is
        armed; the caller only has to notify and keep the loop going.
        """

        if not _stage_retry_enabled():
            return False, 0
        key = _stage_key_for(task, run)
        attempt = run.stage_attempts + 1 if run.stage_key == key else 1
        if attempt > MAX_STAGE_ATTEMPTS:
            return False, attempt
        now = datetime.now(timezone.utc).timestamp()
        granted = DirectorAutoRun(
            **{
                **asdict(run),
                "updated_at": _utc_now(),
                "recovery_keys": self._clear_stage_signatures(run),
                "stage_key": key,
                "stage_attempts": attempt,
                "next_attempt_at": now + _stage_retry_delay(attempt),
                "last_error": f"{RETRY_REASON_PREFIX}{attempt}/{MAX_STAGE_ATTEMPTS}] {reason}",
            }
        )
        if not await self._update_owned(granted):
            return False, attempt
        return True, attempt

    async def _broadcast_status(
        self,
        run: DirectorAutoRun,
        *,
        terminal_task_id: str | None = None,
    ) -> None:
        await broadcast_project_chat_event(
            username=run.username,
            project_id=run.project_id,
            payload={
                "type": "director.auto.status",
                "scope": {"kind": "project", "id": run.project_id, "surface": "director"},
                "status": run.status,
                "episode": run.episode,
                "run_id": run.run_id,
                "message": run.last_error or None,
                "terminal_task_id": terminal_task_id,
                "voice_policy": run.voice_policy or None,
            },
        )

    async def _notify(self, run: DirectorAutoRun, text: str) -> dict[str, Any]:
        ctx = run.context
        message = await asyncio.to_thread(
            chat_service.add_assistant_message,
            run.username,
            run.project_id,
            text,
            [],
            project_dir=ctx.output_dir,
            project_state_dir=ctx.state_dir,
        )
        await broadcast_project_chat_event(
            username=run.username,
            project_id=run.project_id,
            payload={
                "type": "assistant.message",
                "scope": {"kind": "project", "id": run.project_id, "surface": "director"},
                "message": message,
            },
        )
        return message

    async def _agent_continue(
        self,
        run: DirectorAutoRun,
        *,
        final_delivery: bool = False,
        instruction: str | None = None,
    ) -> bool:
        stored_ctx = run.context
        try:
            ctx = await resolve_project_context(
                user={
                    "id": stored_ctx.requester_user_id,
                    "username": stored_ctx.requester_username,
                },
                project_id=run.project_id,
                required_role="editor",
            )
        except Exception as exc:  # noqa: BLE001
            await self._pause_owned(
                run_id=run.run_id,
                username=run.username,
                project_id=run.project_id,
                reason=f"项目权限校验失败：{exc}",
            )
            await self._notify(run, "本集自动已暂停：当前用户已无权继续修改该项目。")
            return False
        # A user-driven turn and a durable continuation share the same project
        # lock. Wait instead of treating ordinary chat activity as auto failure.
        for _ in range(120):
            if not chat_service.chat_run_lock_is_active(run.username, run.project_id):
                break
            latest = await asyncio.to_thread(self.store.get, run.username, run.project_id)
            if latest is None or latest.status != "running":
                return False
            await asyncio.sleep(0.5)
        else:
            await self._pause_owned(
                run_id=run.run_id,
                username=run.username,
                project_id=run.project_id,
                reason="等待捏导空闲超时",
            )
            await self._notify(run, "本集自动已暂停：捏导长时间处于忙碌状态。")
            return False
        if run.is_project_scope:
            base_prompt = (
                "所有剧集的成片合成均已完成。请只读取并展示最终成片，然后简短收口；不要启动新的写任务。"
                if final_delivery
                else (
                    "继续自动制作：若项目准备（配置/角色/分集/肖像）尚未完成，只推进首个缺失的全局步骤；"
                    "否则只推进第 {episode} 集的下一步。只启动一个写任务，完成后收口。"
                ).format(episode=run.episode)
            )
            run_mode_block = (
                "\n\n[DRAMACLAW_RUN_MODE]\nmode=pipeline_auto\n"
                "This is a durable, server-owned one-click production run already authorized by the "
                "user. Advance the mainline pipeline from wherever it is: complete global preparation "
                "(characters, episodes, portraits) and then each episode through final composition, "
                "starting at most one write task per turn. Missing output with satisfied prerequisites "
                "should be generated without per-step confirmation. Do not stop between episodes. "
                "Pause only for: a task failure or cancellation, a missing required configuration, a "
                "missing voice policy when voice prerequisites block audio, destructive actions, or a "
                "genuinely ambiguous choice. Do not ask the user to confirm each step.\n"
                "In the same turn: read whatever state you need at most once, then start the "
                "single write task before ending the turn. Never describe a tool instead of "
                "calling it, and never end a turn on a read-only check.\n"
                "[/DRAMACLAW_RUN_MODE]"
            )
        else:
            base_prompt = (
                "第 {episode} 集成片合成已完成。请只读取并展示正式成片，然后简短收口；不要启动新的写任务。"
                if final_delivery
                else (
                    "只继续第 {episode} 集的下一步。不得启动、修改或推进其他集；"
                    "如果第 {episode} 集已经完成最终合成，只展示并收口。"
                )
            ).format(episode=run.episode)
            run_mode_block = (
                "\n\n[DRAMACLAW_RUN_MODE]\nmode=episode_auto\n"
                "This is a durable server-owned continuation already confirmed by the user. "
                "Inspect current state and start at most one safe missing mainline task. Do not ask "
                "for per-step confirmation. Missing output with satisfied prerequisites should be generated. "
                "Pause for a current-step failure, destructive action, ambiguity, missing voice choice, or "
                "unmet prerequisite, except for one exact Portrait repair explicitly authorized in the "
                "instruction above. In the same turn: read whatever state you need at most once, then "
                "start the single write task before ending the turn. Never describe a tool instead of "
                "calling it, and never end a turn on a read-only check.\n[/DRAMACLAW_RUN_MODE]"
            )
        prompt = instruction or base_prompt
        prompt += run_mode_block
        if final_delivery:
            pack_delivery = _film_pack_delivery_block()
            if pack_delivery:
                prompt += f"\n\n{pack_delivery}"
        if run.is_project_scope and run.target_episodes > 0:
            prompt += f"\n\n分集规划目标集数：{run.target_episodes}。"
        if run.voice_policy == "system":
            prompt += (
                "\n\n[DRAMACLAW_VOICE_POLICY]\npolicy=system\n"
                "The user explicitly authorized system voices when starting this episode auto run. "
                "If the current next_step is voice_setup and voices are missing, call "
                "dramaclaw_prepare_system_voices(confirmed=true) without asking again. This only "
                "prepares voices; audio generation remains a later turn.\n[/DRAMACLAW_VOICE_POLICY]"
            )
        elif run.voice_policy == "custom":
            prompt += (
                "\n\n[DRAMACLAW_VOICE_POLICY]\npolicy=custom\n"
                "The user chose uploaded/recorded custom voices. Never substitute system voices. "
                "If required custom voices are still missing at voice_setup, call "
                "dramaclaw_control_episode_auto(action='pause'), then explain the missing items and "
                "ask the user to upload or record them in 捏塘. Do not start audio generation."
                "\n[/DRAMACLAW_VOICE_POLICY]"
            )
        turn_id = f"director-auto-{run.run_id}-{uuid.uuid4().hex[:10]}"
        saw_write = False
        last_tool_note = ""

        async def on_event(event: dict[str, Any]) -> None:
            nonlocal saw_write, last_tool_note
            if event.get("type") in {"tool_started", "tool_updated", "tool_update"}:
                name = _event_tool_name(event)
                if name:
                    detail = _event_failure_detail(event)
                    last_tool_note = f"{name}（{detail}）" if detail else name
                if _event_is_write_tool(event):
                    saw_write = True
            if event.get("type") == "assistant_message" and isinstance(event.get("message"), dict):
                await broadcast_project_chat_event(
                    username=run.username,
                    project_id=run.project_id,
                    payload={
                        "type": "assistant.message",
                        "scope": {"kind": "project", "id": run.project_id, "surface": "director"},
                        "turn_id": turn_id,
                        "message": event["message"],
                    },
                )

        try:
            await chat_service.stream_assistant_reply(
                run.username,
                run.project_id,
                prompt,
                on_event,
                project_dir=ctx.output_dir,
                project_state_dir=ctx.state_dir,
                surface="director",
                turn_id=turn_id,
                route_prompt="自动继续下一步",
            )
            self._remember_continuation(
                run.run_id, wrote=saw_write, note=last_tool_note
            )
            return saw_write
        except Exception as exc:  # noqa: BLE001
            logger.exception("director auto continuation failed project=%s", run.project_id)
            await self._pause_owned(
                run_id=run.run_id,
                username=run.username,
                project_id=run.project_id,
                reason=str(exc),
            )
            await self._notify(run, "本集自动已暂停：捏导继续执行失败，请检查服务状态后重试。")
            return False

    async def _open_ctx_store(self, ctx: ProjectContext):
        from novelvideo.api.deps import make_sqlite_store_for_context

        return await make_sqlite_store_for_context(ctx)

    async def _pause_gate(self, run: DirectorAutoRun) -> str | None:
        """Return a human-facing reason to stop, or None to keep automating.

        A project-scope run only stops for a choice it must not make on the
        user's behalf: a missing project configuration, or a missing voice
        policy once audio prerequisites become the next step. Everything else
        keeps advancing.
        """

        from novelvideo.pipeline_status import _user_has_configured, compute_pipeline_status

        ctx = run.context
        if not _user_has_configured(run.username, ctx.project_name):
            return "项目配置缺失（如视觉风格、叙事方式等），请先完成配置"
        store = await self._open_ctx_store(ctx)
        try:
            data = await compute_pipeline_status(
                store=store,
                username=run.username,
                project_name=ctx.project_name,
                project_dir=ctx.output_dir,
                ctx=ctx,
                episode=run.episode if run.episode > 0 else None,
            )
        finally:
            close = getattr(store, "close", None)
            if close:
                await close()
        if str(data.get("next_step") or "") == "voice_setup" and not run.voice_policy:
            return "缺少配音声线，且未选择声线策略（系统声线／自定义声线）"
        return None

    async def _dispatch_stage(
        self, run: DirectorAutoRun, *, overlapped: bool = False
    ) -> tuple[bool, str | None]:
        """Deterministic fast path: enqueue the next stage directly.

        Returns ``(dispatched, signature)``. ``dispatched`` is False when the
        stage has no deterministic handler or was already attempted, so the
        caller should fall back to the agent. ``overlapped`` marks dispatches
        that went out while an earlier stage was still running (see
        ``_dispatch_overlap``); the timing log uses it to show how much of the
        pipeline actually overlapped.
        """

        from novelvideo.pipeline_auto import dispatch_next_stage

        store = await self._open_ctx_store(run.context)
        try:
            task_type, signature = await dispatch_next_stage(
                ctx=run.context,
                username=run.username,
                store=store,
                episode=run.episode,
                target_episodes=run.target_episodes,
                attempted=set(run.recovery_keys),
            )
        finally:
            close = getattr(store, "close", None)
            if close:
                await close()
        if task_type is not None:
            record_event(
                run.context.output_dir,
                "dispatch",
                stage=task_type,
                signature=signature,
                overlapped=overlapped,
            )
        return task_type is not None, signature

    async def _dispatch_overlap(self, run: DirectorAutoRun) -> bool:
        """在已有任务在飞时，把下一批已就绪的阶段也发出去（失败静默）。

        与 ``_continue_owned`` 的唯一区别是**不回退到 agent**：重叠派发是纯提速
        路径，任何异常或「暂无就绪产物」都只是「这次没重叠」，绝不能把 agent 拉进来
        改写正常流程。
        """

        if not (run.is_project_scope and run.is_deterministic):
            return False
        try:
            dispatched, signature = await self._dispatch_stage(run, overlapped=True)
        except Exception:  # noqa: BLE001
            logger.debug(
                "overlap dispatch skipped project=%s", run.project_id, exc_info=True
            )
            return False
        if not dispatched:
            return False
        if signature and signature not in run.recovery_keys:
            recorded = DirectorAutoRun(
                **{
                    **asdict(run),
                    "updated_at": _utc_now(),
                    "recovery_keys": tuple(
                        sorted({*run.recovery_keys, signature})
                    ),
                }
            )
            try:
                await self._update_owned(recorded)
            except Exception:  # noqa: BLE001
                logger.debug("overlap dispatch record failed", exc_info=True)
        return True

    async def _continue_owned(self, run: DirectorAutoRun) -> bool:
        """Advance one step: deterministic dispatch when configured, else agent."""

        if run.is_project_scope and run.is_deterministic:
            try:
                dispatched, signature = await self._dispatch_stage(run)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "deterministic dispatch failed project=%s", run.project_id
                )
                dispatched, signature = False, None
            if dispatched:
                if signature and signature not in run.recovery_keys:
                    recorded = DirectorAutoRun(
                        **{
                            **asdict(run),
                            "updated_at": _utc_now(),
                            "recovery_keys": tuple(
                                sorted({*run.recovery_keys, signature})
                            ),
                        }
                    )
                    await self._update_owned(recorded)
                return True
        return await self._agent_continue(run)

    async def _next_incomplete_episode(self, run: DirectorAutoRun) -> int | None:
        from novelvideo.pipeline_status import incomplete_episodes

        ctx = run.context
        store = await self._open_ctx_store(ctx)
        try:
            episodes = incomplete_episodes(store, ctx.output_dir)
        finally:
            close = getattr(store, "close", None)
            if close:
                await close()
        if run.target_episodes and run.target_episodes > 0:
            episodes = [ep for ep in episodes if ep <= run.target_episodes]
        return episodes[0] if episodes else None

    async def _run(self, run_id: str, username: str, project_id: str) -> None:
        taskless_continuations = 0
        last_overlap_at = 0.0
        busy_since: float | None = None
        while not self._stopping:
            if not await self._owns_run(run_id):
                return
            run = await asyncio.to_thread(self.store.get, username, project_id)
            if run is None or run.run_id != run_id or run.status != "running":
                return
            if run.next_attempt_at > datetime.now(timezone.utc).timestamp():
                # A retry is armed but its backoff has not elapsed yet: give the
                # upstream a moment instead of hammering it with the same request.
                await asyncio.sleep(POLL_SECONDS)
                continue
            tasks = await asyncio.to_thread(get_task_manager().list_tasks_for_project, run.context)
            relevant = _relevant_tasks(run, tasks)
            baseline = set(run.baseline_task_ids)
            handled = set(run.handled_task_ids)
            new_tasks = [task for task in relevant if task.task_id not in baseline]
            pending_new = [task for task in new_tasks if task.task_id not in handled]
            active = [task for task in pending_new if task.status in ACTIVE_STATUSES]
            if active:
                taskless_continuations = 0
                now = time.monotonic()
                if busy_since is None:
                    busy_since = now
                    record_event(run.context.output_dir, "busy_start")
                # 有任务在飞不代表必须干等：确定性调度是「按产物就绪」派发的，所以
                # 可以在旧任务跑完之前，把下一批已经就绪的活也发出去，让
                # 草图→首帧→视频→合成在时间上重叠。这是整条链路最大的一块提速：
                # 实测首帧阶段 49 次生成只花了 1.5h 计算，却铺了 10.2h 墙钟——绝大部分
                # 时间都花在「等这一阶段彻底结束才开始下一阶段」。
                if now - last_overlap_at >= OVERLAP_DISPATCH_SECONDS:
                    last_overlap_at = now
                    await self._dispatch_overlap(run)
                await asyncio.sleep(POLL_SECONDS)
                continue
            if busy_since is not None:
                record_event(
                    run.context.output_dir,
                    "busy_end",
                    duration_seconds=round(time.monotonic() - busy_since, 1),
                )
                busy_since = None

            terminal = [
                task
                for task in pending_new
                if task.status in TERMINAL_STATUSES
            ]
            if terminal:
                if not await self._owns_run(run_id):
                    return
                handled.update(task.task_id for task in terminal)
                failures = [task for task in terminal if task.status != "completed"]
                completed = [task for task in terminal if task.status == "completed"]
                # The retry budget belongs to the stage the run is driving and is
                # cleared only when the run actually moves on to another stage.
                # A completion *inside* the same stage must not clear it: for a
                # single-task stage whose completion does not satisfy the status
                # probe the dispatcher reads (something wrote less than the probe
                # looks for), clearing here would let the coordinator re-enqueue
                # the same stage forever instead of stopping after the budget.
                observed_key = str((completed or terminal)[-1].task_type or run.stage_key)
                stage_reset: dict[str, Any] = {}
                if observed_key != run.stage_key:
                    stage_reset = {
                        "stage_key": observed_key,
                        "stage_attempts": 0,
                        "next_attempt_at": 0.0,
                        "last_error": "",
                    }
                updated = DirectorAutoRun(
                    **{
                        **asdict(run),
                        "updated_at": _utc_now(),
                        "handled_task_ids": tuple(sorted(handled)),
                        **stage_reset,
                    }
                )
                if not await self._update_owned(updated):
                    return
                if failures:
                    failed = failures[0]
                    reason = failed.error or failed.current_task or failed.status
                    reason = _failure_reason_with_detail(failed, reason)
                    if _task_is_stalled(failed):
                        # The task stopped reporting progress (idle watchdog).
                        # Treat it as an ordinary transient failure while the
                        # stage still has retry budget, and only offer the user a
                        # decision once that budget is spent.
                        if not await self._owns_run(run_id):
                            return
                        granted, attempt = await self._grant_stage_retry(
                            run=updated,
                            task=failed,
                            reason=f"卡住：{reason}",
                        )
                        if granted:
                            await self._notify(
                                updated,
                                f"{_task_instruction(failed)} 长时间没有进度更新，正在自动重试"
                                f"（第 {attempt}/{MAX_STAGE_ATTEMPTS} 次）。",
                            )
                            taskless_continuations = 0
                            await asyncio.sleep(POLL_SECONDS)
                            continue
                        await self._suspend_owned(
                            run_id=run_id,
                            username=username,
                            project_id=project_id,
                            reason=f"{STALL_REASON_TAG} {_task_label(failed)}卡住：{reason}",
                            terminal_task_id=failed.task_id,
                        )
                        await self._notify(
                            updated,
                            f"本集自动已暂停：{_task_instruction(failed)} 长时间没有进度更新，"
                            "判定为卡住。请在弹出的提示中选择重试当前步骤或终止本次自动制作。",
                        )
                        return
                    character = _missing_character_portrait(failed)
                    recovery_key = f"missing-character-portrait:{character}" if character else ""
                    if (
                        len(failures) == 1
                        and character
                        and recovery_key not in set(updated.recovery_keys)
                    ):
                        if not await self._owns_run(run_id):
                            return
                        recovering = DirectorAutoRun(
                            **{
                                **asdict(updated),
                                "updated_at": _utc_now(),
                                "recovery_keys": tuple(
                                    sorted({*updated.recovery_keys, recovery_key})
                                ),
                            }
                        )
                        if not await self._update_owned(recovering):
                            return
                        await self._notify(
                            recovering,
                            f"检测到 {_task_instruction(failed)} 缺少前置肖像，"
                            f"正在自动补生成「{character}」肖像。",
                        )
                        if not await self._owns_run(run_id):
                            return
                        wrote = await self._agent_continue(
                            recovering,
                            instruction=(
                                f"自动修复第 {run.episode} 集身份图前置条件：失败信息明确指出角色"
                                f"「{character}」缺少 Portrait（面部特写）。请只读确认后，仅启动该角色"
                                "的一个 Portrait 生成任务；不要重试身份图，不要启动其他步骤，不要询问用户确认。"
                            ),
                        )
                        if wrote:
                            taskless_continuations = 0
                            await asyncio.sleep(POLL_SECONDS)
                            continue
                        latest = await asyncio.to_thread(self.store.get, username, project_id)
                        if latest is None or latest.status != "running":
                            return
                        await self._pause_owned(
                            run_id=run_id,
                            username=username,
                            project_id=project_id,
                            reason=f"无法自动补生成「{character}」肖像",
                            terminal_task_id=failed.task_id,
                        )
                        await self._notify(
                            recovering,
                            f"本集自动已暂停：未能启动「{character}」肖像生成，请检查角色肖像配置。",
                        )
                        return
                    fatal = _is_fatal_failure(failed)
                    if fatal:
                        # Billing/permission/cancellation: a retry cannot change
                        # the outcome, so stop instead of burning the budget.
                        await self._pause_owned(
                            run_id=run_id,
                            username=username,
                            project_id=project_id,
                            reason=_failure_reason_with_detail(failed, fatal),
                            terminal_task_id=failed.task_id,
                        )
                        await self._notify(
                            updated,
                            f"本集自动已暂停：{_task_instruction(failed)} 失败，{fatal}，无需自动重试。",
                        )
                        return
                    granted, attempt = await self._grant_stage_retry(
                        run=updated,
                        task=failed,
                        reason=reason,
                    )
                    if granted:
                        await self._notify(
                            updated,
                            f"{_task_instruction(failed)} 执行失败，正在自动重试"
                            f"（第 {attempt}/{MAX_STAGE_ATTEMPTS} 次）。",
                        )
                        taskless_continuations = 0
                        await asyncio.sleep(POLL_SECONDS)
                        continue
                    await self._pause_owned(
                        run_id=run_id,
                        username=username,
                        project_id=project_id,
                        reason=reason,
                        terminal_task_id=failed.task_id,
                    )
                    await self._notify(
                        updated,
                        f"本集自动已暂停：{_task_instruction(failed)} "
                        f"{('已取消' if failed.status == 'cancelled' else '失败')}，{reason}。"
                        f"（已自动重试 {max(0, min(attempt - 1, MAX_STAGE_ATTEMPTS))} 次仍未成功）",
                    )
                    return
                compose = next((task for task in terminal if task.task_type == "compose_episode"), None)
                if len(terminal) == 1:
                    summary = f"✅ {_task_instruction(terminal[0])} 已完成。"
                else:
                    summary = f"✅ 本批 {len(terminal)} 个任务已完成。"
                if compose is not None:
                    await self._notify(updated, f"{summary}正在展示第 {run.episode} 集成片。")
                    if updated.is_project_scope:
                        next_episode = await self._next_incomplete_episode(updated)
                        if next_episode is not None:
                            advanced = DirectorAutoRun(
                                **{
                                    **asdict(updated),
                                    "episode": next_episode,
                                    "updated_at": _utc_now(),
                                }
                            )
                            if not await self._update_owned(advanced):
                                return
                            await self._notify(
                                advanced,
                                f"第 {run.episode} 集已完成，成片自动继续制作第 {next_episode} 集。",
                            )
                            if not await self._owns_run(run_id):
                                return
                            await self._continue_owned(advanced)
                            taskless_continuations = 0
                            await asyncio.sleep(POLL_SECONDS)
                            continue
                    if not await self._owns_run(run_id):
                        return
                    await self._agent_continue(updated, final_delivery=True)
                    if not await self._owns_run(run_id):
                        return
                    completed = DirectorAutoRun(
                        **{**asdict(updated), "status": "completed", "updated_at": _utc_now()}
                    )
                    if not await self._update_owned(completed):
                        return
                    await self._broadcast_status(completed)
                    return
                if updated.is_project_scope:
                    pause_reason = await self._pause_gate(updated)
                    if pause_reason:
                        if not await self._owns_run(run_id):
                            return
                        await self._pause_owned(
                            run_id=run_id,
                            username=username,
                            project_id=project_id,
                            reason=pause_reason,
                        )
                        await self._notify(updated, f"自动成片已暂停：{pause_reason}。")
                        return
                auto_label = "自动成片" if updated.is_project_scope else "本集自动"
                await self._notify(updated, f"{summary}{auto_label}正在继续下一步。")
                if not await self._owns_run(run_id):
                    return
                await self._continue_owned(updated)
                # Deliberately *not* resetting ``taskless_continuations`` here: a
                # task can complete without moving the stage along (its output did
                # not satisfy the status probe the dispatcher reads). Keeping the
                # counter makes the taskless budget below the backstop for that,
                # so the run pauses after the retry budget instead of re-enqueuing
                # the same stage forever. It is cleared as soon as any task is
                # actually running (see the ``active`` branch above).
                await asyncio.sleep(POLL_SECONDS)
                continue

            activated_age = max(0.0, datetime.now(timezone.utc).timestamp() - _timestamp(run.activated_at))
            if not pending_new and activated_age >= INITIAL_GRACE_SECONDS:
                if chat_service.chat_run_lock_is_active(username, project_id):
                    await asyncio.sleep(POLL_SECONDS)
                    continue
                # A taskless continuation is often transient: the agent may have
                # spent the turn on reads (or on a route that 404s) before it
                # could start the step. Retry within a bounded budget and only
                # then pause the durable run for the user's decision.
                if taskless_continuations >= MAX_TASKLESS_CONTINUATIONS:
                    wrote, note = self._last_continuation(run_id)
                    reason = (
                        "自动续跑未产生新的任务"
                        if wrote
                        else "自动续跑未产生写任务"
                    )
                    if note:
                        reason = f"{reason}（最后一次工具调用：{note}）"
                    retried_from = _retry_pause_detail(run)
                    if retried_from:
                        reason = f"{reason}；最后失败原因：{retried_from}"
                    granted, attempt = await self._grant_stage_retry(
                        run=run,
                        task=None,
                        reason=reason,
                    )
                    if granted:
                        await self._notify(
                            run,
                            f"自动续跑连续几轮没有产生新任务，正在重新驱动"
                            f"（第 {attempt}/{MAX_STAGE_ATTEMPTS} 次）。",
                        )
                        taskless_continuations = 0
                        await asyncio.sleep(POLL_SECONDS)
                        continue
                    await self._pause_owned(
                        run_id=run_id,
                        username=username,
                        project_id=project_id,
                        reason=reason,
                    )
                    await self._notify(
                        run,
                        "本集自动已暂停：捏导连续几轮没有产生新的生成任务，"
                        "请检查当前前置条件后重试。",
                    )
                    return
                taskless_continuations += 1
                if not await self._owns_run(run_id):
                    return
                await self._continue_owned(run)
                await asyncio.sleep(POLL_SECONDS)
                continue
            await asyncio.sleep(POLL_SECONDS)


coordinator = DirectorAutoCoordinator()
