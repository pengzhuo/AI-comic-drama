"""Prepare opt-in system-generated reference voices for the mainline agent."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from novelvideo.egress_context import ambient_egress_context
from novelvideo.generators.tts_generator import EdgeTTSGenerator
from novelvideo.project_config import (
    load_effective_narration_style_for_voice,
    load_narrator_reference_audio,
    set_narrator_reference_audio,
)
from novelvideo.seedance2_i2v.character_voice_storage import (
    DEFAULT_SLOT,
    persist_character_voice_file,
    project_relative_path,
    utc_now_iso,
    voice_content_sha256,
)
from novelvideo.seedance2_i2v.speaker_matching import select_character_for_speaker
from novelvideo.seedance2_i2v.voice_clone import (
    dialogue_text,
    narration_beat_text,
    normalize_seedance2_audio_type,
    resolve_dialogue_reference_audio,
    resolve_narrator_source,
)

SYSTEM_VOICE_SAMPLE_TEXT = "你好，这是系统生成的参考声线，用于保持整部作品的声音稳定自然。"
NARRATOR_VOICE = "zh-CN-XiaoxuanNeural"
FEMALE_VOICES = (
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-XiaoyiNeural",
    "zh-CN-XiaomoNeural",
)
MALE_VOICES = (
    "zh-CN-YunxiNeural",
    "zh-CN-YunjianNeural",
    "zh-CN-YunyangNeural",
)
CHILD_VOICE = "zh-CN-XiaoshuangNeural"

# 网关 TTS 路径：``SYSTEM_VOICE_TTS_MODEL`` 配置了部署自己的网关模型时，系统声线
# 走 ``/v1/audio/speech`` 的 preset 模式（豆包预置音色），而不是 EdgeTTS。EdgeTTS
# 仍是默认路径，也是网关失败时的兜底。
GATEWAY_NARRATOR_VOICE = "zh_female_sophie_uranus_bigtts"
GATEWAY_CHILD_VOICE = "zh_female_qingxinnvsheng_uranus_bigtts"
GATEWAY_FEMALE_VOICES = (
    "zh_female_xiaohe_uranus_bigtts",
    "zh_female_tianmeitaozi_uranus_bigtts",
    "zh_female_vv_uranus_bigtts",
    "zh_female_cancan_uranus_bigtts",
)
GATEWAY_MALE_VOICES = (
    "zh_male_taocheng_uranus_bigtts",
    "zh_male_m191_uranus_bigtts",
    "zh_male_liufei_uranus_bigtts",
    "zh_male_ruyayichen_uranus_bigtts",
)

#: 角色声线合成的有界并发。长剧一集 27+ 个角色，每角色一次网关往返 10~70s（偶发
#: 单次 5 分钟以上），串行实测 ``system_voice_setup`` span 549s。热调参
#: ``runtime/system_voice_setup.json`` 的 ``concurrency``，env
#: ``SYSTEM_VOICE_TTS_CONCURRENCY`` 优先级更高；设 1 = 逐角色串行（旧行为）。
SYSTEM_VOICE_STAGE = "system_voice_setup"
SYSTEM_VOICE_CONCURRENCY_ENV = "SYSTEM_VOICE_TTS_CONCURRENCY"
SYSTEM_VOICE_CONCURRENCY_DEFAULT = 4

#: 群杂兜底。dialogue beat 的 ``speaker`` 对不上任何角色时（「路人」「伙计」
#: 「众弟子」这类脚本里的临时说话人），默认硬失败、提示用户去补角色；把它设成一条
#: 预设音色后，这些说话人统一用这条「群杂音色」配音，不再阻塞整集。
#: 取值为网关 preset 音色 id，空字符串 = 保持旧口径（硬失败）。
UNMATCHED_SPEAKER_VOICE_ENV = "SYSTEM_VOICE_UNMATCHED_VOICE"

#: 群杂音色在合成批次里的占位目标名；它只落 ``assets/system_voices`` 缓存，
#: 不会出现在 ``prepared`` 里（群杂不是一个角色）。
UNMATCHED_SPEAKER_TARGET = "__unmatched_speaker__"


def gateway_voice_model() -> str:
    """网关 TTS 模型名；为空表示系统声线只走 EdgeTTS。"""

    return str(os.environ.get("SYSTEM_VOICE_TTS_MODEL", "") or "").strip()


def system_voice_concurrency() -> int:
    """角色声线合成的在飞上限：env > ``runtime/system_voice_setup.json`` > 常量。"""

    from novelvideo.shared.stage_runtime import stage_int

    return max(
        1,
        stage_int(
            SYSTEM_VOICE_STAGE,
            SYSTEM_VOICE_CONCURRENCY_ENV,
            "concurrency",
            SYSTEM_VOICE_CONCURRENCY_DEFAULT,
        ),
    )


def unmatched_speaker_voice(project_override: str | None = None) -> str:
    """群杂音色 id：项目级显式值 > env > ``runtime/system_voice_setup.json`` > 空。

    ``project_override`` 非 ``None`` 时它就是最终决定（``""`` = 该项目显式关闭兜底，
    回到「对不上角色就硬失败」的旧口径）；``None`` 表示该项目没写这个键，
    跟随部署级默认。
    """

    if project_override is not None:
        return str(project_override).strip()
    env = str(os.environ.get(UNMATCHED_SPEAKER_VOICE_ENV, "") or "").strip()
    if env:
        return env
    from novelvideo.shared.stage_runtime import load_runtime_tuning

    tuned = load_runtime_tuning(SYSTEM_VOICE_STAGE).get("unmatched_speaker_voice")
    return str(tuned or "").strip()


def unmatched_speaker_voice_for_store(store: object | None = None) -> str:
    """按项目解析群杂音色：项目配置 > 部署级（env / 热调参）。

    读取路径统一走 ``store.state_dir/project_config.json``（v2.0 项目配置就在
    state 目录下）；store 没有 ``state_dir``（测试桩、旧对象）时退回部署级默认。
    """

    state_dir = str(getattr(store, "state_dir", "") or "").strip()
    override: str | None = None
    if state_dir:
        from novelvideo.project_config import unmatched_speaker_voice_override_from_state_dir

        try:
            override = unmatched_speaker_voice_override_from_state_dir(state_dir)
        except Exception:  # noqa: BLE001 - 配置读不到就按「没配」处理
            override = None
    return unmatched_speaker_voice(override)


def _unmatched_speaker_voice_candidates(voice: str) -> list[tuple[str, str]]:
    """群杂音色的合成候选；未配置群杂音色时返回空列表。"""

    voice = str(voice or "").strip()
    if not voice:
        return []
    if gateway_voice_model():
        return [("gateway", voice)]
    return [("edge", voice)]


def unmatched_speaker_voice_cache_path(
    project_dir: str | Path, *, voice: str | None = None
) -> Path | None:
    """已合成好的群杂音色文件；未配置或还没合成时返回 ``None``。

    下游 :func:`resolve_dialogue_reference_audio` 用它给「对不上角色」的说话人
    兜底，因此这里只认已经存在且非空的缓存文件，不触发任何合成。

    ``voice`` 省略时按部署级口径解析；调用方已经按项目解析过就把它传进来，
    避免同一处两套口径（项目级开关与文件解析必须一致）。
    """

    voice = str(voice if voice is not None else unmatched_speaker_voice()).strip()
    if not voice:
        return None
    root = Path(project_dir)
    candidates: list[Path] = []
    model = gateway_voice_model()
    if model:
        candidates.append(
            _system_voice_cache_path(root, voice, provider=f"gateway-{model}")
        )
    candidates.append(_system_voice_cache_path(root, voice, provider="edge"))
    safe = _safe_voice_filename(voice)
    cache_dir = root / "assets" / "system_voices"
    candidates.extend(
        path for path in sorted(cache_dir.glob(f"*__{safe}.mp3")) if path not in candidates
    )
    for path in candidates:
        try:
            if path.exists() and path.stat().st_size > 0:
                return path
        except OSError:  # pragma: no cover - 读不到按未命中处理
            continue
    return None


def _env_voice(name: str, default: str) -> str:
    return str(os.environ.get(name, "") or "").strip() or default


def _env_voice_pool(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    voices = tuple(item.strip() for item in raw.split(",") if item.strip())
    return voices or default


_FEMALE_GENDER_MARKERS = ("女", "female", "woman", "girl")
_MALE_GENDER_MARKERS = ("男", "male", "man", "boy")


def character_gender(character) -> str:
    """Normalized gender: ``female``, ``male``, or ``""`` when the script is unclear.

    An empty gender used to silently fall through to the male pool, which is how
    female characters ended up speaking with a male preset. Callers must now
    decide explicitly instead of guessing.
    """

    raw = str(getattr(character, "gender", "") or "").strip().lower().replace(" ", "")
    if not raw:
        return ""
    if any(marker in raw for marker in _FEMALE_GENDER_MARKERS):
        return "female"
    if any(marker in raw for marker in _MALE_GENDER_MARKERS):
        return "male"
    return ""


def gender_missing_error(character) -> str | None:
    """Actionable message when a character cannot be matched to a preset voice."""

    if character_gender(character):
        return None
    name = str(getattr(character, "name", "") or "").strip() or "未命名角色"
    return (
        f"角色「{name}」未设置性别，无法匹配系统声线："
        "请在角色面板补充性别，或为该角色上传/录制声线"
    )


def _is_female(character) -> bool:
    return character_gender(character) == "female"


def _is_child(character) -> bool:
    return str(getattr(character, "age_group", "") or "").strip().lower() == "child"


def _stable_voice(
    pool: tuple[str, ...], character, *, claimed: set[str] | None = None
) -> str:
    """Deterministic pool pick, rotating past voices another character uses."""

    stable_index = sum(ord(char) for char in str(getattr(character, "name", "") or ""))
    if claimed:
        for offset in range(len(pool)):
            voice = pool[(stable_index + offset) % len(pool)]
            if voice not in claimed:
                return voice
    return pool[stable_index % len(pool)]


def _edge_voice_for_character(character, *, claimed: set[str] | None = None) -> str:
    if _is_female(character):
        if _is_child(character):
            return CHILD_VOICE
        return _stable_voice(FEMALE_VOICES, character, claimed=claimed)
    return _stable_voice(MALE_VOICES, character, claimed=claimed)


def _gateway_voice_for_character(character, *, claimed: set[str] | None = None) -> str:
    if _is_female(character):
        if _is_child(character):
            return _env_voice("SYSTEM_VOICE_TTS_CHILD_VOICE", GATEWAY_CHILD_VOICE)
        return _stable_voice(
            _env_voice_pool("SYSTEM_VOICE_TTS_FEMALE_VOICES", GATEWAY_FEMALE_VOICES),
            character,
            claimed=claimed,
        )
    return _stable_voice(
        _env_voice_pool("SYSTEM_VOICE_TTS_MALE_VOICES", GATEWAY_MALE_VOICES),
        character,
        claimed=claimed,
    )


def _narrator_voice_candidates() -> list[tuple[str, str]]:
    """(通道, 声线) 候选，按优先级排列。"""

    candidates: list[tuple[str, str]] = []
    if gateway_voice_model():
        candidates.append(
            ("gateway", _env_voice("SYSTEM_VOICE_TTS_NARRATOR", GATEWAY_NARRATOR_VOICE))
        )
    candidates.append(("edge", NARRATOR_VOICE))
    return candidates


def _character_voice_candidates(
    character, *, claimed: set[str] | None = None
) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    if gateway_voice_model():
        candidates.append(
            ("gateway", _gateway_voice_for_character(character, claimed=claimed))
        )
    candidates.append(("edge", _edge_voice_for_character(character, claimed=claimed)))
    return candidates


def _cached_system_voice_hashes(project_dir: Path) -> dict[str, str]:
    """Map ``sha256 -> preset voice`` for the samples already cached in a project."""

    cache_dir = project_dir / "assets" / "system_voices"
    if not cache_dir.is_dir():
        return {}
    hashes: dict[str, str] = {}
    for path in sorted(cache_dir.glob("*.mp3")):
        stem = path.stem
        if "__" not in stem:
            continue
        voice = stem.split("__", 1)[1]
        try:
            content = path.read_bytes()
        except OSError:
            continue
        if content:
            hashes[voice_content_sha256(content)] = voice
    return hashes


def _claimed_system_voices(*, project_dir: Path, characters: list) -> set[str]:
    """Preset voices already taken by a character in this project.

    Characters keep only the hash of their reference sample, so the cached
    system-voice samples are what maps that hash back to the preset it came
    from. A character with a custom (捏塘) sample simply never matches.
    """

    known = _cached_system_voice_hashes(project_dir)
    if not known:
        return set()
    claimed: set[str] = set()
    for character in characters:
        sha256 = str(getattr(character, "reference_audio_sha256", "") or "").strip()
        if not sha256:
            continue
        voice = known.get(sha256)
        if voice:
            claimed.add(voice)
    return claimed


def _safe_voice_filename(voice: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", voice).strip("._") or "system_voice"


def _system_voice_cache_path(project_dir: Path, voice: str, *, provider: str) -> Path:
    return (
        project_dir
        / "assets"
        / "system_voices"
        / f"{_safe_voice_filename(provider)}__{_safe_voice_filename(voice)}.mp3"
    )


async def _edge_voice_content(*, project_dir: Path, voice: str) -> bytes:
    output_path = _system_voice_cache_path(project_dir, voice, provider="edge")
    if not output_path.exists() or output_path.stat().st_size <= 0:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        result = await EdgeTTSGenerator(
            voice=voice,
            egress_context=ambient_egress_context(),
        ).generate(
            text=SYSTEM_VOICE_SAMPLE_TEXT,
            output_path=str(output_path),
            generate_subtitle=False,
        )
        if not result.success or not output_path.exists():
            raise RuntimeError(result.error or f"系统声线生成失败：{voice}")
    content = output_path.read_bytes()
    if not content:
        raise RuntimeError(f"系统声线生成结果为空：{voice}")
    return content


async def _gateway_voice_content(*, project_dir: Path, model: str, voice: str) -> bytes:
    """用部署自己的网关 preset 通道合成参考声线。"""

    from novelvideo.freezone.audio_node import _write_newapi_audio_speech

    output_path = _system_voice_cache_path(
        project_dir, voice, provider=f"gateway-{model}"
    )
    if not output_path.exists() or output_path.stat().st_size <= 0:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        await _write_newapi_audio_speech(
            output_path=output_path,
            model=model,
            input_text=SYSTEM_VOICE_SAMPLE_TEXT,
            response_format="mp3",
            voice=voice,
            metadata={"speech_mode": "preset"},
            business_task_id=f"system-voice:{voice}",
        )
    content = output_path.read_bytes()
    if not content:
        raise RuntimeError(f"系统声线生成结果为空：{voice}（{model}）")
    return content


_PROVIDER_LABELS = {"gateway": "网关 TTS", "edge": "EdgeTTS"}


async def _system_voice_content(
    *,
    project_dir: Path,
    candidates: list[tuple[str, str]],
    cache: dict[str, tuple[bytes, str, str]],
) -> tuple[bytes, str, str]:
    """按候选顺序合成参考声线，返回 (音频内容, 生效通道, 实际声线)。"""

    cache_key = candidates[0][1]
    cached = cache.get(cache_key)
    if cached:
        return cached
    model = gateway_voice_model()
    errors: list[str] = []
    for provider, voice in candidates:
        try:
            if provider == "gateway":
                content = await _gateway_voice_content(
                    project_dir=project_dir, model=model, voice=voice
                )
            else:
                content = await _edge_voice_content(project_dir=project_dir, voice=voice)
        except Exception as error:  # noqa: BLE001 - 回落到下一条候选
            errors.append(f"{_PROVIDER_LABELS[provider]}声线生成失败（{voice}）：{error}")
            continue
        cache[cache_key] = (content, provider, voice)
        return cache[cache_key]
    raise RuntimeError("；".join(errors) or f"系统声线生成失败：{cache_key}")


def _matching_character(speaker: str, characters: list):
    return select_character_for_speaker(speaker, characters)


async def prepare_missing_system_voices(
    *,
    store,
    username: str,
    project: str,
    project_dir: str | Path,
    episode: int,
) -> dict:
    """Fill only missing narrator/character voice references with system presets."""

    root = Path(project_dir)
    beats = list(await store.get_beats_as_dicts(episode))
    characters = list(await store.list_characters())
    character_targets: dict[str, object] = {}
    needs_narrator = False
    unresolved_speakers: set[str] = set()
    needs_unmatched_voice = False
    # 项目级开关只影响本项目：没写键就跟随部署级（env / 热调参）。
    crowd_voice = unmatched_speaker_voice_for_store(store)

    for beat in beats:
        audio_type = normalize_seedance2_audio_type(beat)
        if audio_type == "narration" and narration_beat_text(beat):
            needs_narrator = True
        elif audio_type == "dialogue" and dialogue_text(beat):
            if await resolve_dialogue_reference_audio(beat, store) is not None:
                continue
            speaker = str(beat.get("speaker") or "").strip()
            character = _matching_character(speaker, characters)
            if character is None:
                # 配了群杂音色就统一兜底（不再把「路人」「伙计」这类临时说话人
                # 当成必须补角色的阻塞项）；没配就维持硬失败口径。
                if crowd_voice:
                    needs_unmatched_voice = True
                else:
                    unresolved_speakers.add(speaker or "未指定说话身份")
            else:
                character_targets[character.name] = character

    narration_style = load_effective_narration_style_for_voice(username, project)
    if needs_narrator:
        stored = load_narrator_reference_audio(username, project)
        resolution = resolve_narrator_source(
            store=store,
            narration_style=narration_style,
            project_narrator_stored_path=stored.get("path", ""),
            characters=characters,
        )
        if resolution.audio_path is None and narration_style == "first_person":
            protagonist = next((character for character in characters if character.is_main), None)
            if protagonist is not None:
                character_targets[protagonist.name] = protagonist

    prepared: list[dict[str, str]] = []
    content_cache: dict[str, tuple[bytes, str, str]] = {}
    claimed_voices = _claimed_system_voices(project_dir=root, characters=characters)
    gender_missing: list[str] = []

    if needs_narrator and narration_style != "first_person":
        stored = load_narrator_reference_audio(username, project)
        resolution = resolve_narrator_source(
            store=store,
            narration_style=narration_style,
            project_narrator_stored_path=stored.get("path", ""),
            characters=characters,
        )
        if resolution.audio_path is None:
            content, provider, narrator_voice = await _system_voice_content(
                project_dir=root,
                candidates=_narrator_voice_candidates(),
                cache=content_cache,
            )
            target = root / "assets" / "narrator" / "voice.mp3"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            set_narrator_reference_audio(
                username,
                project,
                relative_path=project_relative_path(root, target),
                sha256=voice_content_sha256(content),
            )
            prepared.append(
                {
                    "target": "项目解说人",
                    "voice": narrator_voice,
                    "slot": "narrator",
                    "provider": provider,
                }
            )

    # 角色声线合成是这一阶段最长的串行热点（实测 10 次网关调用串行 535s，其中一次
    # 单请求 5m26s）。声线选择必须按角色顺序串行——同项目内去重靠 ``claimed`` 逐个
    # 递进；但合成本身互不依赖，于是先串行定好每个角色要生成什么，再按有界并发合成，
    # 最后按原顺序落库，``prepared`` 与去重结果都与逐角色串行一致。
    generation_jobs: list[tuple[str, list[tuple[str, str]]]] = []
    for character_name, character in character_targets.items():
        gender_error = gender_missing_error(character)
        if gender_error:
            gender_missing.append(gender_error)
            continue
        candidates = _character_voice_candidates(character, claimed=claimed_voices)
        # 串行版本里 ``claimed`` 收的是本次实际生效的声线；预设通道成功时正是这里的
        # 主候选。先按主候选预约，保证同性别角色不撞预置音色。
        if candidates:
            claimed_voices.add(candidates[0][1])
        generation_jobs.append((character_name, candidates))

    # 声线池只有个位数，池耗尽后多个角色会落到同一条预置音色。同一主声线加锁，让
    # 后到的角色直接命中 ``assets/system_voices`` 缓存，不重复打网关（串行版靠文件
    # 缓存天然去重，并发下不加锁会变成 N 次重复请求）。
    voice_locks: dict[str, asyncio.Lock] = {}

    async def _generate_character_voice(
        job: tuple[str, list[tuple[str, str]]],
    ) -> tuple[bytes, str, str]:
        _character_name, candidates = job
        lock = voice_locks.setdefault(candidates[0][1], asyncio.Lock())
        async with lock:
            return await _system_voice_content(
                project_dir=root,
                candidates=candidates,
                cache=content_cache,
            )

    failures: list[BaseException] = []

    def _note_generation_failure(
        _job: tuple[str, list[tuple[str, str]]], exc: BaseException
    ) -> None:
        failures.append(exc)

    from novelvideo.utils.bounded_concurrency import map_bounded

    # 群杂音色和角色声线走同一条合成通道、同一个并发池；它只占批次末尾一位，
    # 后面按 `generation_jobs`（批次前缀）合并，所以不会写进 ``prepared``。
    batch_jobs: list[tuple[str, list[tuple[str, str]]]] = list(generation_jobs)
    if needs_unmatched_voice:
        batch_jobs.append(
            (UNMATCHED_SPEAKER_TARGET, _unmatched_speaker_voice_candidates(crowd_voice))
        )

    generated = await map_bounded(
        batch_jobs,
        _generate_character_voice,
        limit=system_voice_concurrency(),
        # 音频不是 LLM 调用，不占进程级 LLM 在飞闸门。
        gate=False,
        on_error=_note_generation_failure,
    )
    if failures:
        # 与逐角色串行同口径：任一角色声线合成失败就整集失败（让阶段重试），
        # 不静默退化成「少几个角色声线」。
        raise failures[0]

    # 只落库角色（``generation_jobs`` 是批次前缀，群杂占位目标不会进来）。
    for (character_name, _candidates), outcome in zip(generation_jobs, generated):
        if outcome is None:  # pragma: no cover - 失败已在上面上抛
            raise RuntimeError(f"角色「{character_name}」系统声线合成为空结果")
        content, provider, voice = outcome
        rel_path, sha256, updated_at = persist_character_voice_file(
            project_dir=root,
            character_name=character_name,
            slot=DEFAULT_SLOT,
            filename="system_voice.mp3",
            content=content,
        )
        await store.update_character(
            character_name,
            reference_audio_path=rel_path,
            reference_audio_sha256=sha256,
            reference_audio_updated_at=updated_at or utc_now_iso(),
        )
        prepared.append(
            {
                "target": character_name,
                "voice": voice,
                "slot": DEFAULT_SLOT,
                "provider": provider,
            }
        )

    from novelvideo.audio.indextts2_beat_audio_task import (
        collect_indextts2_voice_prereq_errors,
    )

    remaining = await collect_indextts2_voice_prereq_errors(
        store=store,
        username=username,
        project=project,
        episode=episode,
        beat_numbers=None,
        mode="sync_changed",
    )
    # 「怎么修」的原因必须排在逐 beat 的重复报错前面：任务失败只带 errors[:5]，
    # 排在第六条就等于用户永远看不到补性别/认说话人这条真正的出口。
    actionable: list[str] = []
    for detail in gender_missing:
        if detail not in actionable:
            actionable.append(detail)
    for speaker in sorted(unresolved_speakers):
        detail = f"未找到说话身份对应角色：{speaker}"
        if detail not in actionable:
            actionable.append(detail)
    per_beat = [detail for detail in remaining if detail not in actionable]
    ordered = actionable + per_beat
    return {
        "prepared": prepared,
        "prepared_count": len(prepared),
        "remaining_errors": ordered,
        "ready": not ordered,
    }
