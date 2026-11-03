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


# ---------------------------------------------------------------------------
# 「制作成本预估」闸门（分镜之后、第一批花钱的图/视频之前）
# ---------------------------------------------------------------------------


def test_cost_estimate_gate_defaults_to_on_and_unanswered(gate_env):
    # 缺省视为开：新项目默认在花大钱前停一次。
    assert gate_env.cost_estimate_gate_enabled("alice", "novel-x") is True
    assert gate_env.cost_estimate_confirmed("alice", "novel-x", 1) is False


def test_cost_estimate_gate_can_be_switched_off(gate_env):
    gate_env.set_cost_estimate_gate("alice", "novel-x", False)

    assert gate_env.cost_estimate_gate_enabled("alice", "novel-x") is False
    assert gate_env.load_project_config("alice", "novel-x")[gate_env.COST_ESTIMATE_GATE_KEY] is False


def test_cost_estimate_answer_is_per_episode(gate_env):
    gate_env.confirm_cost_estimate("alice", "novel-x", 1)

    assert gate_env.cost_estimate_confirmed("alice", "novel-x", 1) is True
    assert gate_env.cost_estimate_confirmed("alice", "novel-x", 2) is False
    assert gate_env.load_project_config("alice", "novel-x")[gate_env.COST_ESTIMATE_CONFIRMED_KEY] == {
        "1": True
    }


def test_cost_estimate_gate_ignores_a_non_numeric_episode(gate_env):
    assert gate_env.cost_estimate_confirmed("alice", "novel-x", "第一集") is False  # type: ignore[arg-type]


def test_cost_estimate_gate_does_not_disturb_other_config_keys(gate_env):
    gate_env.save_project_config("alice", "novel-x", {"visual_style": "anime"})

    gate_env.confirm_cost_estimate("alice", "novel-x", 2)
    gate_env.set_cost_estimate_gate("alice", "novel-x", False)

    config = gate_env.load_project_config("alice", "novel-x")
    assert config["visual_style"] == "anime"
    assert config[gate_env.COST_ESTIMATE_CONFIRMED_KEY] == {"2": True}
    assert config[gate_env.COST_ESTIMATE_GATE_KEY] is False


# ---------------------------------------------------------------------------
# 伴点预扣台账（points_charges）：同一集只扣一次
# ---------------------------------------------------------------------------


def test_points_charge_defaults_to_none(gate_env):
    assert gate_env.points_charge("alice", "novel-x", 1) is None


def test_points_charge_round_trip_and_is_per_episode(gate_env):
    entry = gate_env.record_points_charge(
        "alice", "novel-x", 1, amount=23081, cny=230.81, phone="13800138000"
    )
    assert entry["amount"] == 23081

    stored = gate_env.points_charge("alice", "novel-x", 1)
    assert stored["amount"] == 23081
    assert stored["cny"] == 230.81
    assert stored["phone"] == "13800138000"
    assert stored["at"]
    assert gate_env.points_charge("alice", "novel-x", 2) is None


def test_points_charge_does_not_disturb_other_config_keys(gate_env):
    gate_env.save_project_config("alice", "novel-x", {"visual_style": "anime"})
    gate_env.confirm_cost_estimate("alice", "novel-x", 1)
    gate_env.record_points_charge(
        "alice", "novel-x", 1, amount=100, cny=1.0, phone="13800138000"
    )

    config = gate_env.load_project_config("alice", "novel-x")
    assert config["visual_style"] == "anime"
    assert config[gate_env.COST_ESTIMATE_CONFIRMED_KEY] == {"1": True}
    assert config[gate_env.POINTS_CHARGES_KEY]["1"]["amount"] == 100


def test_points_charge_ignores_a_non_numeric_episode(gate_env):
    assert gate_env.points_charge("alice", "novel-x", "第一集") is None  # type: ignore[arg-type]
