"""按**项目**定位一键成片 run（不看请求者身份）。

历史 bug：闸门确认与「继续」都按请求者 ``username`` 查 run。内部端（username
``local``）代客户点确认时查不到客户的 run，答案被写进了
``state/local/<project_id>/project_config.json``，恢复也失败，却返回 200 —— 客户的
项目就永远卡在「制作成本预估」闸门上了。``get_by_project`` 是这次修复的底座。
"""

from __future__ import annotations

from novelvideo.chat.director_auto import DirectorAutoRun, DirectorAutoStore


def _run(**over) -> DirectorAutoRun:
    base = {
        "run_id": "run-a",
        "username": "13800138000",
        "project_id": "proj-1",
        "episode": 1,
        "status": "paused",
        "activated_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "context_json": "{}",
        "baseline_task_ids": (),
        "handled_task_ids": (),
    }
    base.update(over)
    return DirectorAutoRun(**base)


def test_get_by_project_ignores_requester_username(tmp_path):
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(_run(run_id="run-client", username="13800138000"))

    got = store.get_by_project("proj-1")
    assert got is not None
    assert got.run_id == "run-client"
    assert got.username == "13800138000"


def test_get_by_project_prefers_most_recent(tmp_path):
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    store.upsert(
        _run(run_id="run-old", username="local", updated_at="2026-01-01T00:00:00+00:00")
    )
    store.upsert(
        _run(
            run_id="run-new",
            username="13800138000",
            updated_at="2026-02-01T00:00:00+00:00",
        )
    )
    got = store.get_by_project("proj-1")
    assert got is not None
    assert got.run_id == "run-new"


def test_get_by_project_missing_returns_none(tmp_path):
    store = DirectorAutoStore(tmp_path / "director-auto.db")
    assert store.get_by_project("nope") is None
