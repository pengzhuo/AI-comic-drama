from __future__ import annotations

from novelvideo.api.routes.chat import (
    _AUTO_CONFIG_KEYS,
    _auto_config_updates,
    _director_auto_payload,
    DirectorAutoStartIn,
)


def test_start_payload_defaults_to_episode_scope() -> None:
    payload = DirectorAutoStartIn()

    assert payload.scope == "episode"
    assert payload.episode == 1
    assert payload.voice_policy is None
    assert payload.target_episodes == 0
    assert payload.config is None


def test_start_payload_accepts_project_scope_with_config() -> None:
    payload = DirectorAutoStartIn(
        scope="project",
        target_episodes=12,
        voice_policy="system",
        config={"visual_style": "anime", "unknown_key": "ignored"},
    )

    assert payload.scope == "project"
    assert payload.target_episodes == 12
    assert _auto_config_updates(payload.config or {}) == {"visual_style": "anime"}


def test_auto_config_whitelist_ignores_unknown_keys() -> None:
    updates = _auto_config_updates(
        {
            "visual_style": "realistic",
            "tts_provider": "cosyvoice",
            "video_resolution": "1080x1920",
            "target_episodes": 8,
            "not_a_project_field": True,
        }
    )

    assert set(updates) <= _AUTO_CONFIG_KEYS
    assert "not_a_project_field" not in updates
    assert "target_episodes" not in updates
    assert updates["visual_style"] == "realistic"


def test_payload_reports_scope_and_target() -> None:
    class Run:
        status = "running"
        episode = 3
        run_id = "r1"
        activated_at = "t0"
        updated_at = "t1"
        last_error = ""
        voice_policy = "system"
        scope = "project"
        target_episodes = 5

    data = _director_auto_payload(Run())

    assert data["scope"] == "project"
    assert data["target_episodes"] == 5
    assert data["voice_policy"] == "system"


def test_payload_defaults_for_legacy_run_without_scope() -> None:
    class Run:
        status = "running"
        episode = 1
        run_id = "r1"
        activated_at = "t0"
        updated_at = "t1"
        last_error = ""
        voice_policy = ""

    data = _director_auto_payload(Run())

    assert data["scope"] == "episode"
    assert data["target_episodes"] == 0
