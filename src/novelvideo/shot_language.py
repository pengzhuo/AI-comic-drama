"""分镜规划：给每个 beat（镜头）定「景别 / 机位 / 运镜」。

这一步是**确定性**的，不走 LLM，理由有三：

1. 短剧镜头语言的核心是**相邻两镜必须有差**（正打↔反打、全景↔特写），这是一条
   跨镜头的约束；逐 beat 调用 LLM 看不到邻居，最容易给出「每镜都是中景」的结果。
2. 同一批剧本重跑，镜头语言必须一致，否则「重新生成一集」会带来莫名其妙的景别漂移。
3. 免费。分镜规划是一次纯规则计算，不该为它多付一次模型调用。

产出落在 beat 的 ``shot_size`` / ``shot_angle`` / ``shot_move`` 三个字段上，供
首帧生成（构图）与视频提示词（运动）共同使用。
"""

from __future__ import annotations

import copy
import dataclasses
import re
from dataclasses import dataclass
from typing import Any, Sequence

#: 景别（由近及远）。数值越大越远。
SHOT_SIZES: tuple[str, ...] = (
    "大特写",
    "特写",
    "近景",
    "中近景",
    "中景",
    "全景",
    "远景",
    "大远景",
)
#: 机位/角度。
CAMERA_ANGLES: tuple[str, ...] = (
    "平视",
    "过肩",
    "俯视",
    "仰视",
    "侧面",
    "背面",
    "第一人称",
)
#: 运镜。
CAMERA_MOVES: tuple[str, ...] = ("固定", "推", "拉", "摇", "移", "跟", "升降", "手持")

#: 短台词判定上限（去空白字数）：这么短的台词配特写最有力。
CLOSEUP_LINE_MAX_CHARS = 12

_EMOTION_MARKS = "！!？?"


@dataclass(frozen=True)
class ShotLanguage:
    """单个镜头的镜头语言。"""

    beat_number: int
    shot_size: str = ""
    shot_angle: str = ""
    shot_move: str = ""
    rationale: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "shot_size": self.shot_size,
            "shot_angle": self.shot_angle,
            "shot_move": self.shot_move,
        }

    def prompt_fragment(self) -> str:
        """给提示词用的中文短语，例如 ``中近景、过肩反打、固定机位``。"""

        parts: list[str] = []
        if self.shot_size:
            parts.append(self.shot_size)
        if self.shot_angle:
            parts.append(
                f"{self.shot_angle}反打" if self.shot_angle == "过肩" else self.shot_angle
            )
        if self.shot_move:
            parts.append(f"{self.shot_move}机位" if self.shot_move != "固定" else "固定机位")
        return "、".join(parts)


def _narration(beat: Any) -> str:
    if isinstance(beat, dict):
        text = beat.get("narration_segment") or beat.get("narration") or ""
    else:
        text = getattr(beat, "narration_segment", "") or getattr(beat, "narration", "") or ""
    return str(text or "")


def _audio_type(beat: Any) -> str:
    if isinstance(beat, dict):
        value = beat.get("audio_type") or ""
    else:
        value = getattr(beat, "audio_type", "") or ""
    return str(value or "").strip().lower()


def _scene_key(beat: Any) -> str:
    scene = beat.get("scene_ref") if isinstance(beat, dict) else getattr(beat, "scene_ref", None)
    if isinstance(scene, dict):
        return str(scene.get("scene_id") or scene.get("location") or "")
    if scene is not None:
        return str(getattr(scene, "scene_id", "") or "")
    return ""


def _beat_number(beat: Any, fallback: int) -> int:
    if isinstance(beat, dict):
        value = beat.get("beat_number")
    else:
        value = getattr(beat, "beat_number", None)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _next_size(size: str) -> str:
    """把景别往「更近一档」挪，用于消掉与上一镜的重复。"""

    try:
        index = SHOT_SIZES.index(size)
    except ValueError:
        return "特写"
    return SHOT_SIZES[max(0, index - 1)]


def plan_shot_language(beats: Sequence[Any]) -> list[ShotLanguage]:
    """按整集顺序给每个 beat 规划镜头语言。

    规则（从上到下依次判定，越靠前优先级越高）：

    - **建立镜头**：全片第 1 镜、以及每换一个场景后的第 1 镜，给全景平视（交代空间）。
    - **空镜/过场**（``audio_type == "silence"``）：远景平视、摇镜（氛围呼吸）。
    - **对白**：按该场景内的对白轮次**正打/反打交替**（平视 ↔ 过肩）；台词很短或带
      情绪标记时改用特写；连续三轮以上插一个特写，避免一直是中近景。
    - **旁白**：以中景为主，情绪标记给近景/推镜，每四镜插一个近景打破单调。
    - **收尾**：最后一镜推向近景/特写收束。
    - **硬约束**：相邻两镜的（景别, 机位）不得完全一致；撞了就就近调一档景别。
    """

    items = list(beats)
    if not items:
        return []

    planned: list[ShotLanguage] = []
    prev_scene = "\u0000"
    prev_size = ""
    prev_angle = ""
    dialogue_turn = 0
    narration_run = 0

    for index, beat in enumerate(items):
        beat_number = _beat_number(beat, index + 1)
        is_first = index == 0
        is_last = index == len(items) - 1
        audio_type = _audio_type(beat)
        text = _narration(beat)
        scene = _scene_key(beat)
        new_scene = scene != prev_scene

        if audio_type == "dialogue":
            dialogue_turn += 1
        else:
            dialogue_turn = 0
        if audio_type in {"narration", ""}:
            narration_run += 1
        else:
            narration_run = 0

        size, angle, move, why = "中景", "平视", "固定", ""

        if is_first or new_scene:
            size, angle = "全景", "平视"
            move = "推" if is_first else "固定"
            why = "建立镜头"
        elif audio_type == "silence":
            size, angle, move = "远景", "平视", "摇"
            why = "空镜氛围"
        elif audio_type == "dialogue":
            compact = re.sub(r"\s+", "", text)
            emotional = bool(compact) and compact[-1] in _EMOTION_MARKS
            if len(compact) <= CLOSEUP_LINE_MAX_CHARS or emotional or dialogue_turn % 3 == 0:
                size = "特写"
                why = "台词重音特写"
            else:
                size = "中近景"
                why = "对白"
            # 正打（平视）/反打（过肩）交替——反打是短剧语感的来源。
            angle = "平视" if dialogue_turn % 2 == 1 else "过肩"
        else:
            size = "中景"
            if narration_run % 4 == 0:
                size = "近景"
                why = "旁白换气"
            if any(mark in text for mark in _EMOTION_MARKS):
                size = "近景"
                move = "推"
                why = "旁白情绪推进"

        # 收尾推镜优先级**低于**建立镜头与空镜：开场/交代场景不该被压成特写，空镜也不该
        # 被推镜打断。只有普通镜头才做收尾处理（单镜集里同一条 beat 既是开场又是收尾，
        # 此时保留建立镜头）。
        if is_last and not is_first and not new_scene and audio_type != "silence":
            size = "特写" if audio_type == "dialogue" else "近景"
            move = "推"
            why = why or "收尾"
        if is_first:
            move = "推"

        # 硬约束：相邻两镜不能同景别同机位。优先调**景别**（机位承载正反打语义）。
        if size == prev_size and angle == prev_angle:
            size = _next_size(size)
            if size == prev_size:
                size = SHOT_SIZES[min(len(SHOT_SIZES) - 1, SHOT_SIZES.index(size) + 1)]
            why = f"{why}，避开上一镜重复" if why else "换切点"

        planned.append(
            ShotLanguage(
                beat_number=beat_number,
                shot_size=size,
                shot_angle=angle,
                shot_move=move,
                rationale=why,
            )
        )
        prev_scene = scene
        prev_size = size
        prev_angle = angle

    return planned


def plan_shot_language_by_number(beats: Sequence[Any]) -> dict[int, ShotLanguage]:
    """``{beat_number: ShotLanguage}``，方便 runner 按 beat 取用。"""

    return {item.beat_number: item for item in plan_shot_language(beats)}


def apply_shot_language_plan(beats: Sequence[Any]) -> list[Any]:
    """把整集的分镜规划盖回 beat 上，返回**新对象**（不原地改入参）。

    用在「草图之前」的入口：全局优化阶段排在草图**之后**，所以草图任务下发/剧本落库
    需要自己先算一次。``plan_shot_language`` 是纯函数且确定性，两处算出的值完全一致，
    重复调用不会漂移。
    """

    planned = {item.beat_number: item for item in plan_shot_language(beats)}
    result: list[Any] = []
    for index, beat in enumerate(beats):
        item = planned.get(_beat_number(beat, index + 1))
        update = item.as_dict() if item is not None else {"shot_size": "", "shot_angle": "", "shot_move": ""}
        result.append(_beat_with_shot_language(beat, update))
    return result


def _beat_with_shot_language(beat: Any, update: dict[str, str]) -> Any:
    if isinstance(beat, dict):
        merged = dict(beat)
        merged.update(update)
        return merged
    copier = getattr(beat, "model_copy", None)
    if callable(copier):
        return copier(update=update)
    if dataclasses.is_dataclass(beat) and not isinstance(beat, type):
        return dataclasses.replace(beat, **update)
    clone = copy.copy(beat)
    for key, value in update.items():
        if hasattr(clone, key):
            setattr(clone, key, value)
    return clone
