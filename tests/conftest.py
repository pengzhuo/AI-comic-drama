from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def restore_ports_registry_globals():
    from novelvideo.ports import registry
    from novelvideo.ports.product_surface_access import LocalProductSurfaceAccess

    ports_snapshot = dict(registry._PORTS)
    bootstrapped_snapshot = registry._BOOTSTRAPPED
    missing_port_error_snapshot = registry.PortNotRegistered
    if "product_surface_access" not in registry._PORTS:
        registry.register_port("product_surface_access", LocalProductSurfaceAccess())
    try:
        yield
    finally:
        registry._PORTS.clear()
        registry._PORTS.update(ports_snapshot)
        registry._BOOTSTRAPPED = bootstrapped_snapshot
        # Registry contract tests reload the module. Restore the exception's
        # identity too, so consumers imported before the reload still catch it.
        registry.PortNotRegistered = missing_port_error_snapshot


@pytest.fixture(autouse=True)
def inline_task_worker_identity(monkeypatch):
    """让测试进程扮演 inline worker（= API 进程，startup 时绑定事件循环）。

    ``task_state._sweep_interrupted_inline_tasks_once`` 只在「本进程就是 inline
    worker」时才按自己的启动时刻回收在途行——只有它的作业句柄真的在内存里，这条
    推断才成立。测试里造 store/读任务行的正是"worker"，所以默认认领；
    ``test_task_state_restart_reconcile.py`` 另有用例验证非 worker 进程不许扫。
    """

    from novelvideo import task_state

    monkeypatch.setattr(task_state, "_INLINE_TASK_WORKER_CLAIMED", True)
    yield


@pytest.fixture(autouse=True)
async def close_sqlite_stores_created_by_test(monkeypatch):
    from novelvideo.sqlite_store import SQLiteStore

    stores = []
    original_init = SQLiteStore.__init__

    def tracked_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        stores.append(self)

    monkeypatch.setattr(SQLiteStore, "__init__", tracked_init)
    yield
    for store in reversed(stores):
        if not store.is_closed():
            await store.close()


@pytest.fixture(scope="session", autouse=True)
def api_coverage_testclient_patch():
    if not os.environ.get("ST_API_COVERAGE_FILE"):
        yield
        return

    from novelvideo.shared.api_coverage import (
        install_httpx_asgi_transport_api_coverage_patch,
        install_testclient_api_coverage_patch,
    )

    restore_testclient_patch = install_testclient_api_coverage_patch()
    restore_asgi_transport_patch = install_httpx_asgi_transport_api_coverage_patch()
    try:
        yield
    finally:
        restore_asgi_transport_patch()
        restore_testclient_patch()


@pytest.fixture(params=("ce", pytest.param("ee", marks=pytest.mark.ee)))
def app_client(request, monkeypatch):
    """Testing.md dual-mode fixture skeleton.

    T1 uses this only as a mode anchor; full app assembly starts in later slices.
    CE delenv does not affect an already-instantiated control_plane.config.settings
    singleton. Full app assembly (L2 from T3 onward) must rebuild via the app
    factory or a subprocess; do not use this fixture to assert settings behavior.
    """
    mode = str(request.param)
    if mode == "ce":
        monkeypatch.delenv("ST_CONTROL_PLANE_DSN", raising=False)
        monkeypatch.setenv("ST_EDITION", "ce")
    return {"mode": mode}
