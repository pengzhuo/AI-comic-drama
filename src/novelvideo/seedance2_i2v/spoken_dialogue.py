"""Dialogue parsing helpers for Seedance 2.0 voice references."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from novelvideo.seedance2_i2v.voice_clone import (
    SCRIPT_LEADING_NOTE_RE as _LEADING_PERFORMANCE_NOTE_RE,
    SCRIPT_SPEAKER_PREFIX_RE as _SPEAKER_PREFIX_RE,
    dialogue_text,
    normalize_seedance2_audio_type,
    strip_leading_script_marks as _strip_leading_script_marks,
)


@dataclass(frozen=True)
class Seedance2SpokenLine:
    speaker: str
    text: str
    action: str = ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _spoken_source(beat: dict[str, Any]) -> str:
    return _text(
        beat.get("dialogue") or beat.get("narration_segment") or beat.get("narration") or ""
    )


def parse_seedance2_spoken_lines(beat: dict[str, Any]) -> list[Seedance2SpokenLine]:
    """Parse dialogue text into speaker/action/text lines.

    Only parses explicit literal-script lines such as ``角色（动作）：台词``.
    Ambiguous beat-level prose is left intact for the AI prompt composer.
    """

    if normalize_seedance2_audio_type(beat) != "dialogue":
        return []

    raw = _spoken_source(beat)
    if not raw:
        return []

    matches = list(_SPEAKER_PREFIX_RE.finditer(raw))
    lines: list[Seedance2SpokenLine] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        text = raw[start:end].strip(" \t\r\n，,。；;")
        if not text:
            continue
        lines.append(
            Seedance2SpokenLine(
                speaker=_text(match.group("speaker")),
                action=_text(match.group("action") or match.group("action_ascii")),
                text=text,
            )
        )
    if lines:
        return lines

    return []


def required_seedance2_dialogue_texts(beat: dict[str, Any]) -> list[str]:
    """Return the minimum verbatim dialogue required in the final prompt.

    The AI composer still receives the untouched source and decides how to express
    performance directions. This helper only unwraps one explicit leading
    parenthetical note so validation does not confuse it with spoken dialogue.
    """

    lines = parse_seedance2_spoken_lines(beat)
    if lines:
        return [line.text for line in lines if line.text]

    text = dialogue_text(beat).strip(" \t\r\n，,。；;")
    if not text:
        return []
    leading_note = _LEADING_PERFORMANCE_NOTE_RE.match(text)
    if leading_note:
        spoken_text = text[leading_note.end() :].strip(" \t\r\n，,。；;")
        if spoken_text:
            text = spoken_text
    return [text]


_SUBTITLE_AUDIO_TYPES = frozenset({"dialogue", "narration"})


def beat_subtitle_text(beat: dict[str, Any]) -> str:
    """Return the text a composed film burns in as this beat's subtitle.

    字幕只显示真正念出来的内容：去掉 ``许知意（os）：`` 这类说话人标签和
    ``（神色坚定，喘息未平便开口）`` 这类表演提示，句末标点保留，多行文本逐行
    清洗；没有语音的 beat（silence/音效等）不出字幕，免得把画面描述当成台词。
    """

    if normalize_seedance2_audio_type(beat) not in _SUBTITLE_AUDIO_TYPES:
        return ""
    raw = _spoken_source(beat)
    if not raw:
        return ""
    return "\n".join(
        cleaned
        for cleaned in (_strip_leading_script_marks(line) for line in raw.splitlines())
        if cleaned
    )


def unique_seedance2_dialogue_speakers(beat: dict[str, Any]) -> list[str]:
    """Return dialogue speakers in first-spoken order."""

    seen: set[str] = set()
    speakers: list[str] = []
    for line in parse_seedance2_spoken_lines(beat):
        if line.speaker in seen:
            continue
        seen.add(line.speaker)
        speakers.append(line.speaker)
    if not speakers:
        speaker = _text(beat.get("speaker"))
        if speaker:
            speakers.append(speaker)
    return speakers


def speaker_display_name(value: str) -> str:
    text = _text(value)
    if "_" in text:
        return text.split("_", 1)[0]
    return text
