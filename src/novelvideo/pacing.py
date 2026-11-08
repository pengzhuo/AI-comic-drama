"""剧集节奏：每集≈目标时长（默认 60s），且镜头数尽量少。

这里集中放**确定性**的节奏工具，供两处复用：

- 分集（``sqlite_store.build_episodes_by_duration``）：把整篇剧本按自然边界切成
  ~目标时长的集；单集内容明显超长时按自然子边界拆分，略超时压缩到目标时长。
- 镜头合并（``literal_script_writing``）：逐行剧本天然“一行一镜”，这里把相邻且
  音频上下文一致的 beat 合并成更少、更长的镜头。

全程不依赖 LLM、不写文件，方便单测。时长按字数估算，语速沿用 ``config`` 的
TTS 配置；这样分集阶段与镜头阶段用的是同一套刻度。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Sequence

# 目标时长（秒）。成片 = 片头 + 正片 + 片尾，所以正片预算要扣掉头尾。
DEFAULT_EPISODE_TARGET_SECONDS = 60.0
# 单集短于该值就与相邻集合并，避免出现一堆十几秒的碎片集。
DEFAULT_EPISODE_MERGE_FLOOR_SECONDS = 45.0
# 单集（或单个自然单元）超过该值视为“超过太多”，按自然子边界拆分。
DEFAULT_EPISODE_SPLIT_THRESHOLD_SECONDS = 90.0
# 单个镜头的目标上限，决定“最少镜头数”：取视频模型允许的最长时长
# （Seedance2.5 = 30s；2.0 系列仍由各自的时长边界在生成器处回落到 15s）。
DEFAULT_SHOT_TARGET_SECONDS = 30.0
# 拆分单条超长朗读 beat 时的保护系数：TTS 实测语速通常比字数估算略慢，直接按
# “估算 == 模型上限”切分会卡在边界上，实际音频仍可能溢出。
SHOT_SPLIT_SAFETY = 0.9
DEFAULT_INTRO_SECONDS = 3.0
DEFAULT_OUTRO_SECONDS = 2.0

# 空旁白（纯画面 beat）给一个名义时长，避免在合并时被当成 0 秒吞掉。
_SILENCE_NOMINAL_SECONDS = 2.0

# 句末标点（中英文）。分句与拼接都以此为准。
_SENTENCE_END = "。！？!?…；;"
_CLAUSE_END = "，,、；;：:" + _SENTENCE_END + "\"'”’）)】」』"


def _env_float(name: str, default: float) -> float:
    import os

    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def _to_float(value: Any, default: float) -> float:
    if value is None:
        return float(default)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    if parsed <= 0:
        return float(default)
    return parsed


def default_chars_per_second() -> float:
    """旁白/混合文本的通用语速（字/秒）。"""

    try:
        from novelvideo.config import TTS_CHARS_PER_SECOND

        return float(TTS_CHARS_PER_SECOND)
    except Exception:  # pragma: no cover - 配置缺失时退回到实测均值
        return 5.8


def default_dialogue_chars_per_second() -> float:
    try:
        from novelvideo.config import TTS_DIALOGUE_CHARS_PER_SECOND

        return float(TTS_DIALOGUE_CHARS_PER_SECOND)
    except Exception:  # pragma: no cover
        return 4.45


def estimate_duration_seconds(
    text: str | None, *, chars_per_second: float | None = None
) -> float:
    """按去空白后的字数估算朗读时长（秒）。"""

    rate = float(chars_per_second or default_chars_per_second())
    if rate <= 0:
        rate = 1.0
    compact = re.sub(r"\s+", "", text or "")
    return len(compact) / rate


@dataclass(frozen=True)
class PacingSettings:
    """一套节奏参数（全局默认 / 项目覆盖后的结果）。"""

    episode_target_seconds: float = DEFAULT_EPISODE_TARGET_SECONDS
    merge_floor_seconds: float = DEFAULT_EPISODE_MERGE_FLOOR_SECONDS
    split_threshold_seconds: float = DEFAULT_EPISODE_SPLIT_THRESHOLD_SECONDS
    shot_target_seconds: float = DEFAULT_SHOT_TARGET_SECONDS
    intro_seconds: float = DEFAULT_INTRO_SECONDS
    outro_seconds: float = DEFAULT_OUTRO_SECONDS

    @property
    def content_budget_seconds(self) -> float:
        """正片预算：目标时长扣掉片头片尾。"""

        return max(1.0, self.episode_target_seconds - self.intro_seconds - self.outro_seconds)

    @property
    def max_shots(self) -> int:
        """一集最多几个镜头（向上取整）。"""

        return max(1, int(math.ceil(self.content_budget_seconds / max(self.shot_target_seconds, 0.1))))

    def normalized(self) -> "PacingSettings":
        """保证 merge_floor < target < split_threshold 的自洽。"""

        target = self.episode_target_seconds
        floor = min(self.merge_floor_seconds, target)
        threshold = max(self.split_threshold_seconds, target + 1.0)
        shot = self.shot_target_seconds
        return replace(
            self,
            episode_target_seconds=target,
            merge_floor_seconds=floor,
            split_threshold_seconds=threshold,
            shot_target_seconds=shot,
        )


def resolve_pacing_settings(config: dict | None = None) -> PacingSettings:
    """把（项目）配置 + 环境变量合成一套节奏参数。"""

    data = dict(config or {})
    settings = PacingSettings(
        episode_target_seconds=_env_float(
            "EPISODE_TARGET_SECONDS", DEFAULT_EPISODE_TARGET_SECONDS
        ),
        merge_floor_seconds=_env_float(
            "EPISODE_MERGE_FLOOR_SECONDS", DEFAULT_EPISODE_MERGE_FLOOR_SECONDS
        ),
        split_threshold_seconds=_env_float(
            "EPISODE_SPLIT_THRESHOLD_SECONDS", DEFAULT_EPISODE_SPLIT_THRESHOLD_SECONDS
        ),
        shot_target_seconds=_env_float(
            "SHOT_TARGET_SECONDS", DEFAULT_SHOT_TARGET_SECONDS
        ),
        intro_seconds=_env_float("EPISODE_INTRO_SECONDS", DEFAULT_INTRO_SECONDS),
        outro_seconds=_env_float("EPISODE_OUTRO_SECONDS", DEFAULT_OUTRO_SECONDS),
    )
    settings = replace(
        settings,
        episode_target_seconds=_to_float(
            data.get("episode_target_seconds"), settings.episode_target_seconds
        ),
        merge_floor_seconds=_to_float(
            data.get("episode_merge_floor_seconds"), settings.merge_floor_seconds
        ),
        split_threshold_seconds=_to_float(
            data.get("episode_split_threshold_seconds"),
            settings.split_threshold_seconds,
        ),
        shot_target_seconds=_to_float(
            data.get("shot_target_seconds"), settings.shot_target_seconds
        ),
    )
    return settings.normalized()


# ── 文本切分 ──────────────────────────────────────────────────────────────────


def _split_keeping_delimiters(text: str, delimiters: str) -> list[str]:
    """按给定标点集合切分，保留标点；无标点则整段返回。"""

    raw = (text or "").strip()
    if not raw:
        return []
    parts: list[str] = []
    buf: list[str] = []
    for ch in raw:
        buf.append(ch)
        if ch in delimiters:
            segment = "".join(buf).strip()
            if segment:
                parts.append(segment)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def split_sentences(text: str) -> list[str]:
    """按句末标点切句，保留标点；无标点则整段返回。"""

    return _split_keeping_delimiters(text, _SENTENCE_END)


def split_natural_units(text: str) -> list[str]:
    """把一段文本切成自然单元：优先空行分段，其次按句聚合成段。

    只做“自然边界”的粗切；真正的时长分组在 ``build_episode_segments`` 里。
    """

    raw = (text or "").strip()
    if not raw:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", raw) if p.strip()]
    if len(paragraphs) > 1:
        return paragraphs
    # 没有空行分隔时，退回到按句，再把句子两两并成单元，避免单元过碎。
    sentences = split_sentences(raw)
    if not sentences:
        return [raw]
    if len(sentences) == 1:
        return [raw]
    units: list[str] = []
    for i in range(0, len(sentences), 2):
        units.append("".join(sentences[i : i + 2]))
    return units


def _split_unit_by_sentences(
    text: str, *, target_seconds: float, chars_per_second: float
) -> list[str]:
    """按句子把超长单元切成 ~target_seconds 的块。"""

    sentences = split_sentences(text)
    if len(sentences) <= 1:
        return [text.strip()] if text.strip() else []
    chunks: list[str] = []
    buf: list[str] = []
    buf_seconds = 0.0
    for sentence in sentences:
        seconds = estimate_duration_seconds(sentence, chars_per_second=chars_per_second)
        if buf and buf_seconds + seconds > target_seconds:
            chunks.append("".join(buf).strip())
            buf = []
            buf_seconds = 0.0
        buf.append(sentence)
        buf_seconds += seconds
    if buf:
        chunks.append("".join(buf).strip())
    return [c for c in chunks if c]


def condense_text(
    text: str,
    *,
    max_seconds: float,
    chars_per_second: float | None = None,
) -> str:
    """把文本压缩到 max_seconds 以内（确定性降采样）。

    策略：保留开头若干句 + **保留最后一句**（结尾钩子），丢弃中间最不关键的
    部分。这样既不丢开场设定，也不丢悬念。已达标则原样返回。
    """

    raw = (text or "").strip()
    if not raw:
        return raw
    rate = float(chars_per_second or default_chars_per_second())
    if estimate_duration_seconds(raw, chars_per_second=rate) <= max_seconds:
        return raw
    sentences = split_sentences(raw)
    if len(sentences) <= 1:
        # 一整句都超预算：按字数硬截到预算。
        limit = max(1, int(max_seconds * rate))
        return re.sub(r"\s+", "", raw)[:limit]
    last = sentences[-1]
    last_seconds = estimate_duration_seconds(last, chars_per_second=rate)
    remaining = max_seconds - last_seconds
    head: list[str] = []
    acc = 0.0
    for sentence in sentences[:-1]:
        seconds = estimate_duration_seconds(sentence, chars_per_second=rate)
        if acc + seconds > remaining:
            break
        head.append(sentence)
        acc += seconds
    if not head:
        # 连开头都塞不下，只能只留结尾。
        return last
    return "".join(head) + last


# ── 分集：把自然单元按目标时长组合成集 ────────────────────────────────────────


def build_episode_segments(
    chapters: Sequence[tuple[int, str]],
    *,
    settings: PacingSettings | None = None,
    chars_per_second: float | None = None,
) -> list[dict[str, Any]]:
    """把 [(章节号, 章节正文), ...] 组合成 ~目标时长的分集。

    返回 ``[{"chapter_start", "chapter_end", "text"}, ...]``。

    规则（与产品口径一致）：
    - 自然单元（章节→段落）超过 ``split_threshold`` → 按句子拆成多块；
    - 组合后单集超过正片预算 → 压缩到预算；
    - 单集不足 ``merge_floor`` → 与相邻单元继续合并。
    """

    pacing = (settings or resolve_pacing_settings()).normalized()
    rate = float(chars_per_second or default_chars_per_second())
    budget = pacing.content_budget_seconds
    floor = pacing.merge_floor_seconds

    # 1) 展开为 (章节号, 文本, 时长) 的自然单元。
    #    - 章节 <= 拆分阈值：整章作为一个单元（后续分组/压缩）。
    #    - 章节 > 拆分阈值（“超过太多”）：按段落/句子切成 ~正片预算 的块。
    units: list[tuple[int, str, float]] = []
    for chapter_number, content in chapters:
        text = (content or "").strip()
        if not text:
            continue
        chapter_seconds = estimate_duration_seconds(text, chars_per_second=rate)
        if chapter_seconds <= pacing.split_threshold_seconds:
            units.append((chapter_number, text, chapter_seconds))
            continue
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()] or [text]
        for paragraph in paragraphs:
            paragraph_seconds = estimate_duration_seconds(paragraph, chars_per_second=rate)
            if paragraph_seconds > budget:
                pieces = _split_unit_by_sentences(
                    paragraph, target_seconds=budget, chars_per_second=rate
                )
            else:
                pieces = [paragraph]
            for piece in pieces:
                units.append(
                    (
                        chapter_number,
                        piece,
                        estimate_duration_seconds(piece, chars_per_second=rate),
                    )
                )

    if not units:
        return []

    # 2) 贪心分组：尽量凑到正片预算，切在自然单元边界。
    groups: list[list[tuple[int, str, float]]] = []
    current: list[tuple[int, str, float]] = []
    current_seconds = 0.0
    for unit in units:
        unit_seconds = unit[2]
        if current and current_seconds + unit_seconds > budget and current_seconds >= floor:
            groups.append(current)
            current = []
            current_seconds = 0.0
        current.append(unit)
        current_seconds += unit_seconds
    if current:
        # 结尾只剩极短的一截才并回上一组；否则单独成集，避免为了合并又得大幅压缩。
        if groups and current_seconds < floor / 2:
            groups[-1].extend(current)
        else:
            groups.append(current)

    # 3) 组装；超出预算的集压缩到预算。
    segments: list[dict[str, Any]] = []
    for group in groups:
        chapter_numbers = [g[0] for g in group]
        combined = "\n".join(g[1] for g in group)
        total_seconds = sum(g[2] for g in group)
        if total_seconds > budget:
            combined = condense_text(combined, max_seconds=budget, chars_per_second=rate)
        segments.append(
            {
                "chapter_start": min(chapter_numbers),
                "chapter_end": max(chapter_numbers),
                "text": combined,
            }
        )
    return segments


# ── 镜头合并：把相邻 beat 合并成更少、更长的镜头 ──────────────────────────────


def _scene_key(beat: Any) -> str:
    scene_ref = getattr(beat, "scene_ref", None)
    if scene_ref is None:
        return ""
    if isinstance(scene_ref, dict):
        return str(scene_ref.get("scene_id") or "")
    return str(getattr(scene_ref, "scene_id", "") or "")


def _audio_key(beat: Any) -> tuple[str, str, str]:
    return (
        str(getattr(beat, "audio_type", "") or ""),
        str(getattr(beat, "speaker", "") or ""),
        str(getattr(beat, "speaker_kind", "") or ""),
    )


def _merge_key(beat: Any) -> tuple[str, str, str, str, str]:
    audio_type, speaker, speaker_kind = _audio_key(beat)
    return (
        str(getattr(beat, "time_of_day", "") or ""),
        _scene_key(beat),
        audio_type,
        speaker,
        speaker_kind,
    )


def _is_silence_beat(beat: Any) -> bool:
    """纯画面 beat（``audio_type == "silence"``，来自剧本里的 △ 空镜行）。

    这类 beat 没有台词，是过场/氛围镜，不再单独成镜，而是并入相邻的对白/旁白镜，
    只贡献名义时长；是否保留其画面由 :func:`compact_beats_for_shots` 的宿主镜决定。
    """

    return str(getattr(beat, "audio_type", "") or "").strip().lower() == "silence"


def _join_narration(parts: Iterable[str]) -> str:
    out = ""
    for part in parts:
        text = (part or "").strip()
        if not text:
            continue
        if out and out[-1] not in _CLAUSE_END:
            out += "，"
        out += text
    return out


def _clone_beat(beat: Any, **updates: Any) -> Any:
    if hasattr(beat, "model_copy"):
        return beat.model_copy(update=updates)
    # Fallback:浅拷贝 + setattr（非 pydantic 的替身对象，测试用）。
    import copy

    clone = copy.copy(beat)
    for key, value in updates.items():
        setattr(clone, key, value)
    return clone


def compact_beats_for_shots(
    beats: Sequence[Any],
    *,
    shot_target_seconds: float = DEFAULT_SHOT_TARGET_SECONDS,
    max_shots: int | None = None,
    chars_per_second: float | None = None,
) -> list[Any]:
    """把相邻 beat 合并成更少、更长的镜头。

    - 只合并音频上下文一致的相邻 beat（同 ``audio_type``/``speaker``/``speaker_kind``
      /``time_of_day``/``scene``），保证配音不串。
    - 纯画面 ``silence`` 过场 beat（△ 空镜行）不单独成镜，直接并入相邻的对白/旁白
      组；宿主镜取对白/旁白 beat，画面锚点也优先取对白/旁白画面。
    - 累积预估时长达到单镜目标就断开。
    - 若给 ``max_shots``，先按 ``max(单镜目标, 总时长/max_shots)`` 放宽单镜预算，
      尽量压到镜头数上限。
    """

    items = [b for b in beats]
    if len(items) <= 1:
        return items

    rate = float(chars_per_second or default_chars_per_second())
    durations = [
        estimate_duration_seconds(getattr(b, "narration_segment", "") or "", chars_per_second=rate)
        for b in items
    ]
    # 纯画面 beat 给一个名义时长，避免被合并时当成 0 秒吞掉。
    durations = [d if d > 0 else _SILENCE_NOMINAL_SECONDS for d in durations]
    total = sum(durations)

    effective = float(shot_target_seconds)
    if max_shots and max_shots > 0:
        effective = max(effective, total / max_shots)
    if effective <= 0:
        effective = float(shot_target_seconds) or DEFAULT_SHOT_TARGET_SECONDS

    merged: list[Any] = []
    group_indices: list[int] = []
    group_seconds = 0.0
    group_key: tuple | None = None
    group_has_speech = False

    def flush() -> None:
        nonlocal group_indices, group_seconds, group_key, group_has_speech
        if not group_indices:
            return
        # 宿主镜：优先取组内第一个对白/旁白 beat，避免纯过场 beat 当宿主而丢掉音色。
        head = items[
            next(
                (i for i in group_indices if not _is_silence_beat(items[i])),
                group_indices[0],
            )
        ]
        narration = _join_narration(
            getattr(items[i], "narration_segment", "") for i in group_indices
        )
        # 画面锚点优先取对白/旁白的画面；组内只有过场时才退回过场画面。
        visual = ""
        for i in group_indices:
            candidate = str(getattr(items[i], "visual_description", "") or "").strip()
            if candidate and not _is_silence_beat(items[i]):
                visual = candidate
                break
        if not visual:
            for i in group_indices:
                candidate = str(
                    getattr(items[i], "visual_description", "") or ""
                ).strip()
                if candidate:
                    visual = candidate
                    break
        merged_seconds = sum(durations[i] for i in group_indices)
        beat_number = len(merged) + 1
        merged.append(
            _clone_beat(
                head,
                beat_number=beat_number,
                narration_segment=narration,
                visual_description=visual or str(getattr(head, "visual_description", "") or ""),
                estimated_duration=merged_seconds,
                # 手工分镜的显式时长对新合并出的 beat 不再适用；保留其顺序。
                duration_seconds=(
                    getattr(head, "duration_seconds", None)
                    if getattr(head, "is_manual_shot", False)
                    else None
                ),
            )
        )
        group_indices = []
        group_seconds = 0.0
        group_key = None
        group_has_speech = False

    for index, beat in enumerate(items):
        key = _merge_key(beat)
        silence = _is_silence_beat(beat)
        if group_indices and not silence and key != group_key:
            # 纯过场组并入即将开始的对白/旁白组（不先 flush），其余情况照常断开。
            if group_has_speech:
                flush()
            else:
                group_key = key
        if group_indices and group_seconds + durations[index] > effective:
            flush()
        group_indices.append(index)
        group_seconds += durations[index]
        if not silence:
            group_key = key
            group_has_speech = True
    flush()
    return merged


# ── 单条超长朗读 beat 拆分 ────────────────────────────────────────────────────


def _hard_split_by_chars(
    text: str, *, budget_seconds: float, chars_per_second: float
) -> list[str]:
    """单句在标点处仍切不动时，按字数硬切（保留原标点）。"""

    rate = float(chars_per_second) if chars_per_second > 0 else 1.0
    limit = max(1, int(max(0.1, budget_seconds) * rate))
    raw = text or ""
    return [
        raw[i : i + limit].strip()
        for i in range(0, len(raw), limit)
        if raw[i : i + limit].strip()
    ]


def _split_text_for_budget(
    text: str, *, budget_seconds: float, chars_per_second: float
) -> list[str]:
    """把一段文本切成各 ≤ ``budget_seconds`` 的块。

    优先在句末标点切、其次子句标点，单句仍超长时按字数硬切；最后把相邻小单元贪心
    合并到尽量贴近预算（但不超过）。
    """

    raw = (text or "").strip()
    if not raw:
        return []
    budget = max(0.1, float(budget_seconds))
    if estimate_duration_seconds(raw, chars_per_second=chars_per_second) <= budget:
        return [raw]

    units: list[str] = []
    for sentence in split_sentences(raw):
        if (
            estimate_duration_seconds(sentence, chars_per_second=chars_per_second)
            <= budget
        ):
            units.append(sentence)
            continue
        for clause in _split_keeping_delimiters(sentence, _CLAUSE_END):
            if (
                estimate_duration_seconds(clause, chars_per_second=chars_per_second)
                <= budget
            ):
                units.append(clause)
            else:
                units.extend(
                    _hard_split_by_chars(
                        clause,
                        budget_seconds=budget,
                        chars_per_second=chars_per_second,
                    )
                )

    chunks: list[str] = []
    buf: list[str] = []
    buf_seconds = 0.0
    for unit in units:
        seconds = estimate_duration_seconds(unit, chars_per_second=chars_per_second)
        if buf and buf_seconds + seconds > budget:
            chunks.append("".join(buf).strip())
            buf = []
            buf_seconds = 0.0
        buf.append(unit)
        buf_seconds += seconds
    if buf:
        chunks.append("".join(buf).strip())
    return [chunk for chunk in chunks if chunk]


def split_long_beats_for_shots(
    beats: Sequence[Any],
    *,
    max_seconds: float = DEFAULT_SHOT_TARGET_SECONDS,
    safety: float = SHOT_SPLIT_SAFETY,
    chars_per_second: float | None = None,
) -> list[Any]:
    """把单条过长的朗读 beat 拆成多条更短的 beat。

    :func:`compact_beats_for_shots` 只合并、不拆分，所以“一行就是一条”的超长台词/
    旁白仍会超过视频模型的单镜上限（Seedance2.5 = 30s），成片只能截断、末尾覆盖不到。
    这里在合并之后补一刀：

    - 只处理有朗读文本，且估算时长超过 ``max_seconds * safety`` 的 beat；
    - 优先在句末标点切分，其次子句标点，单句仍超长时按字数硬切；
    - 拆出的多条 beat 继承 speaker/``audio_type``/画面/场景/时间，序号整体重排；
    - 手工分镜（``is_manual_shot``，或带显式 ``duration_seconds``）与纯画面 beat 不动。

    ``safety`` 用来留余量：TTS 实测语速通常比字数估算略慢，直接把门槛压到估算上限
    时，实际音频仍可能溢出。
    """

    items = list(beats)
    if not items:
        return items

    rate = float(chars_per_second or default_chars_per_second())
    if rate <= 0:
        rate = 1.0
    cap = max(0.1, float(max_seconds) * float(safety))

    expanded: list[Any] = []
    split_any = False
    for beat in items:
        narration = str(getattr(beat, "narration_segment", "") or "").strip()
        if (
            not narration
            or getattr(beat, "is_manual_shot", False)
            or getattr(beat, "duration_seconds", None) is not None
        ):
            expanded.append(beat)
            continue
        if estimate_duration_seconds(narration, chars_per_second=rate) <= cap:
            expanded.append(beat)
            continue
        chunks = _split_text_for_budget(
            narration, budget_seconds=cap, chars_per_second=rate
        )
        if len(chunks) <= 1:
            expanded.append(beat)
            continue
        split_any = True
        for chunk in chunks:
            expanded.append(
                _clone_beat(
                    beat,
                    narration_segment=chunk,
                    estimated_duration=estimate_duration_seconds(
                        chunk, chars_per_second=rate
                    ),
                    duration_seconds=None,
                )
            )

    if not split_any:
        return items

    # 序号整体重排，保持 1..N 连续；只有序号变化的 beat 才需要克隆。
    renumbered: list[Any] = []
    for index, beat in enumerate(expanded, start=1):
        if getattr(beat, "beat_number", None) == index:
            renumbered.append(beat)
        else:
            renumbered.append(_clone_beat(beat, beat_number=index))
    return renumbered
