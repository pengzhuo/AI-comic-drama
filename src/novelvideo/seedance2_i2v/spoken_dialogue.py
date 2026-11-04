"""Dialogue parsing helpers for Seedance 2.0 voice references."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from novelvideo.seedance2_i2v.voice_clone import (
    SCRIPT_LEADING_NOTE_RE as _LEADING_PERFORMANCE_NOTE_RE,
    SCRIPT_SPEAKER_PREFIX_RE as _SPEAKER_PREFIX_RE,
    dialogue_text,
    looks_like_speaker_label,
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


#: 句末标点后紧跟空白 = 脚本把下一句（多半是动作旁白）并进同一行的分隔。
_LAST_LINE_PROSE_SPLIT_RE = re.compile(r"[。！？!?]\s+")


def _drop_trailing_prose(text: str) -> str:
    """Keep only the first spoken sentence of the last line's captured text.

    台词行之间由说话人前缀分隔；句末标点后**紧跟空白**再接一段没有说话人前缀的
    文字，是脚本把动作描述并进同一行的写法（``…。 苏念愣住，睁圆眼睛。``）。校验
    只要求「至少包含台词」，所以这里只保留第一句——少 require 不会误伤，多
    require 却会把整条 beat 拦死。
    """

    match = _LAST_LINE_PROSE_SPLIT_RE.search(text)
    if match is None:
        return text
    return text[: match.start() + 1].strip(" \t\r\n，,。；;")


def parse_seedance2_spoken_lines(beat: dict[str, Any]) -> list[Seedance2SpokenLine]:
    """Parse dialogue text into speaker/action/text lines.

    Only parses explicit literal-script lines such as ``角色（动作）：台词``.
    Ambiguous beat-level prose is left intact for the AI prompt composer.

    ``角色（动作）：`` 带括注的就是脚本台词形态，直接认；裸 ``名前：`` 才过
    ``looks_like_speaker_label``：散文里的冒号（``认定书上写着：违规操作…``）不是
    说话人，误判的代价是给一个不存在的角色去要参考声线，整条 beat 直接卡在发请求
    之前。字幕剥离走的就是这条规则，两处口径必须一致。
    """

    if normalize_seedance2_audio_type(beat) != "dialogue":
        return []

    raw = _spoken_source(beat)
    if not raw:
        return []

    matches = [
        match
        for match in _SPEAKER_PREFIX_RE.finditer(raw)
        if match.group("action")
        or match.group("action_ascii")
        or looks_like_speaker_label(match.group("speaker"))
    ]
    lines: list[Seedance2SpokenLine] = []
    last_index = len(matches) - 1
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        text = raw[start:end].strip(" \t\r\n，,。；;")
        if index == last_index:
            # 最后一行台词后面若还缀着没有说话人前缀的旁白（``…。 苏念愣住…``），
            # 只保留第一句：校验只要求「至少包含台词」，宁可少 require，也不能把
            # 动作描述当成必录台词，把整条 beat 拦在发请求之前。
            text = _drop_trailing_prose(text)
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
