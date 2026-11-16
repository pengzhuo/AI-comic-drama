"""整集「声音层」：配乐 / 环境音 / 音效的设计与生成。

成片的声音过去只有人声（模型原生音轨或 TTS），没有配乐、没有环境底噪、没有
动作音效，听感是「干」的——这是与专业短剧最直观的差距之一。这一层在**视频生成
之后、合成导出之前**跑：按整集时间轴设计三类音频 cue（每集一条配乐、每个场景
一段环境音、脚本里显式标注的音效），逐条经网关音乐模型生成并缓存到
``audio/design/epNNN/``，最后由 :mod:`novelvideo.task_backend.runners.video`
的合成链路混入成片。

设计是**确定性**的（不调 LLM），只有音轨合成走模型，方便单测与复现。
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence

logger = logging.getLogger(__name__)

STAGE = "episode_audio_design"

#: 配乐/环境音/音效统一走网关的音乐模型（见 freezone/audio_node.py 的同款契约）。
MUSIC_MODEL = "LingShan-MU-11"

#: 三条母带支路的默认增益（相对人声 0dB）；最终还会过一遍 loudnorm 归一。
BGM_GAIN_DB = -13.0
AMBIENCE_GAIN_DB = -17.0
SFX_GAIN_DB = -5.0

#: 每集最多生成几段环境音 / 音效，避免一集几十次模型调用。
MAX_AMBIENCE_CUES = 6
MAX_SFX_CUES = 6

#: 网关音乐模型的长度下限（毫秒）。
MIN_CUE_MS = 3_000
MAX_CUE_MS = 600_000

#: 音效默认时长（脚本没给时长时）。
DEFAULT_SFX_SECONDS = 3.0

#: 部署级开关：关掉时流水线不再要求「声音层」（`EPISODE_AUDIO_DESIGN=0`）。
AUDIO_DESIGN_ENABLED_ENV = "EPISODE_AUDIO_DESIGN"

#: 每集环境音/音效上限的部署级覆盖（不设则用上面的默认值）。
AUDIO_DESIGN_MAX_AMBIENCE_ENV = "EPISODE_AUDIO_DESIGN_MAX_AMBIENCE"
AUDIO_DESIGN_MAX_SFX_ENV = "EPISODE_AUDIO_DESIGN_MAX_SFX"


def audio_design_enabled() -> bool:
    """整集声音层是否在流水线里启用（默认开）。"""

    import os

    raw = str(os.environ.get(AUDIO_DESIGN_ENABLED_ENV, "1") or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _int_env(name: str) -> int | None:
    import os

    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


def audio_design_caps() -> tuple[int | None, int | None]:
    """``(环境音上限, 音效上限)`` 的部署级覆盖；未配置时返回 ``(None, None)``。"""

    return (
        _int_env(AUDIO_DESIGN_MAX_AMBIENCE_ENV),
        _int_env(AUDIO_DESIGN_MAX_SFX_ENV),
    )


#: 脚本里显式标注音效的写法：``【加游戏音效：First Blood】`` / ``[音效：爆头]``。
_SFX_MARKER_RE = re.compile(r"音效\s*[：:]\s*([^】\]】\n，。；;！!？?]+)")
_SFX_SPLIT_RE = re.compile(r"[、,，/]+")


@dataclass(frozen=True)
class AudioCue:
    """一条待生成/待混入的音频 cue。"""

    cue_id: str
    kind: str  # bgm | ambience | sfx
    start_seconds: float
    end_seconds: float
    prompt: str
    gain_db: float = 0.0

    @property
    def duration_seconds(self) -> float:
        return max(0.0, float(self.end_seconds) - float(self.start_seconds))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AudioCue":
        return cls(
            cue_id=str(data.get("cue_id") or ""),
            kind=str(data.get("kind") or ""),
            start_seconds=float(data.get("start_seconds") or 0.0),
            end_seconds=float(data.get("end_seconds") or 0.0),
            prompt=str(data.get("prompt") or ""),
            gain_db=float(data.get("gain_db") or 0.0),
        )


def audio_design_dir(project_dir: str | Path, episode: int) -> Path:
    return Path(project_dir) / "audio" / "design" / f"ep{int(episode):03d}"


def audio_design_plan_path(project_dir: str | Path, episode: int) -> Path:
    return audio_design_dir(project_dir, episode) / "plan.json"


def cue_audio_path(project_dir: str | Path, episode: int, cue: AudioCue) -> Path:
    return audio_design_dir(project_dir, episode) / f"{cue.kind}_{cue.cue_id}.mp3"


def _scene_key(beat: dict[str, Any]) -> str:
    scene_ref = beat.get("scene_ref") or {}
    if isinstance(scene_ref, dict):
        scene_id = str(scene_ref.get("scene_id") or "").strip()
        if scene_id:
            return scene_id
    return str(beat.get("time_of_day") or "").strip() or "默认场景"


def _beat_text(beat: dict[str, Any]) -> str:
    parts = [
        str(beat.get("narration_segment") or ""),
        str(beat.get("visual_description") or ""),
    ]
    return "\n".join(part for part in parts if part)


def extract_sfx_names(text: str) -> list[str]:
    """从脚本文本里抽出显式标注的音效名。"""

    names: list[str] = []
    for match in _SFX_MARKER_RE.finditer(text or ""):
        for piece in _SFX_SPLIT_RE.split(match.group(1)):
            cleaned = piece.strip().strip("】[]【")
            if cleaned and cleaned not in names:
                names.append(cleaned)
    return names


def bgm_prompt(style_hint: str = "") -> str:
    hint = str(style_hint or "").strip() or "tense mystery drama"
    return (
        f"cinematic short-drama background score, {hint}, "
        "instrumental only, no vocals, restrained build, loopable, low mid energy "
        "so dialogue stays clear"
    )


def ambience_prompt(scene_key: str, sample_text: str = "") -> str:
    scene = str(scene_key or "").strip() or "indoor"
    snippet = re.sub(r"\s+", " ", str(sample_text or "")).strip()[:80]
    detail = f", context: {snippet}" if snippet else ""
    return (
        f"ambient soundscape only, {scene}{detail}, "
        "no music, no vocals, seamless background, natural room tone"
    )


def sfx_prompt(name: str) -> str:
    return f"single clean sound effect: {name}, no music, no vocals"


def design_audio_cues(
    *,
    timeline: Sequence[tuple[dict[str, Any], float, float]],
    style_hint: str = "",
    max_ambience: int | None = None,
    max_sfx: int | None = None,
) -> list[AudioCue]:
    """按整集时间轴设计三类 cue。

    ``timeline`` 是 ``(beat, start_seconds, end_seconds)`` 的有序序列。
    环境音/音效上限默认取模块常量，可传入覆盖以控制每集模型调用次数。
    """

    ambience_cap = MAX_AMBIENCE_CUES if max_ambience is None else int(max_ambience)
    sfx_cap = MAX_SFX_CUES if max_sfx is None else int(max_sfx)

    cues: list[AudioCue] = []
    if not timeline:
        return cues
    total = max(float(end) for _beat, _start, end in timeline)
    if total <= 0:
        return cues

    cues.append(
        AudioCue(
            cue_id="bgm",
            kind="bgm",
            start_seconds=0.0,
            end_seconds=total,
            prompt=bgm_prompt(style_hint),
            gain_db=BGM_GAIN_DB,
        )
    )

    # 环境音：相邻同场景的 beat 合成一段；超过上限就把剩下的并入最后一段。
    runs: list[tuple[str, float, float, str]] = []
    for beat, start, end in timeline:
        key = _scene_key(beat)
        if runs and runs[-1][0] == key:
            prev_key, prev_start, _prev_end, prev_text = runs[-1]
            runs[-1] = (prev_key, prev_start, float(end), prev_text)
        else:
            runs.append((key, float(start), float(end), _beat_text(beat)[:120]))
    if len(runs) > ambience_cap > 0:
        head = runs[: ambience_cap - 1]
        tail = runs[ambience_cap - 1 :]
        merged = (
            tail[0][0],
            tail[0][1],
            tail[-1][2],
            tail[0][3],
        )
        runs = head + [merged]
    for index, (key, start, end, sample) in enumerate(runs):
        if end - start <= 0:
            continue
        cues.append(
            AudioCue(
                cue_id=f"amb{index}",
                kind="ambience",
                start_seconds=start,
                end_seconds=end,
                prompt=ambience_prompt(key, sample),
                gain_db=AMBIENCE_GAIN_DB,
            )
        )

    sfx_count = 0
    for beat, start, end in timeline:
        if sfx_count >= sfx_cap:
            break
        for name in extract_sfx_names(_beat_text(beat)):
            if sfx_count >= sfx_cap:
                break
            duration = min(DEFAULT_SFX_SECONDS, max(0.5, float(end) - float(start)))
            cues.append(
                AudioCue(
                    cue_id=f"sfx{sfx_count}",
                    kind="sfx",
                    start_seconds=float(start),
                    end_seconds=float(start) + duration,
                    prompt=sfx_prompt(name),
                    gain_db=SFX_GAIN_DB,
                )
            )
            sfx_count += 1
    return cues


def cue_length_ms(cue: AudioCue) -> int:
    ms = int(math.ceil(cue.duration_seconds * 1000.0))
    return max(MIN_CUE_MS, min(MAX_CUE_MS, ms))


def load_audio_design_plan(
    project_dir: str | Path, episode: int
) -> list[dict[str, Any]]:
    """读回已生成的 cue 计划；文件不存在/损坏返回空列表。"""

    path = audio_design_plan_path(project_dir, episode)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    cues = payload.get("cues")
    return list(cues) if isinstance(cues, list) else []


async def generate_episode_audio_design(
    *,
    project_dir: str | Path,
    episode: int,
    timeline: Sequence[tuple[dict[str, Any], float, float]],
    style_hint: str = "",
    max_ambience: int | None = None,
    max_sfx: int | None = None,
    egress_context: Any = None,
    log: Callable[[str], None] | None = None,
    progress: Callable[[int, int], None] | None = None,
    generate: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """设计并生成整集声音层，落 ``plan.json``。

    单条 cue 生成失败只记日志、跳过该条（不留半成品音轨），保证这一阶段不会
    因为某一条音效被网关拒了而把整集卡住。
    """

    cues = design_audio_cues(
        timeline=timeline,
        style_hint=style_hint,
        max_ambience=max_ambience,
        max_sfx=max_sfx,
    )
    target_dir = audio_design_dir(project_dir, episode)
    target_dir.mkdir(parents=True, exist_ok=True)

    if generate is None:
        from novelvideo.freezone.audio_node import generate_music_audio_file

        generate = generate_music_audio_file

    def _emit(message: str) -> None:
        logger.info("%s", message)
        if log is not None:
            log(message)

    plan_cues: list[dict[str, Any]] = []
    generated = 0
    reused = 0
    failed: list[str] = []
    total = len(cues)
    for index, cue in enumerate(cues):
        path = cue_audio_path(project_dir, episode, cue)
        rel = path.relative_to(Path(project_dir)).as_posix()
        if path.exists() and path.stat().st_size > 0:
            reused += 1
            plan_cues.append({**cue.to_dict(), "audio_path": rel})
            if progress is not None:
                progress(index + 1, total)
            continue
        try:
            await generate(
                output_path=path,
                prompt=cue.prompt,
                music_length_ms=cue_length_ms(cue),
                egress_context=egress_context,
            )
        except Exception as exc:  # noqa: BLE001 - 单条失败不阻断整集
            failed.append(f"{cue.kind}:{cue.cue_id}: {type(exc).__name__}")
            _emit(f"声音层 {cue.kind} {cue.cue_id} 生成失败，跳过：{type(exc).__name__}")
            if progress is not None:
                progress(index + 1, total)
            continue
        if not path.exists() or path.stat().st_size <= 0:
            failed.append(f"{cue.kind}:{cue.cue_id}: 空文件")
            if progress is not None:
                progress(index + 1, total)
            continue
        generated += 1
        plan_cues.append({**cue.to_dict(), "audio_path": rel})
        _emit(f"声音层已生成 {cue.kind} {cue.cue_id}（{cue.duration_seconds:.1f}s）")
        if progress is not None:
            progress(index + 1, total)

    plan_path = audio_design_plan_path(project_dir, episode)
    plan_path.write_text(
        json.dumps(
            {
                "episode": int(episode),
                "style_hint": style_hint,
                "cues": plan_cues,
                "failed": failed,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "cue_count": len(plan_cues),
        "generated": generated,
        "reused": reused,
        "failed": failed,
        "plan_path": plan_path.as_posix(),
    }


def iter_mixable_cues(
    plan: Iterable[dict[str, Any]], project_dir: str | Path
) -> list[tuple[AudioCue, str]]:
    """把 plan 过滤成「音轨文件确实存在」的 ``(cue, 绝对路径)`` 列表。"""

    root = Path(project_dir)
    result: list[tuple[AudioCue, str]] = []
    for item in plan:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("audio_path") or "").strip()
        if not rel:
            continue
        path = root / rel
        if not path.exists() or path.stat().st_size <= 0:
            continue
        cue = AudioCue.from_dict(item)
        if cue.duration_seconds <= 0:
            continue
        result.append((cue, str(path)))
    return result
