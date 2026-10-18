"""legacy Agent 的模型路由测试。

CE 网关只开通 ``DC-*`` 逻辑别名，直连厂商的预设模型名（``doubao-seed-1-6-251015``
等）在网关上没有渠道；这里锁住「不许再把预设模型名发给网关」这条防线。
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

from novelvideo import config
from novelvideo.model_gateway_settings import save_custom_newapi_gateway
from novelvideo.shared import runtime_env

_IMAGE_VERIFIERS = [
    ("novelvideo.verification.frame_verifier", "FrameVerifier"),
    ("novelvideo.verification.image_verifier", "ImageVerifier"),
    ("novelvideo.verification.sketch_comparer", "SketchComparer"),
    ("novelvideo.verification.sketch_scorer", "SketchScorer"),
    ("novelvideo.verification.consistency_verifier", "ConsistencyVerifier"),
    ("novelvideo.verification.continuity_verifier", "ContinuityVerifier"),
    ("novelvideo.verification.episode_reviewer", "EpisodeReviewer"),
]


def _isolate_settings_db(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(runtime_env, "load_project_dotenv", lambda override=False: None)
    monkeypatch.setenv("ST_EDITION", "ce")
    for key in (
        "ST_CONTROL_PLANE_DSN",
        "MODEL_GATEWAY_MODE",
        "MODEL_GATEWAY_RUNTIME_VERSION",
        "NEWAPI_API_KEY",
        "NEWAPI_BASE_URL",
        "MODEL_NAME",
        "LEGACY_AGENT_MODEL",
        "VERIFICATION_VISION_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)


def _configure_ce_gateway() -> None:
    """CE 运行时的网关凭据来自 settings 库（与线上一致）。"""

    save_custom_newapi_gateway(
        base_url="http://new-api:3000",
        api_key="sk-database-secret",
        activate=True,
    )


def test_legacy_factory_routes_ce_default_to_gateway_alias(monkeypatch, tmp_path):
    _isolate_settings_db(monkeypatch, tmp_path)
    _configure_ce_gateway()
    captured: dict[str, object] = {}

    def fake_model(model_name, **kwargs):
        captured.update(model_name=model_name, **kwargs)
        return "ce-model"

    monkeypatch.setattr(config, "_newapi_text_openai_model", fake_model)

    assert config.get_pydantic_model() == "ce-model"
    assert captured["model_name"] == "DC-hermes-LLM"

    monkeypatch.setenv("LEGACY_AGENT_MODEL", "DC-custom-agent-LLM")
    assert config.get_pydantic_model() == "ce-model"
    assert captured["model_name"] == "DC-custom-agent-LLM"

    # 显式 MODEL_NAME 依然优先。
    monkeypatch.setenv("MODEL_NAME", "DC-explicit-LLM")
    assert config.get_pydantic_model() == "ce-model"
    assert captured["model_name"] == "DC-explicit-LLM"


def test_legacy_factory_keeps_ee_preset_default(monkeypatch, tmp_path):
    _isolate_settings_db(monkeypatch, tmp_path)
    monkeypatch.setenv("ST_EDITION", "ee")
    monkeypatch.setenv("ST_CONTROL_PLANE_DSN", "postgresql://control-plane")
    monkeypatch.setenv("NEWAPI_API_KEY", "sk-ee-secret")
    monkeypatch.setenv("NEWAPI_BASE_URL", "https://ee-gateway.example/v1")
    monkeypatch.setattr(config, "NEWAPI_API_KEY", "sk-ee-secret")
    monkeypatch.setattr(config, "NEWAPI_BASE_URL", "https://ee-gateway.example/v1")
    captured: dict[str, object] = {}

    class FakeModel:
        async def request(self, *_args):
            return "ee-response"

    def fake_model(model_name, **kwargs):
        captured.update(model_name=model_name, **kwargs)
        return FakeModel()

    monkeypatch.setattr(config, "_newapi_text_openai_model", fake_model)

    # EE 仍由部署环境决定模型名，行为不变（模型在请求时才真正构造）。
    model = config.get_pydantic_model()
    assert captured == {}
    assert asyncio.run(model.request([], None, object())) == "ee-response"
    assert (
        captured["model_name"] == config.PROVIDER_PRESETS["volcengine"]["default_model"]
    )


def test_explicit_model_name_override_wins_over_ce_alias(monkeypatch, tmp_path):
    _isolate_settings_db(monkeypatch, tmp_path)
    _configure_ce_gateway()
    captured: dict[str, object] = {}

    def fake_model(model_name, **kwargs):
        captured.update(model_name=model_name, **kwargs)
        return "ce-model"

    monkeypatch.setattr(config, "_newapi_text_openai_model", fake_model)

    config.get_pydantic_model(
        provider_override="openrouter",
        model_name_override="openrouter/DC-explicit-LLM",
    )

    assert captured["model_name"] == "DC-explicit-LLM"


def test_verification_factory_routes_vision_and_text_aliases(monkeypatch, tmp_path):
    _isolate_settings_db(monkeypatch, tmp_path)
    calls: list[tuple[str, str | None]] = []

    def fake_model(model_env, default_model=None, **_kwargs):
        calls.append((model_env, default_model))
        return object()

    monkeypatch.setattr(config, "get_newapi_text_pydantic_model", fake_model)

    config.get_verification_pydantic_model()
    config.get_verification_pydantic_model(vision=True)

    assert calls == [
        ("LEGACY_AGENT_MODEL", "DC-hermes-LLM"),
        ("VERIFICATION_VISION_MODEL", "DC-video-identity-detector-LLM"),
    ]


@pytest.mark.parametrize(("module_name", "class_name"), _IMAGE_VERIFIERS)
def test_image_based_verifiers_use_vision_route(monkeypatch, module_name, class_name):
    module = importlib.import_module(module_name)
    calls: list[tuple[str, str | None]] = []

    def fake_model(model_env, default_model=None, **_kwargs):
        calls.append((model_env, default_model))
        return object()

    monkeypatch.setattr(config, "get_newapi_text_pydantic_model", fake_model)
    monkeypatch.setattr(module, "Agent", lambda model, **kwargs: model)

    verifier = getattr(module, class_name)()
    verifier._get_agent()

    assert calls == [("VERIFICATION_VISION_MODEL", "DC-video-identity-detector-LLM")]


def test_voxel_codegen_falls_back_to_gateway_alias_in_ce(monkeypatch):
    module = importlib.import_module("novelvideo.generators.scene_voxel.codegen_agent")
    monkeypatch.delenv("VOXEL_VLM_MODEL", raising=False)
    monkeypatch.setenv("ST_EDITION", "ce")

    assert module._voxel_model_name() == config.DEFAULT_LEGACY_AGENT_MODEL

    monkeypatch.setenv("VOXEL_VLM_MODEL", "DC-voxel-LLM")
    assert module._voxel_model_name() == "DC-voxel-LLM"
