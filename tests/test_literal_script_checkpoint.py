"""逐行剧本的断点续跑：落点、复用条件、成功后删档。"""

from __future__ import annotations

import json
from hashlib import sha256
from types import SimpleNamespace

import pytest

from novelvideo.agents.asset_compiler import AssetCompiler
from novelvideo.workflows.literal_script_writing import (
    SCRIPT_CHECKPOINT_RELATIVE_DIR,
    SCRIPT_CHECKPOINT_VERSION,
    LiteralBeatMetaOutput,
    LiteralScriptWritingWorkflow,
)
from novelvideo.workflows.script_writing import create_script_writing_workflow

# 两个场次块、共 10 个内容行（第 6 行仍在第一块里，用来验证从块中间续跑）。
SOURCE = "\n".join(
    [
        "第1场 苏鸾寝殿 夜 内",
        "△烛火摇晃。",
        "苏鸾：你来了。",
        "△少女抬起头。",
        "苏鸾：坐下说话。",
        "△窗外的雪更大了。",
        "苏鸾：夜深了。",
        "第2场 御书房 日 内",
        "△阳光落在书案上。",
        "沈砚：把门关上。",
        "△沈砚翻开卷宗。",
        "沈砚：谁在外面？",
    ]
)

TOTAL_LINES = 10


class _Character:
    def __init__(self, name: str, identity_id: str) -> None:
        self.name = name
        self.aliases: list[str] = []
        self.identities = [
            SimpleNamespace(identity_id=identity_id, identity_name="")
        ]


class _FakeStore:
    """够 literal workflow 跑完一集的假 store。

    刻意只暴露 ``project_dir``（与生产 ``CogneeStore`` 一致——它没有
    ``output_dir`` 属性），这样测试覆盖的是 runner 真正走的那条路径。
    """

    _props: dict = {}

    def __init__(self, episode, *, project_dir: str = "") -> None:
        self._episode = episode
        self.project_dir = project_dir
        self.saved: list = []

    async def load_graph_state(self):
        return None

    async def get_episode_from_graph(self, episode_num: int):
        return self._episode

    def get_episode(self, episode_num: int):
        return self._episode

    def get_all_characters(self):
        return [
            _Character("苏鸾", "苏鸾_默认"),
            _Character("沈砚", "沈砚_默认"),
        ]

    async def persist_narration_script(self, script):
        self.saved.append(script)


def _episode(*, extra_scene: bool = False, identity_ids=None):
    scene_menu = [
        SimpleNamespace(
            scene_id="苏鸾寝殿", base_scene_id="", variant_id="", time_of_day=""
        ),
        SimpleNamespace(scene_id="御书房", base_scene_id="", variant_id="", time_of_day=""),
    ]
    if extra_scene:
        scene_menu.append(
            SimpleNamespace(
                scene_id="后花园", base_scene_id="", variant_id="", time_of_day=""
            )
        )
    return SimpleNamespace(
        number=1,
        title="第 1 集",
        identity_ids=list(identity_ids or ["苏鸾_默认", "沈砚_默认"]),
        identity_default_map={},
        scene_menu=scene_menu,
        prop_menu=[],
    )


def _patch_agent(monkeypatch, *, fail_on_call: int | None = None) -> list[str]:
    """把 workflow 的 agent 换成「确定性回声」，返回每次调用的 prompt 列表。

    输出里带上 prompt 摘要，因此续跑拿到的 prompt 只要与完整跑不同，测试就会失败。
    """

    prompts: list[str] = []

    class _Agent:
        async def run(self, prompt: str):
            prompts.append(prompt)
            if fail_on_call is not None and len(prompts) == fail_on_call:
                raise RuntimeError("model exploded")
            current_line = ""
            for line in prompt.splitlines():
                if line.startswith("- 当前行: "):
                    current_line = line[len("- 当前行: ") :]
            if current_line.startswith("苏鸾："):
                marker, speaker, audio_type = "{{苏鸾_默认}}", "苏鸾", "dialogue"
            elif current_line.startswith("沈砚："):
                marker, speaker, audio_type = "{{沈砚_默认}}", "沈砚", "dialogue"
            else:
                marker, speaker, audio_type = "", "", "narration"
            digest = sha256(prompt.encode("utf-8")).hexdigest()[:8]
            return SimpleNamespace(
                output=LiteralBeatMetaOutput(
                    audio_type=audio_type,
                    speaker=speaker,
                    speaker_kind="character",
                    visual_description=f"{marker}画面 {digest}".strip(),
                    scene_id="",
                )
            )

    monkeypatch.setattr(
        LiteralScriptWritingWorkflow, "agent", property(lambda self: _Agent())
    )
    return prompts


@pytest.fixture(autouse=True)
def _no_asset_menu_planning(monkeypatch):
    async def _ensure(self, episode, *, source_text, on_log=None):
        return None

    monkeypatch.setattr(AssetCompiler, "ensure_episode_menus", _ensure)


def _checkpoint_path(base, episode_num: int = 1):
    return base / SCRIPT_CHECKPOINT_RELATIVE_DIR / f"ep{episode_num}.json"


async def _run(store, *, source_text: str = SOURCE, on_log=None):
    """走生产工厂构造 workflow（runner 就是这么建的）。"""

    workflow = create_script_writing_workflow(store)
    return await workflow.run(episode_num=1, source_text=source_text, on_log=on_log)


def test_production_constructor_scopes_checkpoints_to_store_project_dir(tmp_path):
    store = _FakeStore(_episode(), project_dir=str(tmp_path))

    workflow = create_script_writing_workflow(store)

    # CogneeStore 没有 output_dir，所以 workflow.output_dir 为空；
    # 断点根目录必须回落到 store.project_dir，否则这个特性在生产里会变成空操作。
    assert workflow.output_dir == ""
    assert workflow._script_checkpoint_path(1) == _checkpoint_path(tmp_path)


async def test_checkpoint_is_dropped_after_successful_persist(tmp_path, monkeypatch):
    store = _FakeStore(_episode(), project_dir=str(tmp_path))
    prompts = _patch_agent(monkeypatch)

    script = await _run(store)

    assert len(prompts) == TOTAL_LINES
    assert len(script.beats) == TOTAL_LINES
    assert store.saved == [script]
    assert not _checkpoint_path(tmp_path).exists()


async def test_failure_keeps_checkpoint_at_last_five_line_boundary(
    tmp_path, monkeypatch
):
    store = _FakeStore(_episode(), project_dir=str(tmp_path))
    _patch_agent(monkeypatch, fail_on_call=6)

    with pytest.raises(RuntimeError):
        await _run(store)

    path = _checkpoint_path(tmp_path)
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == SCRIPT_CHECKPOINT_VERSION
    assert payload["total"] == TOTAL_LINES
    assert payload["next_index"] == 6
    assert len(payload["beats"]) == 5
    assert payload["beats"][0]["beat_number"] == 1


async def test_resume_reuses_checkpoint_and_matches_clean_run(tmp_path, monkeypatch):
    store = _FakeStore(_episode(), project_dir=str(tmp_path))
    _patch_agent(monkeypatch, fail_on_call=6)
    with pytest.raises(RuntimeError):
        await _run(store)

    clean_dir = tmp_path / "clean"
    clean_store = _FakeStore(_episode(), project_dir=str(clean_dir))
    _patch_agent(monkeypatch)
    clean_script = await _run(clean_store)

    resumed_prompts = _patch_agent(monkeypatch)
    logs: list[str] = []
    script = await _run(store, on_log=logs.append)

    # 只补第 6 行之后的 5 行
    assert len(resumed_prompts) == TOTAL_LINES - 5
    # 续跑后的 beat（含同期 sticky/safe history 造成的 prompt 差异）与完整跑逐字一致
    assert [beat.model_dump() for beat in script.beats] == [
        beat.model_dump() for beat in clean_script.beats
    ]
    assert any("复用未完成的断点" in line for line in logs)
    assert not _checkpoint_path(tmp_path).exists()


async def test_resume_restores_block_scoped_state_mid_block(tmp_path, monkeypatch):
    """断点落在块中间时，候选身份集/锁定结果必须与完整跑一致。"""

    store = _FakeStore(_episode(), project_dir=str(tmp_path))
    _patch_agent(monkeypatch, fail_on_call=6)
    with pytest.raises(RuntimeError):
        await _run(store)

    resumed_prompts = _patch_agent(monkeypatch)
    await _run(store)

    # 第 6 行仍在第一块（苏鸾寝殿），恢复后候选身份只能是苏鸾_默认
    resumed_sixth = resumed_prompts[0]
    assert "苏鸾：夜深了。" in resumed_sixth
    assert "苏鸾_默认" in resumed_sixth
    assert "沈砚_默认" not in resumed_sixth
    # 续跑内已经学过第 2-5 行的锁定结果
    assert "当前场次已锁定结果" in resumed_sixth


async def test_checkpoint_dropped_when_source_text_changes(tmp_path, monkeypatch):
    store = _FakeStore(_episode(), project_dir=str(tmp_path))
    _patch_agent(monkeypatch, fail_on_call=6)
    with pytest.raises(RuntimeError):
        await _run(store)
    assert _checkpoint_path(tmp_path).exists()

    prompts = _patch_agent(monkeypatch)
    changed = SOURCE.replace("△烛火摇晃。", "△烛火猛烈摇晃。")
    logs: list[str] = []
    await _run(store, source_text=changed, on_log=logs.append)

    assert len(prompts) == TOTAL_LINES
    assert any("指纹与本次输入不一致" in line for line in logs)


async def test_checkpoint_dropped_when_episode_menu_changes(tmp_path, monkeypatch):
    store = _FakeStore(_episode(), project_dir=str(tmp_path))
    _patch_agent(monkeypatch, fail_on_call=6)
    with pytest.raises(RuntimeError):
        await _run(store)
    assert _checkpoint_path(tmp_path).exists()

    store._episode = _episode(extra_scene=True)
    prompts = _patch_agent(monkeypatch)
    await _run(store)

    assert len(prompts) == TOTAL_LINES


async def test_checkpoint_not_written_without_project_dir(tmp_path, monkeypatch):
    store = _FakeStore(_episode())
    prompts = _patch_agent(monkeypatch)

    script = await _run(store)

    assert len(prompts) == TOTAL_LINES
    assert len(script.beats) == TOTAL_LINES
    workflow = create_script_writing_workflow(store)
    assert workflow._script_checkpoint_path(1) is None
    assert list(tmp_path.iterdir()) == []


def test_fingerprint_tracks_source_style_and_menus(tmp_path):
    workflow = LiteralScriptWritingWorkflow(cognee_store=None, output_dir=str(tmp_path))
    workflow._valid_scene_ids = {"苏鸾寝殿"}
    blocks = LiteralScriptWritingWorkflow._build_scene_blocks(
        ["第1场 苏鸾寝殿 夜 内", "△烛火摇晃。"]
    )

    def fingerprint(**overrides):
        kwargs = {
            "episode_num": 1,
            "episode": _episode(),
            "source_text_sha256": "a" * 64,
            "line_count": 1,
            "scene_blocks": blocks,
            "episode_identity_default_map": {},
            "visual_style": "chinese_period_drama",
        }
        kwargs.update(overrides)
        return workflow._script_checkpoint_fingerprint(**kwargs)

    base = fingerprint()
    assert base == fingerprint()
    assert base != fingerprint(source_text_sha256="b" * 64)
    assert base != fingerprint(visual_style="anime")
    assert base != fingerprint(episode_identity_default_map={"苏鸾": "苏鸾_默认"})
    # 场景菜单经 self._valid_scene_ids 进指纹（生产的 run() 在建指纹前已填好）
    workflow._valid_scene_ids = {"苏鸾寝殿", "后花园"}
    assert base != fingerprint()


def test_load_checkpoint_rejects_broken_files(tmp_path):
    workflow = LiteralScriptWritingWorkflow(cognee_store=None, output_dir=str(tmp_path))
    path = workflow._script_checkpoint_path(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    logs: list[str] = []

    def load(fingerprint: str = "fp"):
        return workflow._load_script_checkpoint(
            episode_num=1, fingerprint=fingerprint, total=3, log=logs.append
        )

    path.write_text("{not json", encoding="utf-8")
    assert load() is None

    path.write_text(
        json.dumps({"version": SCRIPT_CHECKPOINT_VERSION + 1, "fingerprint": "fp"}),
        encoding="utf-8",
    )
    assert load() is None

    path.write_text(
        json.dumps(
            {
                "version": SCRIPT_CHECKPOINT_VERSION,
                "fingerprint": "other",
                "total": 3,
                "next_index": 2,
                "beats": [{"beat_number": 1}],
            }
        ),
        encoding="utf-8",
    )
    assert load() is None

    # 进度与已生成镜头条数不一致 → 当没有断点
    path.write_text(
        json.dumps(
            {
                "version": SCRIPT_CHECKPOINT_VERSION,
                "fingerprint": "fp",
                "total": 3,
                "next_index": 3,
                "beats": [{"beat_number": 1}],
            }
        ),
        encoding="utf-8",
    )
    assert load() is None

    # 合法断点（next_index=total+1 表示逐行已完成、只差落库）
    path.write_text(
        json.dumps(
            {
                "version": SCRIPT_CHECKPOINT_VERSION,
                "fingerprint": "fp",
                "total": 1,
                "next_index": 2,
                "beats": [
                    {
                        "beat_number": 1,
                        "narration_segment": "旁白",
                        "visual_description": "画面",
                    }
                ],
                "current_block_index": 0,
                "episode_sticky_identities": {"苏鸾": "苏鸾_默认"},
                "block_sticky_identities": {},
                "safe_prompt_history": ["△烛火摇晃。"],
                "degraded_lines": [1],
            }
        ),
        encoding="utf-8",
    )
    payload = workflow._load_script_checkpoint(
        episode_num=1, fingerprint="fp", total=1, log=logs.append
    )
    assert payload is not None
    assert payload["next_index"] == 2
    assert payload["beats"][0].visual_description == "画面"
    assert payload["episode_sticky_identities"] == {"苏鸾": "苏鸾_默认"}
    assert payload["degraded_lines"] == [1]
