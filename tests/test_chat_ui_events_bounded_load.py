"""``scope.changed`` 会把聊天历史一次性回给前端，历史读取必须是有界的。

生产事故（2026-10-22）：单个画布攒了 53,062 条 ``assistant.message_parts``
快照 / 2.78GB，``_load_ui_events`` 无 ``LIMIT`` 全量载入 → 进程涨到 8GB+ 被 cgroup
OOM 杀掉 → 容器重启 → 浏览器重连再拉一次 → OOM 死循环。
"""

from __future__ import annotations

import json

import pytest

from novelvideo.chat import store as store_module
from novelvideo.chat.store import ChatScope, ChatStore


def scope(tmp_path) -> ChatScope:
    return ChatScope(
        kind="project",
        id="project-1",
        surface="freezone",
        canvas_id="canvas-1",
        agent_id="main",
        state_dir=str(tmp_path),
    )


async def _insert_event(db, turn_id: str, event_type: str, payload: dict) -> None:
    await db.execute(
        "INSERT INTO chat_ui_events(turn_id, event_type, payload_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (turn_id, event_type, json.dumps(payload), "2026-01-01T00:00:00Z"),
    )


async def _seed_turn(store: ChatStore, target: ChatScope, turn_id: str, snapshots: int) -> None:
    await store.append_message_async("alice", target, "user", "做一张图", turn_id=turn_id)
    await store.append_message_async("alice", target, "assistant", "好的", turn_id=turn_id)
    db = await store.connect_async("alice", target)
    try:
        for index in range(snapshots):
            await _insert_event(
                db,
                turn_id,
                "assistant.message_parts",
                {"parts": [{"id": "p1", "type": "text", "text": "x", "seq": index}]},
            )
        await _insert_event(db, turn_id, "tool.result", {"ok": turn_id})
        await db.commit()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_load_ui_events_keeps_only_recent_parts_snapshots(tmp_path) -> None:
    """每个 turn 只留最近几次快照；非快照事件一条不少。"""

    store = ChatStore()
    target = scope(tmp_path)
    await _seed_turn(store, target, "turn-1", snapshots=4000)

    db = await store.connect_async("alice", target)
    try:
        loaded = await store._load_ui_events_async(db, limit=50)
    finally:
        await db.close()

    parts_events = [
        event
        for event in loaded["turn-1"]
        if event["type"] == "assistant.message_parts"
    ]
    assert len(parts_events) == store_module._UI_PARTS_SNAPSHOTS_PER_TURN
    other_events = [event for event in loaded["turn-1"] if event["type"] == "tool.result"]
    assert len(other_events) == 1


@pytest.mark.asyncio
async def test_list_messages_renders_the_newest_parts_snapshot(tmp_path) -> None:
    """合并结果必须仍是最新状态：保留最近几次快照后，parts 由最后一条快照决定。"""

    store = ChatStore()
    target = scope(tmp_path)
    await _seed_turn(store, target, "turn-1", snapshots=4000)

    messages = await store.list_messages_async("alice", target)

    assistant = next(message for message in messages if message["role"] == "assistant")
    assert [part["seq"] for part in assistant["parts"]] == [3999]


@pytest.mark.asyncio
async def test_load_ui_events_ignores_turns_outside_the_visible_window(tmp_path) -> None:
    """只读最近 ``limit`` 条消息所属 turn 的事件，旧 turn 的事件不再被拖进内存。"""

    store = ChatStore()
    target = scope(tmp_path)
    await _seed_turn(store, target, "turn-old", snapshots=50)
    await _seed_turn(store, target, "turn-new", snapshots=50)

    db = await store.connect_async("alice", target)
    try:
        loaded = await store._load_ui_events_async(db, limit=2)
    finally:
        await db.close()

    assert set(loaded) == {"turn-new"}


@pytest.mark.asyncio
async def test_append_ui_event_prunes_old_message_parts_snapshots(tmp_path) -> None:
    """写入侧上限：流式重发的整份快照只保留最近若干条，表不会无限膨胀。"""

    store = ChatStore()
    target = scope(tmp_path)
    retained = store_module._UI_PARTS_SNAPSHOTS_RETAINED
    assert retained >= store_module._UI_PARTS_SNAPSHOTS_PER_TURN

    for index in range(retained + 40):
        await store.append_ui_event_async(
            "alice",
            target,
            "turn-1",
            {"type": "assistant.message_parts", "parts": [{"id": "p1", "seq": index}]},
        )
    # 非快照事件不受裁剪影响。
    await store.append_ui_event_async("alice", target, "turn-1", {"type": "tool.result"})

    db = await store.connect_async("alice", target)
    try:
        rows = await db.execute_fetchall(
            "SELECT id, payload_json FROM chat_ui_events "
            "WHERE event_type = 'assistant.message_parts' ORDER BY id ASC"
        )
        others = await db.execute_fetchall(
            "SELECT count(*) FROM chat_ui_events WHERE event_type = 'tool.result'"
        )
    finally:
        await db.close()

    assert len(rows) == retained
    assert json.loads(rows[-1]["payload_json"])["parts"][0]["seq"] == retained + 40 - 1
    assert others[0][0] == 1


@pytest.mark.asyncio
async def test_append_ui_event_prune_does_not_touch_other_turns(tmp_path) -> None:
    store = ChatStore()
    target = scope(tmp_path)
    retained = store_module._UI_PARTS_SNAPSHOTS_RETAINED
    for index in range(retained + 10):
        await store.append_ui_event_async(
            "alice", target, "turn-old", {"type": "assistant.message_parts", "parts": [], "seq": index}
        )
    await store.append_ui_event_async(
        "alice", target, "turn-new", {"type": "assistant.message_parts", "parts": []}
    )

    db = await store.connect_async("alice", target)
    try:
        counts = await db.execute_fetchall(
            "SELECT turn_id, count(*) FROM chat_ui_events GROUP BY turn_id ORDER BY turn_id"
        )
    finally:
        await db.close()

    assert [tuple(row) for row in counts] == [("turn-new", 1), ("turn-old", retained)]


@pytest.mark.asyncio
async def test_load_ui_events_caps_total_rows(tmp_path) -> None:
    """非快照事件失控时有兜底行数上限。"""

    store = ChatStore()
    target = scope(tmp_path)
    await store.append_message_async("alice", target, "user", "你好", turn_id="turn-1")
    db = await store.connect_async("alice", target)
    try:
        for index in range(store_module._UI_EVENTS_MAX_ROWS + 400):
            await _insert_event(db, "turn-1", "tool.result", {"index": index})
        await db.commit()
        loaded = await store._load_ui_events_async(db, limit=50)
    finally:
        await db.close()

    events = loaded["turn-1"]
    assert len(events) == store_module._UI_EVENTS_MAX_ROWS
    # 截断保留的是最近的一批（id 最大），按 id 升序返回。
    assert events[-1]["index"] == store_module._UI_EVENTS_MAX_ROWS + 399
    assert [event["id"] for event in events] == sorted(event["id"] for event in events)
