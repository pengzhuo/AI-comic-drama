"""发声 beat 的参考声线预检。

音色与口型一致，靠的是两条同时成立：

1. 同一个 beat 的台词用**同一段参考声线**去念（``reference_audios``）；
2. 画面由模型带着原生音轨一起生成（``generate_audio``），口型与声音同源。

第 2 条不成立时声音由 TTS 音轨补，这里不管。第 1 条不成立则是**静默失效**：请求照
发、成片照出，只是上游自己编了一个音色，跨镜音色和口型都失去依据，事后无法补救。
所以在发请求前先判一次，判不过就不发。
"""

from __future__ import annotations

from typing import Any

#: 需要真实人声的音频类型：这类 beat 缺声线参考，音色/口型必然不可控。
SPEECH_AUDIO_TYPES = frozenset({"dialogue", "narration"})


def beat_expects_speech(beat: dict[str, Any]) -> bool:
    """这一 beat 是否必须有真实人声（台词或解说）。

    只有「有可念文本 + 音频类型是台词/解说」的 beat 才要求声线参考：动作/静音 beat
    本来就该没有对白，不能因为音轨安静或没有声线就判不通过。
    """

    from novelvideo.seedance2_i2v.spoken_dialogue import beat_subtitle_text
    from novelvideo.seedance2_i2v.voice_clone import normalize_seedance2_audio_type

    if not str(beat_subtitle_text(beat) or "").strip():
        return False
    return normalize_seedance2_audio_type(beat) in SPEECH_AUDIO_TYPES


def _generate_audio_enabled(seedance2_config: Any) -> bool:
    """请求是否要求模型原生出声；配置缺失/坏掉时按「要出声」保守处理。"""

    config = seedance2_config
    if isinstance(config, str):
        raw = config.strip()
        if not raw or raw in {"{}", "null", "None"}:
            return True
        from novelvideo.seedance2_i2v.models import parse_seedance2_config

        try:
            config = parse_seedance2_config(raw)
        except Exception:  # noqa: BLE001 - 解析失败交给后续构建器报错，这里不拦
            return True
    return bool(getattr(config, "generate_audio", True))


def speech_reference_audio_gap(
    *,
    beat: dict[str, Any],
    beat_num: int,
    references: list[Any],
    seedance2_config: Any = None,
) -> str:
    """说话 beat 却没有参考声线时返回错误文案（否则空串）。

    ``references`` 是本 beat 真正要发给模型的素材引用列表（``ShotReference``）；
    只要其中有 ``type == "audio"`` 的一项，就说明声线已锁定。
    """

    if not beat_expects_speech(beat):
        return ""
    if not _generate_audio_enabled(seedance2_config):
        # 不要求模型出声的 beat，声音由 TTS 音轨补，不在这条保证范围内。
        return ""
    for item in references or []:
        if str(getattr(item, "type", "") or "").strip().lower() == "audio":
            return ""
    speaker = str(beat.get("speaker") or "").strip() or "（未标注说话人）"
    return (
        f"Beat {int(beat_num or 0)} 有台词（说话人 {speaker}），但请求里没有任何参考音频"
        "（reference_audios）：上游会自行编造音色，跨镜音色与口型都无法保证。"
        "请为该身份配置 reference_audio_path（或项目解说音色）后重试。"
    )


__all__ = [
    "SPEECH_AUDIO_TYPES",
    "beat_expects_speech",
    "speech_reference_audio_gap",
]
