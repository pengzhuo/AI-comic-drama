"""一键成片「角色图片指定」闸门的项目配置标记 round-trip。

闸门的答案存在 ``project_config.json``（``_pause_gate`` 读的也是这里），
上传和「跳过」写入同一个标记：语义是「已答」，不是「已上传」。
"""

from __future__ import annotations

import pytest


@pytest.fixture
def gate_env(tmp_path, monkeypatch):
    import novelvideo.project_config as pc

    state_root = tmp_path / "state"
    state_root.mkdir()
    # ``get_project_config_path`` reads the module-level OUTPUT_DIR alias, and the
    # legacy bootstrap only runs when it matches STATE_DIR, so both are redirected.
    monkeypatch.setattr(pc, "STATE_DIR", state_root)
    monkeypatch.setattr(pc, "OUTPUT_DIR", state_root)
    return pc


def test_character_images_gate_defaults_to_unanswered(gate_env):
    assert gate_env.character_images_confirmed("alice", "novel-x") is False


def test_character_images_gate_round_trip(gate_env):
    gate_env.confirm_character_images("alice", "novel-x")

    assert gate_env.character_images_confirmed("alice", "novel-x") is True


def test_identity_images_gate_defaults_to_unanswered(gate_env):
    assert gate_env.identity_images_confirmed("alice", "novel-x", 1) is False


def test_identity_images_gate_is_per_episode(gate_env):
    gate_env.confirm_identity_images("alice", "novel-x", 1)

    assert gate_env.identity_images_confirmed("alice", "novel-x", 1) is True
    assert gate_env.identity_images_confirmed("alice", "novel-x", 2) is False


def test_identity_images_gate_keeps_every_answered_episode(gate_env):
    gate_env.confirm_identity_images("alice", "novel-x", 1)
    gate_env.confirm_identity_images("alice", "novel-x", 3)

    assert gate_env.identity_images_confirmed("alice", "novel-x", 1) is True
    assert gate_env.identity_images_confirmed("alice", "novel-x", 3) is True
    assert gate_env.identity_images_confirmed("alice", "novel-x", 2) is False


def test_identity_images_gate_ignores_a_non_numeric_episode(gate_env):
    assert gate_env.identity_images_confirmed("alice", "novel-x", "第一集") is False  # type: ignore[arg-type]


def test_gates_do_not_disturb_other_config_keys(gate_env):
    gate_env.save_project_config("alice", "novel-x", {"visual_style": "anime"})

    gate_env.confirm_character_images("alice", "novel-x")
    gate_env.confirm_identity_images("alice", "novel-x", 2)

    config = gate_env.load_project_config("alice", "novel-x")
    assert config["visual_style"] == "anime"
    assert config[gate_env.IDENTITY_IMAGES_CONFIRMED_KEY] == {"2": True}
