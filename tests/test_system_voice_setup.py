import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from novelvideo.models import NovelCharacter


class _FakeStore:
    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir
        self.characters = [
            NovelCharacter(name="林夏", gender="女", age_group="youth", is_main=True)
        ]
        self.beats = [
            {
                "beat_number": 1,
                "audio_type": "narration",
                "narration_segment": "夜色笼罩着城市。",
            },
            {
                "beat_number": 2,
                "audio_type": "dialogue",
                "speaker": "林夏_日常",
                "narration_segment": "“我们出发吧。”",
            },
        ]

    async def get_beats_as_dicts(self, episode: int):
        assert episode == 1
        return self.beats

    async def list_characters(self):
        return self.characters

    async def update_character(self, name: str, **updates):
        character = next(item for item in self.characters if item.name == name)
        for key, value in updates.items():
            setattr(character, key, value)


@pytest.mark.asyncio
async def test_prepare_missing_system_voices_fills_narrator_and_character(
    monkeypatch,
    tmp_path,
):
    from novelvideo.audio import indextts2_beat_audio_task
    from novelvideo.audio import system_voice_setup
    from novelvideo.generators.tts_generator import TTSResult

    narrator_updates: list[dict] = []
    seen_egress_contexts: list[object] = []
    sentinel_egress_context = object()

    class FakeEdgeTTSGenerator:
        def __init__(self, voice: str, egress_context=None) -> None:
            self.voice = voice
            seen_egress_contexts.append(egress_context)

        async def generate(self, *, text, output_path, generate_subtitle):  # noqa: ARG002
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"audio:{self.voice}".encode())
            return TTSResult(success=True, audio_path=str(path))

    async def no_remaining_errors(**kwargs):  # noqa: ARG001
        return []

    monkeypatch.setattr(system_voice_setup, "EdgeTTSGenerator", FakeEdgeTTSGenerator)
    monkeypatch.setattr(
        system_voice_setup,
        "ambient_egress_context",
        lambda: sentinel_egress_context,
    )
    monkeypatch.setattr(
        system_voice_setup,
        "load_effective_narration_style_for_voice",
        lambda username, project: "third_person",
    )
    monkeypatch.setattr(
        system_voice_setup,
        "load_narrator_reference_audio",
        lambda username, project: {"path": ""},
    )
    monkeypatch.setattr(
        system_voice_setup,
        "set_narrator_reference_audio",
        lambda username, project, **kwargs: narrator_updates.append(kwargs),
    )
    monkeypatch.setattr(
        indextts2_beat_audio_task,
        "collect_indextts2_voice_prereq_errors",
        no_remaining_errors,
    )
    store = _FakeStore(tmp_path)

    result = await system_voice_setup.prepare_missing_system_voices(
        store=store,
        username="alice",
        project="demo",
        project_dir=tmp_path,
        episode=1,
    )

    assert result["ready"] is True
    assert result["prepared_count"] == 2
    assert {item["target"] for item in result["prepared"]} == {"项目解说人", "林夏"}
    assert narrator_updates[0]["relative_path"] == "assets/narrator/voice.mp3"
    assert store.characters[0].reference_audio_path
    assert (tmp_path / store.characters[0].reference_audio_path).exists()
    # Regression: the synthesizer must receive the request-scoped egress identity.
    # Without it, ``EdgeTTSGenerator`` returns ORG_SERVICE_EGRESS_DENIED even for the
    # local/CE edition, so the system-voice feature could never fill any reference.
    assert seen_egress_contexts
    assert all(context is sentinel_egress_context for context in seen_egress_contexts)


def _patch_voice_setup_io(monkeypatch, system_voice_setup, narrator_updates):
    from novelvideo.audio import indextts2_beat_audio_task

    async def no_remaining_errors(**kwargs):  # noqa: ARG001
        return []

    monkeypatch.setattr(
        indextts2_beat_audio_task,
        "collect_indextts2_voice_prereq_errors",
        no_remaining_errors,
    )
    monkeypatch.setattr(
        system_voice_setup,
        "load_effective_narration_style_for_voice",
        lambda username, project: "third_person",
    )
    monkeypatch.setattr(
        system_voice_setup,
        "load_narrator_reference_audio",
        lambda username, project: {"path": ""},
    )
    monkeypatch.setattr(
        system_voice_setup,
        "set_narrator_reference_audio",
        lambda username, project, **kwargs: narrator_updates.append(kwargs),
    )


@pytest.mark.asyncio
async def test_prepare_missing_system_voices_uses_gateway_preset_voices(monkeypatch, tmp_path):
    """配置了网关 TTS 模型时，系统声线走 preset 通道而不是 EdgeTTS。"""

    from novelvideo.audio import system_voice_setup
    from novelvideo.freezone import audio_node

    narrator_updates: list[dict] = []
    writer_calls: list[dict] = []

    class ExplodingEdgeTTSGenerator:
        def __init__(self, *args, **kwargs):  # noqa: ARG002
            raise AssertionError("EdgeTTSGenerator must not be used on the gateway path")

    async def fake_write_newapi_audio_speech(**kwargs):
        writer_calls.append(kwargs)
        path = Path(kwargs["output_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"gateway:{kwargs['voice']}".encode())
        return {"ok": True}

    _patch_voice_setup_io(monkeypatch, system_voice_setup, narrator_updates)
    monkeypatch.setenv("SYSTEM_VOICE_TTS_MODEL", "doubao-seed-audio-1-0")
    monkeypatch.setattr(system_voice_setup, "EdgeTTSGenerator", ExplodingEdgeTTSGenerator)
    monkeypatch.setattr(
        audio_node, "_write_newapi_audio_speech", fake_write_newapi_audio_speech
    )

    store = _FakeStore(tmp_path)
    result = await system_voice_setup.prepare_missing_system_voices(
        store=store,
        username="alice",
        project="demo",
        project_dir=tmp_path,
        episode=1,
    )

    assert result["ready"] is True
    assert result["prepared_count"] == 2
    by_target = {item["target"]: item for item in result["prepared"]}
    assert by_target["项目解说人"]["voice"] == system_voice_setup.GATEWAY_NARRATOR_VOICE
    assert by_target["林夏"]["voice"] in system_voice_setup.GATEWAY_FEMALE_VOICES
    assert {item["provider"] for item in result["prepared"]} == {"gateway"}

    assert {call["model"] for call in writer_calls} == {"doubao-seed-audio-1-0"}
    assert {call["voice"] for call in writer_calls} == {
        item["voice"] for item in result["prepared"]
    }
    assert all(call["metadata"] == {"speech_mode": "preset"} for call in writer_calls)
    assert all(
        call["response_format"] == "mp3" for call in writer_calls
    )
    # 参考音频仍然落到项目目录，角色/解说人的引用照旧写入。
    assert narrator_updates[0]["relative_path"] == "assets/narrator/voice.mp3"
    assert (tmp_path / "assets" / "narrator" / "voice.mp3").read_bytes().startswith(
        b"gateway:"
    )
    reference = tmp_path / store.characters[0].reference_audio_path
    assert reference.read_bytes().startswith(b"gateway:")
    # 每个声线各自缓存，避免不同角色复用同一条参考。
    cached = sorted(path.name for path in (tmp_path / "assets" / "system_voices").iterdir())
    assert len(cached) == 2
    assert all(name.startswith("gateway-doubao-seed-audio-1-0__") for name in cached)


@pytest.mark.asyncio
async def test_prepare_missing_system_voices_falls_back_to_edge_tts(monkeypatch, tmp_path):
    """网关声线生成失败时回落到 EdgeTTS，而不是让声线准备整体失败。"""

    from novelvideo.audio import system_voice_setup
    from novelvideo.freezone import audio_node
    from novelvideo.generators.tts_generator import TTSResult

    narrator_updates: list[dict] = []
    seen_voices: list[str] = []

    class FakeEdgeTTSGenerator:
        def __init__(self, voice: str, egress_context=None) -> None:  # noqa: ARG002
            self.voice = voice
            seen_voices.append(voice)

        async def generate(self, *, text, output_path, generate_subtitle):  # noqa: ARG002
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"edge:{self.voice}".encode())
            return TTSResult(success=True, audio_path=str(path))

    async def failing_write_newapi_audio_speech(**kwargs):  # noqa: ARG001
        raise RuntimeError("gateway down")

    _patch_voice_setup_io(monkeypatch, system_voice_setup, narrator_updates)
    monkeypatch.setenv("SYSTEM_VOICE_TTS_MODEL", "doubao-seed-audio-1-0")
    monkeypatch.setattr(system_voice_setup, "EdgeTTSGenerator", FakeEdgeTTSGenerator)
    monkeypatch.setattr(
        audio_node, "_write_newapi_audio_speech", failing_write_newapi_audio_speech
    )

    store = _FakeStore(tmp_path)
    result = await system_voice_setup.prepare_missing_system_voices(
        store=store,
        username="alice",
        project="demo",
        project_dir=tmp_path,
        episode=1,
    )

    assert result["ready"] is True
    assert result["prepared_count"] == 2
    assert {item["provider"] for item in result["prepared"]} == {"edge"}
    # 回落时用的是 EdgeTTS 声线表，不是网关预置音色。
    assert set(seen_voices) == {item["voice"] for item in result["prepared"]}
    assert system_voice_setup.NARRATOR_VOICE in seen_voices


@pytest.mark.asyncio
async def test_system_voice_setup_runner_reports_progress(monkeypatch, tmp_path):
    from novelvideo.audio import system_voice_setup
    from novelvideo.task_backend.runners import audio
    from novelvideo import sqlite_store

    progress_updates = []

    class FakeRunnerStore:
        def __init__(self, *args, **kwargs):  # noqa: ARG002
            self.closed = False
            self.graph_state_loaded = False

        async def initialize(self):
            return None

        async def load_graph_state(self):
            self.graph_state_loaded = True

        async def close(self):
            self.closed = True

    class FakeManager:
        def update_progress_for_project(self, *args, **kwargs):
            progress_updates.append(kwargs)

    async def fake_prepare(**kwargs):
        assert kwargs["episode"] == 2
        assert kwargs["store"].graph_state_loaded is True
        return {
            "ready": True,
            "prepared": [{"target": "项目解说人"}],
            "prepared_count": 1,
            "remaining_errors": [],
        }

    monkeypatch.setattr(sqlite_store, "SQLiteStore", FakeRunnerStore)
    monkeypatch.setattr(system_voice_setup, "prepare_missing_system_voices", fake_prepare)
    monkeypatch.setattr(audio, "get_task_manager", lambda: FakeManager())
    ctx = SimpleNamespace(
        owner_project_label="alice/demo",
        owner_username="alice",
        project_name="demo",
        output_dir=tmp_path,
        state_dir=tmp_path / "state",
    )

    result = await audio._run_system_voice_setup({"episode": 2}, ctx)

    assert result["ready"] is True
    assert [update["progress"] for update in progress_updates] == [0.05, 1.0]


@pytest.mark.asyncio
async def test_system_voice_setup_hands_a_paused_auto_run_back(monkeypatch, tmp_path):
    """声线补齐后必须把被声线闸门暂停的「本集自动」接回来，否则它永远停着。"""

    from novelvideo.audio import system_voice_setup
    from novelvideo.chat import director_auto
    from novelvideo.task_backend.runners import audio
    from novelvideo import sqlite_store

    handoffs: list[dict] = []

    class FakeCoordinator:
        def resume_after_system_voice_setup_from_thread(self, **kwargs):
            handoffs.append(kwargs)
            return True

    class FakeRunnerStore:
        def __init__(self, *args, **kwargs):  # noqa: ARG002
            pass

        async def initialize(self):
            return None

        async def load_graph_state(self):
            return None

        async def close(self):
            return None

    class FakeManager:
        def update_progress_for_project(self, *args, **kwargs):  # noqa: ARG002
            return None

    async def fake_prepare(**_kwargs):
        return {"ready": True, "prepared": [], "prepared_count": 0, "remaining_errors": []}

    monkeypatch.setattr(sqlite_store, "SQLiteStore", FakeRunnerStore)
    monkeypatch.setattr(system_voice_setup, "prepare_missing_system_voices", fake_prepare)
    monkeypatch.setattr(audio, "get_task_manager", lambda: FakeManager())
    monkeypatch.setattr(director_auto, "coordinator", FakeCoordinator())
    ctx = SimpleNamespace(
        project_id="project-1",
        owner_project_label="alice/demo",
        owner_username="alice",
        project_name="demo",
        output_dir=tmp_path,
        state_dir=tmp_path / "state",
    )

    await audio._run_system_voice_setup({"episode": 3}, ctx)

    assert handoffs == [{"username": "alice", "project_id": "project-1"}]


def _write_gateway_sample(project_dir, voice: str) -> bytes:
    content = f"gateway:{voice}".encode()
    cache = project_dir / "assets" / "system_voices"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / f"gateway-doubao-seed-audio-1-0__{voice}.mp3").write_bytes(content)
    return content


@pytest.mark.asyncio
async def test_prepare_missing_system_voices_blocks_character_without_gender(
    monkeypatch,
    tmp_path,
):
    """性别为空的角色不再静默落到男声池，而是给出可执行的补齐提示。"""

    from novelvideo.audio import system_voice_setup
    from novelvideo.models import NovelCharacter

    narrator_updates: list[dict] = []
    generated: list[str] = []

    class ExplodingEdgeTTSGenerator:
        def __init__(self, voice: str, egress_context=None) -> None:  # noqa: ARG002
            generated.append(voice)

        async def generate(self, *, text, output_path, generate_subtitle):  # noqa: ARG002
            raise AssertionError("不得为未设置性别的角色生成声线")

    _patch_voice_setup_io(monkeypatch, system_voice_setup, narrator_updates)
    monkeypatch.setattr(system_voice_setup, "EdgeTTSGenerator", ExplodingEdgeTTSGenerator)

    store = _FakeStore(tmp_path)
    store.characters = [
        NovelCharacter(name="系统提示音", gender="", age_group="youth", is_main=False)
    ]
    store.beats = [
        {
            "beat_number": 13,
            "audio_type": "dialogue",
            "speaker": "系统提示音",
            "narration_segment": "恭喜您已完成今日任务。",
        }
    ]

    result = await system_voice_setup.prepare_missing_system_voices(
        store=store,
        username="alice",
        project="demo",
        project_dir=tmp_path,
        episode=1,
    )

    assert result["prepared_count"] == 0
    assert result["ready"] is False
    assert not generated
    assert any(
        "系统提示音" in detail and "性别" in detail
        for detail in result["remaining_errors"]
    )


@pytest.mark.asyncio
async def test_prepare_missing_system_voices_puts_actionable_reasons_first(
    monkeypatch,
    tmp_path,
):
    """可操作的原因排在最前：任务失败只带 errors[:5]，排后面等于看不见。"""

    from novelvideo.audio import system_voice_setup
    from novelvideo.audio import indextts2_beat_audio_task

    narrator_updates: list[dict] = []

    async def per_beat_errors(**kwargs):  # noqa: ARG001
        return [f"Beat {number} 角色声线缺失：系统提示音" for number in range(1, 9)]

    # 顺序要紧：``_patch_voice_setup_io`` 自己会把同一函数替换成「无报错」，
    # 必须在它之后覆盖，否则逐 beat 报错压根不存在。
    _patch_voice_setup_io(monkeypatch, system_voice_setup, narrator_updates)
    monkeypatch.setattr(
        indextts2_beat_audio_task,
        "collect_indextts2_voice_prereq_errors",
        per_beat_errors,
    )

    store = _FakeStore(tmp_path)
    store.characters = [
        NovelCharacter(name="系统提示音", gender="", age_group="youth", is_main=False)
    ]
    store.beats = [
        {
            "beat_number": number,
            "audio_type": "dialogue",
            "speaker": "系统提示音",
            "narration_segment": "恭喜您已完成今日任务。",
        }
        for number in range(1, 9)
    ] + [
        {
            "beat_number": 20,
            "audio_type": "dialogue",
            "speaker": "查无此人",
            "narration_segment": "“你好。”",
        }
    ]

    result = await system_voice_setup.prepare_missing_system_voices(
        store=store,
        username="alice",
        project="demo",
        project_dir=tmp_path,
        episode=1,
    )

    errors = result["remaining_errors"]
    assert result["ready"] is False
    assert errors[0].startswith("角色「系统提示音」未设置性别")
    assert errors[1] == "未找到说话身份对应角色：查无此人"
    assert errors[2:] == [
        f"Beat {number} 角色声线缺失：系统提示音" for number in range(1, 9)
    ]


@pytest.mark.asyncio
async def test_prepare_missing_system_voices_avoids_duplicate_presets(
    monkeypatch,
    tmp_path,
):
    """同项目内声线去重：已被别的角色占用的预置音色不再复用。"""

    from novelvideo.audio import system_voice_setup
    from novelvideo.audio import indextts2_beat_audio_task
    from novelvideo.models import NovelCharacter
    from novelvideo.freezone import audio_node

    narrator_updates: list[dict] = []
    writer_calls: list[dict] = []

    async def fake_write_newapi_audio_speech(**kwargs):
        writer_calls.append(kwargs)
        path = Path(kwargs["output_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"gateway:{kwargs['voice']}".encode())
        return {"ok": True}

    async def no_remaining_errors(**kwargs):  # noqa: ARG001
        return []

    monkeypatch.setattr(
        indextts2_beat_audio_task,
        "collect_indextts2_voice_prereq_errors",
        no_remaining_errors,
    )
    monkeypatch.setattr(
        system_voice_setup,
        "load_effective_narration_style_for_voice",
        lambda username, project: "third_person",
    )
    monkeypatch.setattr(
        system_voice_setup, "load_narrator_reference_audio", lambda username, project: {"path": ""}
    )
    monkeypatch.setattr(
        system_voice_setup,
        "set_narrator_reference_audio",
        lambda username, project, **kwargs: narrator_updates.append(kwargs),
    )
    monkeypatch.setenv("SYSTEM_VOICE_TTS_MODEL", "doubao-seed-audio-1-0")
    monkeypatch.setattr(
        audio_node, "_write_newapi_audio_speech", fake_write_newapi_audio_speech
    )

    # 许知意 已经占了 xiaohe；刘奶奶 的稳定选择同样是 xiaohe，必须让位。
    claimed_voice = system_voice_setup.GATEWAY_FEMALE_VOICES[0]
    content = _write_gateway_sample(tmp_path, claimed_voice)
    existing = tmp_path / "assets" / "characters" / "许知意" / "voices" / "system_voice.mp3"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(content)

    store = _FakeStore(tmp_path)
    store.characters = [
        NovelCharacter(name="许知意", gender="女", age_group="youth", is_main=True),
        NovelCharacter(name="刘奶奶", gender="女", age_group="elder", is_main=False),
    ]
    store.characters[0].reference_audio_path = (
        "assets/characters/许知意/voices/system_voice.mp3"
    )
    store.characters[0].reference_audio_sha256 = system_voice_setup.voice_content_sha256(
        content
    )
    store.beats = [
        {
            "beat_number": 2,
            "audio_type": "dialogue",
            "speaker": "许知意",
            "narration_segment": "今天是我的生日……",
        },
        {
            "beat_number": 6,
            "audio_type": "dialogue",
            "speaker": "刘奶奶",
            "narration_segment": "孩子，别怕。",
        },
    ]

    result = await system_voice_setup.prepare_missing_system_voices(
        store=store,
        username="alice",
        project="demo",
        project_dir=tmp_path,
        episode=1,
    )

    assert result["ready"] is True
    by_target = {item["target"]: item for item in result["prepared"]}
    assert set(by_target) == {"刘奶奶"}
    assert by_target["刘奶奶"]["voice"] == system_voice_setup.GATEWAY_FEMALE_VOICES[1]
    assert by_target["刘奶奶"]["voice"] != claimed_voice


def _multi_character_store(project_dir: Path) -> "_FakeStore":
    """6 女 2 男：女声池只有 4 条，池耗尽后必然出现「多个角色共用一条预置音色」。"""

    store = _FakeStore(project_dir)
    store.characters = [
        NovelCharacter(name="林夏", gender="女", age_group="youth", is_main=True),
        NovelCharacter(name="苏晴", gender="女", age_group="youth", is_main=False),
        NovelCharacter(name="许知意", gender="女", age_group="youth", is_main=False),
        NovelCharacter(name="白薇", gender="女", age_group="youth", is_main=False),
        NovelCharacter(name="柳月", gender="女", age_group="youth", is_main=False),
        NovelCharacter(name="沈清", gender="女", age_group="youth", is_main=False),
        NovelCharacter(name="陈默", gender="男", age_group="youth", is_main=False),
        NovelCharacter(name="赵岩", gender="男", age_group="youth", is_main=False),
    ]
    store.beats = [
        {
            "beat_number": index,
            "audio_type": "dialogue",
            "speaker": character.name,
            "narration_segment": f"“{character.name}开口了。”",
        }
        for index, character in enumerate(store.characters, start=1)
    ]
    return store


def test_system_voice_concurrency_precedence(monkeypatch, tmp_path):
    """并发旋钮优先级 env > ``runtime/system_voice_setup.json`` > 默认；非法值回落。"""

    from novelvideo.audio import system_voice_setup

    monkeypatch.delenv(system_voice_setup.SYSTEM_VOICE_CONCURRENCY_ENV, raising=False)
    monkeypatch.setenv("NOVELVIDEO_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "system_voice_setup.json").write_text(
        '{"concurrency": 6}', encoding="utf-8"
    )

    assert system_voice_setup.system_voice_concurrency() == 6

    monkeypatch.setenv(system_voice_setup.SYSTEM_VOICE_CONCURRENCY_ENV, "2")
    assert system_voice_setup.system_voice_concurrency() == 2

    # 非法 / 非正值一律回落（热调参文件仍生效）。
    monkeypatch.setenv(system_voice_setup.SYSTEM_VOICE_CONCURRENCY_ENV, "0")
    assert system_voice_setup.system_voice_concurrency() == 6

    monkeypatch.delenv(system_voice_setup.SYSTEM_VOICE_CONCURRENCY_ENV)
    (tmp_path / "system_voice_setup.json").unlink()
    assert (
        system_voice_setup.system_voice_concurrency()
        == system_voice_setup.SYSTEM_VOICE_CONCURRENCY_DEFAULT
    )


@pytest.mark.asyncio
async def test_prepare_missing_system_voices_generates_character_voices_concurrently(
    monkeypatch,
    tmp_path,
):
    """角色声线合成有界并发；同一条预置音色只打一次网关。"""

    from novelvideo.audio import system_voice_setup
    from novelvideo.freezone import audio_node

    narrator_updates: list[dict] = []
    writer_calls: list[dict] = []
    in_flight = 0
    max_in_flight = 0

    async def fake_write_newapi_audio_speech(**kwargs):
        nonlocal in_flight, max_in_flight
        writer_calls.append(kwargs)
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(0.02)
            path = Path(kwargs["output_path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"gateway:{kwargs['voice']}".encode())
        finally:
            in_flight -= 1
        return {"ok": True}

    _patch_voice_setup_io(monkeypatch, system_voice_setup, narrator_updates)
    monkeypatch.setenv("SYSTEM_VOICE_TTS_MODEL", "doubao-seed-audio-1-0")
    monkeypatch.setenv(system_voice_setup.SYSTEM_VOICE_CONCURRENCY_ENV, "3")
    monkeypatch.setattr(
        audio_node, "_write_newapi_audio_speech", fake_write_newapi_audio_speech
    )

    store = _multi_character_store(tmp_path)
    result = await system_voice_setup.prepare_missing_system_voices(
        store=store,
        username="alice",
        project="demo",
        project_dir=tmp_path,
        episode=1,
    )

    assert result["ready"] is True
    assert result["prepared_count"] == len(store.characters)
    # 并发真的发生了（串行只会有 1），且不超过配置的在飞上限。
    assert 2 <= max_in_flight <= 3
    # 池耗尽后多个角色共用同一条音色时，仍然只打一次网关（否则并发会放大成 N 次）。
    requested = [call["voice"] for call in writer_calls]
    assert len(requested) == len(set(requested)) == 6


@pytest.mark.asyncio
async def test_character_voice_assignment_identical_at_any_concurrency(
    monkeypatch,
    tmp_path,
):
    """并发只改吞吐：concurrency=1 与并发跑出的声线分配与顺序逐字一致。"""

    from novelvideo.audio import system_voice_setup
    from novelvideo.freezone import audio_node

    async def fake_write_newapi_audio_speech(**kwargs):
        path = Path(kwargs["output_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"gateway:{kwargs['voice']}".encode())
        return {"ok": True}

    monkeypatch.setenv("SYSTEM_VOICE_TTS_MODEL", "doubao-seed-audio-1-0")
    monkeypatch.setattr(
        audio_node, "_write_newapi_audio_speech", fake_write_newapi_audio_speech
    )

    async def run_once(limit: str, directory: Path) -> dict:
        narrator_updates: list[dict] = []
        _patch_voice_setup_io(monkeypatch, system_voice_setup, narrator_updates)
        monkeypatch.setenv(system_voice_setup.SYSTEM_VOICE_CONCURRENCY_ENV, limit)
        store = _multi_character_store(directory)
        return await system_voice_setup.prepare_missing_system_voices(
            store=store,
            username="alice",
            project="demo",
            project_dir=directory,
            episode=1,
        )

    serial = await run_once("1", tmp_path / "serial")
    parallel = await run_once("4", tmp_path / "parallel")

    assert serial["ready"] is True
    assert serial["prepared"] == parallel["prepared"]


@pytest.mark.asyncio
async def test_prepare_missing_system_voices_raises_when_character_voice_fails(
    monkeypatch,
    tmp_path,
):
    """任一角色声线彻底合成失败就整集失败，不静默退化成「少几个角色声线」。"""

    from novelvideo.audio import system_voice_setup
    from novelvideo.freezone import audio_node
    from novelvideo.generators.tts_generator import TTSResult

    class FailingEdgeTTSGenerator:
        def __init__(self, voice: str, egress_context=None) -> None:  # noqa: ARG002
            self.voice = voice

        async def generate(self, *, text, output_path, generate_subtitle):  # noqa: ARG002
            return TTSResult(success=False, error="edge unavailable")

    async def failing_write_newapi_audio_speech(**kwargs):  # noqa: ARG001
        raise RuntimeError("gateway down")

    _patch_voice_setup_io(monkeypatch, system_voice_setup, [])
    monkeypatch.setenv("SYSTEM_VOICE_TTS_MODEL", "doubao-seed-audio-1-0")
    monkeypatch.setattr(
        system_voice_setup, "EdgeTTSGenerator", FailingEdgeTTSGenerator
    )
    monkeypatch.setattr(
        audio_node, "_write_newapi_audio_speech", failing_write_newapi_audio_speech
    )

    store = _multi_character_store(tmp_path)
    with pytest.raises(RuntimeError):
        await system_voice_setup.prepare_missing_system_voices(
            store=store,
            username="alice",
            project="demo",
            project_dir=tmp_path,
            episode=1,
        )


def test_unmatched_speaker_voice_precedence(monkeypatch, tmp_path):
    """群杂音色优先级 env > ``runtime/system_voice_setup.json`` > 空（硬失败）。"""

    from novelvideo.audio import system_voice_setup

    monkeypatch.delenv(system_voice_setup.UNMATCHED_SPEAKER_VOICE_ENV, raising=False)
    monkeypatch.setenv("NOVELVIDEO_RUNTIME_DIR", str(tmp_path))
    assert system_voice_setup.unmatched_speaker_voice() == ""

    (tmp_path / "system_voice_setup.json").write_text(
        '{"unmatched_speaker_voice": "zh_male_taocheng_uranus_bigtts"}',
        encoding="utf-8",
    )
    assert (
        system_voice_setup.unmatched_speaker_voice() == "zh_male_taocheng_uranus_bigtts"
    )

    monkeypatch.setenv(
        system_voice_setup.UNMATCHED_SPEAKER_VOICE_ENV, "zh_female_vv_uranus_bigtts"
    )
    assert system_voice_setup.unmatched_speaker_voice() == "zh_female_vv_uranus_bigtts"


def _crowd_store(project_dir: Path) -> "_FakeStore":
    """林夏 + 一个对不上任何角色的临时说话人「伙计」。"""

    store = _FakeStore(project_dir)
    store.beats = [
        {
            "beat_number": 1,
            "audio_type": "dialogue",
            "speaker": "林夏",
            "narration_segment": "“我们出发吧。”",
        },
        {
            "beat_number": 2,
            "audio_type": "dialogue",
            "speaker": "伙计",
            "narration_segment": "“客官里面请。”",
        },
    ]
    return store


@pytest.mark.asyncio
async def test_unmatched_speaker_voice_unblocks_episode_and_caches(
    monkeypatch,
    tmp_path,
):
    """配了群杂音色后，对不上角色的说话人不再阻塞整集，只用一条缓存声线兜底。"""

    from novelvideo.audio import system_voice_setup
    from novelvideo.freezone import audio_node
    from novelvideo.seedance2_i2v.voice_clone import resolve_dialogue_reference_audio

    writer_calls: list[dict] = []

    async def fake_write_newapi_audio_speech(**kwargs):
        writer_calls.append(kwargs)
        path = Path(kwargs["output_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"gateway:{kwargs['voice']}".encode())
        return {"ok": True}

    _patch_voice_setup_io(monkeypatch, system_voice_setup, [])
    monkeypatch.setenv("SYSTEM_VOICE_TTS_MODEL", "doubao-seed-audio-1-0")
    monkeypatch.setenv(
        system_voice_setup.UNMATCHED_SPEAKER_VOICE_ENV,
        "zh_male_taocheng_uranus_bigtts",
    )
    monkeypatch.setattr(
        audio_node, "_write_newapi_audio_speech", fake_write_newapi_audio_speech
    )

    store = _crowd_store(tmp_path)
    result = await system_voice_setup.prepare_missing_system_voices(
        store=store,
        username="alice",
        project="demo",
        project_dir=tmp_path,
        episode=1,
    )

    assert result["ready"] is True
    # 群杂不是一个角色：只准备林夏的声线，群杂不写进 prepared。
    assert [item["target"] for item in result["prepared"]] == ["林夏"]
    crowd_voice = "zh_male_taocheng_uranus_bigtts"
    crowd_path = system_voice_setup._system_voice_cache_path(
        tmp_path, crowd_voice, provider="gateway-doubao-seed-audio-1-0"
    )
    assert crowd_path.exists() and crowd_path.stat().st_size > 0
    assert {call["voice"] for call in writer_calls} == {
        result["prepared"][0]["voice"],
        crowd_voice,
    }

    # 下游解析器（音频计划与配音生成共用）拿到的是同一条群杂声线。
    resolved = await resolve_dialogue_reference_audio(store.beats[1], store)
    assert resolved is not None
    assert resolved[0] == crowd_path


@pytest.mark.asyncio
async def test_unmatched_speaker_blocks_when_crowd_voice_unset(monkeypatch, tmp_path):
    """没配群杂音色时保持旧口径：对不上角色的说话人让前置硬失败，且不白打网关。"""

    from novelvideo.audio import system_voice_setup
    from novelvideo.freezone import audio_node

    writer_calls: list[dict] = []

    async def fake_write_newapi_audio_speech(**kwargs):
        writer_calls.append(kwargs)
        path = Path(kwargs["output_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"gateway")
        return {"ok": True}

    _patch_voice_setup_io(monkeypatch, system_voice_setup, [])
    monkeypatch.setenv("SYSTEM_VOICE_TTS_MODEL", "doubao-seed-audio-1-0")
    monkeypatch.delenv(system_voice_setup.UNMATCHED_SPEAKER_VOICE_ENV, raising=False)
    monkeypatch.setenv("NOVELVIDEO_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(
        audio_node, "_write_newapi_audio_speech", fake_write_newapi_audio_speech
    )

    store = _crowd_store(tmp_path)
    store.beats = [store.beats[1]]

    result = await system_voice_setup.prepare_missing_system_voices(
        store=store,
        username="alice",
        project="demo",
        project_dir=tmp_path,
        episode=1,
    )

    assert result["ready"] is False
    assert result["remaining_errors"] == ["未找到说话身份对应角色：伙计"]
    assert writer_calls == []


def test_unmatched_speaker_voice_project_override_precedence(monkeypatch, tmp_path):
    """项目级显式值 > env > 热调参；``""`` = 该项目显式关闭兜底。"""

    from novelvideo.audio import system_voice_setup

    monkeypatch.setenv(
        system_voice_setup.UNMATCHED_SPEAKER_VOICE_ENV,
        "zh_female_vv_uranus_bigtts",
    )
    monkeypatch.setenv("NOVELVIDEO_RUNTIME_DIR", str(tmp_path))

    # 没配（None）= 跟随部署级。
    assert (
        system_voice_setup.unmatched_speaker_voice(None)
        == "zh_female_vv_uranus_bigtts"
    )
    # 项目级写了音色 = 该项目用它，压过部署级。
    assert (
        system_voice_setup.unmatched_speaker_voice("zh_male_taocheng_uranus_bigtts")
        == "zh_male_taocheng_uranus_bigtts"
    )
    # 项目级显式空串 = 该项目关闭兜底（回到硬失败）。
    assert system_voice_setup.unmatched_speaker_voice("") == ""


def test_project_config_unmatched_speaker_voice_semantics():
    """``project_config.json`` 里三种写法：缺键 / 显式关 / 显式音色。"""

    from novelvideo.project_config import project_unmatched_speaker_voice

    assert project_unmatched_speaker_voice({}) is None
    assert project_unmatched_speaker_voice({"unmatched_speaker_voice": ""}) == ""
    assert project_unmatched_speaker_voice({"unmatched_speaker_voice": False}) == ""
    assert project_unmatched_speaker_voice({"unmatched_speaker_voice": None}) == ""
    assert (
        project_unmatched_speaker_voice(
            {"unmatched_speaker_voice": "zh_male_taocheng_uranus_bigtts"}
        )
        == "zh_male_taocheng_uranus_bigtts"
    )


def _write_project_unmatched_voice(state_dir: Path, value) -> None:
    import json

    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "project_config.json").write_text(
        json.dumps({"unmatched_speaker_voice": value}), encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_project_crowd_switch_only_affects_that_project(monkeypatch, tmp_path):
    """群杂兜底的项目级开关：写了键的项目放行，没写的项目维持硬失败。"""

    from novelvideo.audio import system_voice_setup
    from novelvideo.freezone import audio_node
    from novelvideo.seedance2_i2v.voice_clone import resolve_dialogue_reference_audio

    crowd_voice = "zh_male_taocheng_uranus_bigtts"
    writer_calls: list[dict] = []

    async def fake_write_newapi_audio_speech(**kwargs):
        writer_calls.append(kwargs)
        path = Path(kwargs["output_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"gateway:{kwargs['voice']}".encode())
        return {"ok": True}

    _patch_voice_setup_io(monkeypatch, system_voice_setup, [])
    monkeypatch.setenv("SYSTEM_VOICE_TTS_MODEL", "doubao-seed-audio-1-0")
    # 部署级默认关：只有项目级写了音色的项目才会兜底。
    monkeypatch.delenv(system_voice_setup.UNMATCHED_SPEAKER_VOICE_ENV, raising=False)
    monkeypatch.setenv("NOVELVIDEO_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(
        audio_node, "_write_newapi_audio_speech", fake_write_newapi_audio_speech
    )

    on_dir = tmp_path / "on"
    on_store = _crowd_store(on_dir)
    on_store.state_dir = str(on_dir / "state")
    _write_project_unmatched_voice(on_dir / "state", crowd_voice)

    result = await system_voice_setup.prepare_missing_system_voices(
        store=on_store,
        username="alice",
        project="demo",
        project_dir=on_dir,
        episode=1,
    )

    assert result["ready"] is True
    assert [item["target"] for item in result["prepared"]] == ["林夏"]
    crowd_path = system_voice_setup._system_voice_cache_path(
        on_dir, crowd_voice, provider="gateway-doubao-seed-audio-1-0"
    )
    assert crowd_path.exists() and crowd_path.stat().st_size > 0
    resolved = await resolve_dialogue_reference_audio(on_store.beats[1], on_store)
    assert resolved is not None and resolved[0] == crowd_path

    # 另一个项目没写这个键：同一份部署配置下仍然硬失败，不会白打网关。
    off_dir = tmp_path / "off"
    off_store = _crowd_store(off_dir)
    off_store.state_dir = str(off_dir / "state")
    off_store.beats = [off_store.beats[1]]

    before = len(writer_calls)
    blocked = await system_voice_setup.prepare_missing_system_voices(
        store=off_store,
        username="alice",
        project="other",
        project_dir=off_dir,
        episode=1,
    )

    assert blocked["ready"] is False
    assert blocked["remaining_errors"] == ["未找到说话身份对应角色：伙计"]
    assert len(writer_calls) == before


@pytest.mark.asyncio
async def test_project_switch_can_disable_crowd_voice_while_deployment_default_on(
    monkeypatch, tmp_path
):
    """部署级默认开着时，项目级显式关仍然有效（互不影响）。"""

    from novelvideo.audio import system_voice_setup

    _patch_voice_setup_io(monkeypatch, system_voice_setup, [])
    monkeypatch.setenv("SYSTEM_VOICE_TTS_MODEL", "doubao-seed-audio-1-0")
    monkeypatch.setenv(
        system_voice_setup.UNMATCHED_SPEAKER_VOICE_ENV,
        "zh_male_taocheng_uranus_bigtts",
    )

    store = _crowd_store(tmp_path)
    store.state_dir = str(tmp_path / "state")
    _write_project_unmatched_voice(tmp_path / "state", "")
    # 只看「对不上角色」的那条 beat：这个用例要验的是项目级关掉兜底，
    # 不给林夏合成声线，避免走到真实 TTS 通道。
    store.beats = [store.beats[1]]

    result = await system_voice_setup.prepare_missing_system_voices(
        store=store,
        username="alice",
        project="demo",
        project_dir=tmp_path,
        episode=1,
    )

    assert result["ready"] is False
    assert result["remaining_errors"] == ["未找到说话身份对应角色：伙计"]

