"""Dialogue parsing helpers for Seedance 2.0 voice references."""

from __future__ import annotations

import re
import unicodedata
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


#: 字幕只跟角色**对话台词**：``narration``（旁白/解说）虽然也是念出来的，但按
#: 产品口径不上屏——旁白照常配 TTS 出声，只是不烧进字幕。
_SUBTITLE_AUDIO_TYPES = frozenset({"dialogue"})

#: 「这一 beat 真正有人声念文本」的完整口径（台词 + 解说）。与字幕口径**解耦**：
#: 字幕收紧到只角色台词之后，声线预检 / 一致性 gate 仍按这里判断，否则旁白会被
#: 误判成无语音、静默丢掉声线参考与 gate 保障。
_SPOKEN_AUDIO_TYPES = frozenset({"dialogue", "narration"})


def _spoken_text_for_beat(beat: dict[str, Any], audio_types: frozenset[str]) -> str:
    if normalize_seedance2_audio_type(beat) not in audio_types:
        return ""
    raw = _spoken_source(beat)
    if not raw:
        return ""
    return "\n".join(
        cleaned
        for cleaned in (_strip_leading_script_marks(line) for line in raw.splitlines())
        if cleaned
    )


def beat_subtitle_text(beat: dict[str, Any]) -> str:
    """Return the text a composed film burns in as this beat's subtitle.

    字幕只显示角色**对话台词**：去掉 ``许知意（os）：`` 这类说话人标签和
    ``（神色坚定，喘息未平便开口）`` 这类表演提示，句末标点保留，多行文本逐行
    清洗；没有语音的 beat（silence/音效等）不出字幕，免得把画面描述当成台词。
    ``narration``（旁白/解说）同样不出字幕——旁白照常配音出声，只是不上屏。
    """

    return _spoken_text_for_beat(beat, _SUBTITLE_AUDIO_TYPES)


def beat_spoken_text(beat: dict[str, Any]) -> str:
    """Return the text this beat speaks aloud (台词 + 解说／旁白).

    口径比 :func:`beat_subtitle_text` 宽一档：旁白也算「有可念文本」。声线预检与
    一致性 gate 用它判断「这一 beat 是否必须有人声」，**不能**跟着字幕一起收紧。
    """

    return _spoken_text_for_beat(beat, _SPOKEN_AUDIO_TYPES)


#: 句末标点：切句用。``……``（两个 ``…`` 连写）由一个 ``+`` 一起吞掉；
#: ``；``/``;`` 也当断句点，但 ``，``/``,`` 不断（那是句中停顿，切了会碎）。
_SENTENCE_BREAK_RE = re.compile(r"[。！？!?…]+|[；;]")

#: 逐句 cue 的最短时长；短于这个值就不拆句，整句上一屏，免得字幕一闪而过。
SUBTITLE_MIN_CUE_SECONDS = 0.8


def split_subtitle_sentences(text: str) -> list[str]:
    """把一条 beat 的台词按标点切成若干句（含句末标点），用于逐句上屏。

    先按行切（多说话人台词本就是多行），再在每行内按句末标点切；空行与纯空白
    丢弃。切不动时返回 ``[原文]``，调用方按单句处理。
    """

    raw = str(text or "").strip()
    if not raw:
        return []

    sentences: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        start = 0
        for match in _SENTENCE_BREAK_RE.finditer(line):
            end = match.end()
            piece = line[start:end].strip()
            if piece:
                sentences.append(piece)
            start = end
        tail = line[start:].strip()
        if tail:
            sentences.append(tail)
    return sentences


def subtitle_cue_windows(
    text: str,
    start: float,
    end: float,
    *,
    min_seconds: float = SUBTITLE_MIN_CUE_SECONDS,
) -> list[tuple[str, float, float]]:
    """把一条 beat 的说话窗口按句拆成 ``(句子, 起, 止)``，用于配合说话的逐句字幕。

    窗口 ``[start, end]`` 是该 beat 在成片时间轴上占的时长（优先 ``beat_NN.mp3``，
    其次 clip）。多句时按**字数占比**把窗口依次分给各句，**逐句依次出现**（近似
    同步，不做强制对齐）：字数多的句子停留久一点。只有一句、窗口非正、或窗口太短
    撑不起 ``min_seconds`` 的逐句展示时，整条文本作为单个 cue 返回。
    """

    sentences = split_subtitle_sentences(text)
    span = float(end) - float(start)
    if not sentences:
        return []
    if len(sentences) <= 1 or span <= 0 or span < len(sentences) * min_seconds:
        return [(text, float(start), float(end))]

    weights = [max(1, len(sentence)) for sentence in sentences]
    total = float(sum(weights))
    windows: list[tuple[str, float, float]] = []
    cursor = float(start)
    last_index = len(sentences) - 1
    for index, (sentence, weight) in enumerate(zip(sentences, weights)):
        cue_end = float(end) if index == last_index else cursor + span * (weight / total)
        windows.append((sentence, cursor, cue_end))
        cursor = cue_end
    return windows


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


def _compact_dialogue_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip())


def dialogue_text_key(value: Any) -> str:
    """台词比对键：忽略空白与标点，只比字。

    最终提示词由上游模型重排，标点常被改写（源文 ``认定书上写着，`` 在提示词里成了
    ``认定书上写着：``）。比对要防的是「漏字改字」——标点差异不该把整条 beat 拦在
    发请求之前，但少字、改字仍然必须拦住。
    """

    return "".join(
        char
        for char in _compact_dialogue_text(value)
        if not unicodedata.category(char).startswith("P")
    )


#: 补齐台词时用的分节标题；模型只认提示词里写死的字。
_DIALOGUE_MENTION_HEADER = "【台词】"


def ensure_dialogue_text_mention(prompt: str, *, beat: dict[str, Any]) -> str:
    """台词 beat 的提示词必须逐字带上要念的台词，漏写时补一段（幂等）。

    流水线闸门 ``_validate_dialogue_final_prompt`` 要求最终提示词逐字包含台词；上游
    composer 偶尔只写镜头与资产约束、漏掉台词本身，整条 beat 会被拦在发请求之前。
    这里在发请求前把缺失的台词按说话人补进提示词，让「要念什么」与「提示词怎么写」
    对齐——不改写任何既有内容，已经出现的台词不重复追加，非台词 beat 与空提示词
    一律不动（空提示词交给调用方报「提示词为空」）。
    """

    text = str(prompt or "")
    if not text.strip():
        return text
    if normalize_seedance2_audio_type(beat) != "dialogue":
        return text

    prompt_key = dialogue_text_key(text)
    lines = parse_seedance2_spoken_lines(beat)
    if not lines:
        speaker = _text(beat.get("speaker"))
        lines = [
            Seedance2SpokenLine(speaker=speaker, text=line_text)
            for line_text in required_seedance2_dialogue_texts(beat)
            if line_text
        ]
    missing = [
        line
        for line in lines
        if dialogue_text_key(line.text) and dialogue_text_key(line.text) not in prompt_key
    ]
    if not missing:
        return text
    rendered = [
        f"{line.speaker.strip()}：{line.text}" if line.speaker.strip() else line.text
        for line in missing
    ]
    block = _DIALOGUE_MENTION_HEADER + "\n" + "\n".join(rendered)
    return f"{text}\n\n{block}"
