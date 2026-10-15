"""Prepare opt-in system-generated reference voices for the mainline agent."""

from __future__ import annotations

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


def gateway_voice_model() -> str:
    """网关 TTS 模型名；为空表示系统声线只走 EdgeTTS。"""

    return str(os.environ.get("SYSTEM_VOICE_TTS_MODEL", "") or "").strip()


def _env_voice(name: str, default: str) -> str:
    return str(os.environ.get(name, "") or "").strip() or default


def _env_voice_pool(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    voices = tuple(item.strip() for item in raw.split(",") if item.strip())
    return voices or default


def _is_female(character) -> bool:
    gender = str(getattr(character, "gender", "") or "").strip().lower()
    return "女" in gender or gender in {"female", "woman", "girl"}


def _is_child(character) -> bool:
    return str(getattr(character, "age_group", "") or "").strip().lower() == "child"


def _stable_voice(pool: tuple[str, ...], character) -> str:
    stable_index = sum(ord(char) for char in str(getattr(character, "name", "") or ""))
    return pool[stable_index % len(pool)]


def _edge_voice_for_character(character) -> str:
    if _is_female(character):
        if _is_child(character):
            return CHILD_VOICE
        return _stable_voice(FEMALE_VOICES, character)
    return _stable_voice(MALE_VOICES, character)


def _gateway_voice_for_character(character) -> str:
    if _is_female(character):
        if _is_child(character):
            return _env_voice("SYSTEM_VOICE_TTS_CHILD_VOICE", GATEWAY_CHILD_VOICE)
        return _stable_voice(
            _env_voice_pool("SYSTEM_VOICE_TTS_FEMALE_VOICES", GATEWAY_FEMALE_VOICES),
            character,
        )
    return _stable_voice(
        _env_voice_pool("SYSTEM_VOICE_TTS_MALE_VOICES", GATEWAY_MALE_VOICES),
        character,
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


def _character_voice_candidates(character) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    if gateway_voice_model():
        candidates.append(("gateway", _gateway_voice_for_character(character)))
    candidates.append(("edge", _edge_voice_for_character(character)))
    return candidates


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
    return next(
        (character for character in characters if speaker.startswith(character.name)),
        None,
    )


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

    for character_name, character in character_targets.items():
        content, provider, voice = await _system_voice_content(
            project_dir=root,
            candidates=_character_voice_candidates(character),
            cache=content_cache,
        )
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
    for speaker in sorted(unresolved_speakers):
        detail = f"未找到说话身份对应角色：{speaker}"
        if detail not in remaining:
            remaining.append(detail)
    return {
        "prepared": prepared,
        "prepared_count": len(prepared),
        "remaining_errors": remaining,
        "ready": not remaining,
    }
