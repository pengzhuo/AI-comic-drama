"""「资产参考图」关卡：缺哪张补哪张，并插在脚本与草图之间。

回归的是「项目里有场景/道具却没有参考图」这一状态：草图只吃场景图、首帧同时吃
场景图与道具图，缺图会让服装/道具/环境在镜头之间漂移。关卡要能被 ``next_step``
定位、被自动派发逐张补齐，并且不能反向压住已经完整的状态。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from novelvideo import asset_references as asset_refs
from novelvideo import pipeline_auto
from novelvideo import pipeline_status as pipeline_status_module
from novelvideo.asset_references import (
    AssetReferenceGap,
    compute_asset_reference_gaps,
    scene_reference_gaps,
)


def _scene(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        scene_type="interior",
        environment_prompt="雨夜的街道",
        description="",
    )


def _prop(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, description="", visual_prompt="")


def _write(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")


def _mark_pano_ready(project_dir: Path, scene_name: str) -> None:
    stage = project_dir / "director_worlds" / scene_name / "v1"
    stage.mkdir(parents=True, exist_ok=True)
    (stage / "pano_360.png").write_bytes(b"x")
    (stage / "stage_manifest.json").write_text(
        json.dumps({"pano_path": "pano_360.png"}), encoding="utf-8"
    )


def test_gap_signatures_are_per_asset(tmp_path: Path) -> None:
    scene_gap = AssetReferenceGap("scene", "咖啡馆", "spatial_layout")
    pano_gap = AssetReferenceGap("pano", "咖啡馆")
    prop_gap = AssetReferenceGap("prop", "黑伞")

    assert scene_gap.signature == "dispatch:asset_ref:scene:咖啡馆:spatial_layout"
    assert pano_gap.signature == "dispatch:asset_ref:pano:咖啡馆"
    assert prop_gap.signature == "dispatch:asset_ref:prop:黑伞"
    assert "空间布局" in scene_gap.label
    assert "360" in pano_gap.label
    assert "黑伞" in prop_gap.label


def test_scene_reference_gaps_report_every_missing_kind(tmp_path: Path) -> None:
    assert scene_reference_gaps(tmp_path, "咖啡馆") == [
        AssetReferenceGap("scene", "咖啡馆", "master"),
        AssetReferenceGap("scene", "咖啡馆", "reverse_master"),
        AssetReferenceGap("scene", "咖啡馆", "spatial_layout"),
    ]

    _write(tmp_path / "assets" / "scenes" / "咖啡馆" / "master.png")
    assert [gap.detail for gap in scene_reference_gaps(tmp_path, "咖啡馆")] == [
        "reverse_master",
        "spatial_layout",
    ]


def test_gaps_are_ordered_scene_by_scene_then_props(tmp_path: Path) -> None:
    _write(tmp_path / "assets" / "scenes" / "A" / "master.png")
    _mark_pano_ready(tmp_path, "A")
    _write(tmp_path / "assets" / "props" / "伞" / "reference_3view.png")

    gaps = compute_asset_reference_gaps(
        tmp_path, scenes=[_scene("A"), _scene("B")], props=[_prop("伞"), _prop("信")]
    )

    assert [gap.signature for gap in gaps] == [
        "dispatch:asset_ref:scene:A:reverse_master",
        "dispatch:asset_ref:scene:A:spatial_layout",
        "dispatch:asset_ref:scene:B:master",
        "dispatch:asset_ref:scene:B:reverse_master",
        "dispatch:asset_ref:scene:B:spatial_layout",
        "dispatch:asset_ref:pano:B",
        "dispatch:asset_ref:prop:信",
    ]


def test_master_gap_is_listed_before_its_pano(tmp_path: Path) -> None:
    """360 的 ``pano_from_master`` 需要 master 先存在。"""

    gaps = compute_asset_reference_gaps(tmp_path, scenes=[_scene("B")], props=[])

    assert [gap.kind for gap in gaps] == ["scene", "scene", "scene", "pano"]


def test_env_switch_disables_the_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(asset_refs.ASSET_REFERENCES_ENV, "0")
    assert asset_refs.asset_references_enabled() is False

    monkeypatch.setenv(asset_refs.ASSET_REFERENCES_ENV, "yes")
    assert asset_refs.asset_references_enabled() is True


@pytest.mark.asyncio
async def test_gaps_for_store_skips_a_light_store(tmp_path: Path) -> None:
    """只实现角色/分集的桩没有资产表：按「无缺口」处理。"""

    class _LightStore:
        pass

    assert await asset_refs.asset_reference_gaps_for_store(_LightStore(), tmp_path) == []


class _AssetStore:
    def __init__(self, scenes, props) -> None:
        self._scenes = scenes
        self._props = props

    async def list_scenes(self):
        return self._scenes

    async def list_props(self):
        return self._props

    async def get_scene(self, name: str):
        for scene in self._scenes:
            if scene.name == name:
                return scene
        return None


def _ctx(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        output_dir=tmp_path,
        state_dir=tmp_path / "state",
        project_name="demo",
        requester_username="alice",
        project_id="p1",
    )


@pytest.mark.asyncio
async def test_dispatch_enqueues_one_reference_at_a_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict] = []

    async def fake_enqueue(ctx, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(task_state=SimpleNamespace(task_id="t"))

    monkeypatch.setattr(pipeline_auto, "enqueue_project_task", fake_enqueue)
    store = _AssetStore([_scene("A"), _scene("B")], [_prop("伞")])

    task_type, signature = await pipeline_auto._dispatch_asset_references(
        _ctx(tmp_path), store, tmp_path, set()
    )

    assert task_type == "scene_reference_asset"
    assert signature == "dispatch:asset_ref:scene:A:master"
    assert len(calls) == 1
    payload = calls[0]["payload"]
    assert payload["scene_name"] == "A"
    assert payload["kind"] == "master"
    assert payload["output_dir"] == str(tmp_path)
    assert payload["model"] == ""
    assert calls[0]["task_type"] == "scene_reference_asset"
    assert calls[0]["episode"] == 0
    assert calls[0]["product_surface"] == "mainline"
    assert calls[0]["queue_kind"] == "default"
    assert calls[0]["scope"]


@pytest.mark.asyncio
async def test_dispatch_skips_an_attempted_gap_and_takes_the_next(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict] = []

    async def fake_enqueue(ctx, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(task_state=SimpleNamespace(task_id="t"))

    monkeypatch.setattr(pipeline_auto, "enqueue_project_task", fake_enqueue)
    store = _AssetStore([_scene("A")], [])

    task_type, signature = await pipeline_auto._dispatch_asset_references(
        _ctx(tmp_path), store, tmp_path, {"dispatch:asset_ref:scene:A:master"}
    )

    assert task_type == "scene_reference_asset"
    assert signature == "dispatch:asset_ref:scene:A:reverse_master"
    assert calls[0]["payload"]["kind"] == "reverse_master"


@pytest.mark.asyncio
async def test_dispatch_enqueues_the_pano_for_a_mastered_scene(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict] = []

    async def fake_enqueue(ctx, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(task_state=SimpleNamespace(task_id="t"))

    monkeypatch.setattr(pipeline_auto, "enqueue_project_task", fake_enqueue)
    for name in ("master.png", "reverse_master.png", "spatial_layout.png"):
        _write(tmp_path / "assets" / "scenes" / "A" / name)
    store = _AssetStore([_scene("A")], [])

    task_type, signature = await pipeline_auto._dispatch_asset_references(
        _ctx(tmp_path), store, tmp_path, set()
    )

    assert task_type == "scene_pano_generation"
    assert signature == "dispatch:asset_ref:pano:A"
    assert calls[0]["payload"]["step"] == "pano_from_master"
    assert calls[0]["queue_kind"] == "world"
    assert calls[0]["payload"]["params"]["style"] == ""
    assert calls[0]["payload"]["params"]["description"]
    assert calls[0]["payload"]["project_dir"] == str(tmp_path)


@pytest.mark.asyncio
async def test_dispatch_enqueues_the_pano_from_text_without_a_master(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict] = []

    async def fake_enqueue(ctx, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(task_state=SimpleNamespace(task_id="t"))

    monkeypatch.setattr(pipeline_auto, "enqueue_project_task", fake_enqueue)
    store = _AssetStore([_scene("A")], [])

    await pipeline_auto._enqueue_asset_reference(
        _ctx(tmp_path), store, tmp_path, AssetReferenceGap("pano", "A")
    )

    assert calls[0]["payload"]["step"] == "pano_from_text"


@pytest.mark.asyncio
async def test_dispatch_enqueues_a_prop_reference(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict] = []

    async def fake_enqueue(ctx, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(task_state=SimpleNamespace(task_id="t"))

    monkeypatch.setattr(pipeline_auto, "enqueue_project_task", fake_enqueue)
    store = _AssetStore([], [_prop("黑伞")])

    task_type, signature = await pipeline_auto._dispatch_asset_references(
        _ctx(tmp_path), store, tmp_path, set()
    )

    assert task_type == "prop_reference_asset"
    assert signature == "dispatch:asset_ref:prop:黑伞"
    assert calls[0]["payload"]["prop_name"] == "黑伞"
    assert calls[0]["payload"]["model"] == ""
    assert calls[0]["payload"]["billing"]["pricing_kind"] == "image"


# ---------------------------------------------------------------------------
# pipeline status: the stage sits between script and sketches
# ---------------------------------------------------------------------------


class _Manager:
    def get_task(self, *_args, **_kwargs):
        return None

    def get_task_for_project(self, *_args, **_kwargs):
        return None


class _Store:
    def __init__(self, characters, episodes, scenes, props) -> None:
        self._characters = characters
        self._episodes = episodes
        self._scenes = scenes
        self._props = props

    def get_all_characters(self):
        return self._characters

    def get_all_episodes(self):
        return self._episodes

    def get_episode(self, _number: int):
        return self._episodes[0]

    async def get_beats_as_dicts(self, _episode: int):
        return [{"beat_number": 1, "narration": "台词", "visual_description": "画面"}]

    async def list_scenes(self):
        return self._scenes

    async def list_props(self):
        return self._props


def _patch_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pipeline_status_module, "get_task_manager", lambda: _Manager())
    monkeypatch.setattr(pipeline_status_module, "_user_has_configured", lambda *_: True)
    monkeypatch.setattr(
        pipeline_status_module,
        "compute_portrait_path",
        lambda _dir, name: tmp_path / f"{name}_portrait.png",
    )
    monkeypatch.setattr(
        pipeline_status_module,
        "compute_identity_path",
        lambda _dir, name, identity: tmp_path / f"{name}_{identity}.png",
    )


async def _status(tmp_path: Path, scenes, props):
    character = SimpleNamespace(
        name="许知意",
        face_prompt="face",
        is_main=True,
        identities=[SimpleNamespace(identity_id="许知意_十八岁", identity_name="十八岁")],
    )
    episode = SimpleNamespace(number=1, identity_ids=["许知意_十八岁"])
    return await pipeline_status_module.compute_pipeline_status(
        store=_Store([character], [episode], scenes, props),
        username="alice",
        project_name="demo",
        project_dir=tmp_path,
        episode=1,
    )


@pytest.mark.asyncio
async def test_status_blocks_on_missing_assets_before_sketches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_status(monkeypatch, tmp_path)

    data = await _status(tmp_path, [_scene("A")], [_prop("伞")])
    status = data["episode_status"]

    assert status["script"] is True
    assert status["asset_references"] is False
    assert status["sketches"] is False
    # 顺序：资产参考图在草图之前。
    assert data["next_step"] == "asset_references"
    assert data["next_step_name"] == "资产参考图"
    signatures = [gap["signature"] for gap in status["asset_references_missing"]]
    assert signatures[0] == "dispatch:asset_ref:scene:A:master"
    assert signatures[-1] == "dispatch:asset_ref:prop:伞"


@pytest.mark.asyncio
async def test_status_moves_on_to_sketches_when_assets_are_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_status(monkeypatch, tmp_path)
    for name in ("master.png", "reverse_master.png", "spatial_layout.png"):
        _write(tmp_path / "assets" / "scenes" / "A" / name)
    _mark_pano_ready(tmp_path, "A")
    _write(tmp_path / "assets" / "props" / "伞" / "reference_3view.png")

    data = await _status(tmp_path, [_scene("A")], [_prop("伞")])

    assert data["episode_status"]["asset_references"] is True
    assert data["episode_status"]["asset_references_missing"] == []
    assert data["next_step"] == "sketch_generation"


@pytest.mark.asyncio
async def test_status_ignores_the_stage_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_status(monkeypatch, tmp_path)
    monkeypatch.setenv(asset_refs.ASSET_REFERENCES_ENV, "0")

    data = await _status(tmp_path, [_scene("A")], [_prop("伞")])

    assert data["episode_status"]["asset_references"] is True
    assert data["next_step"] == "sketch_generation"
