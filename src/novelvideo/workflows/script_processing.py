"""剧本处理工作流 —— 内置「通用AI影视制作流程包」的阶段 1—2 执行体。

一键流程在 ``ingest_fast`` 完成后、角色/分集之前先跑本工作流：它把导入的
剧本原文交给流程包正文处理，产出

* ``understanding``  —— 剧本理解账（题材/时代/人物关系/叙事层/时间线/揭示边界…）
* ``characters``     —— 人物补全（名称/别名/定位/首次出现）
* ``scenes``         —— 场景与基础调度线索
* ``production_script`` —— 结构化制作剧本（逐场：谁→对谁/什么→做什么→触发→结果）
* ``gaps``           —— 原文无法确定、且会改变执行的未决项

长稿必须分段，而且分段大小由两件事共同决定：

1. **输入预算** —— :mod:`novelvideo.utils.model_context_budget` 按 UTF-8 字节
   保守估算，且流程包系统提示本身就占掉模型窗口的一大块；
2. **输出长度** —— 过大的分段会让模型的「理解账 + 逐场制作剧本」结构化输出
   被输出上限截断，随后 PydanticAI 的内建输出重试会把上一次的完整响应重新
   塞进下一次请求，反而撞上上下文上限（实测：一个 8,000 字段落首包约 102 KB
   可通过，但重试请求会涨到约 168 KB 而被拦下）。

因此本模块：

* 按**调用作用域**裁剪流程包注入：分段只带第 02 份，总控 01 只进归并调用；
* 关闭 PydanticAI 内建输出重试（``retries={"output": 0}``），改由本工作流用
  「丢弃历史 + 缩小片段」重试，任意一段失败都不会中断整条流水线；
* 归并调用超出预算时按批归并再合并，最差情况下退化为确定性合并。

两类失败必须分开处理，否则会互相加重：

* **请求/上下文太大**（超窗、结构化输出截断、400/413/422）——折小片段重试；
* **上游瞬时故障**（429、5xx、连接失败，含网关自身的过载保护
  ``system_cpu_overloaded``）——**保持片段大小**退避重试。折半只会把请求数翻倍，
  在网关已经因为 CPU 过载而拒绝请求时反而加压，所以这里绝不拆小；连续多次瞬时
  失败还会触发一次冷却，冷却期间本阶段的有效并发降到 1。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from typing import Awaitable, Callable, Iterable, Optional, TypeVar

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.exceptions import (
    ContentFilterError,
    ModelHTTPError,
    UnexpectedModelBehavior,
)

from novelvideo import script_process as script_process_store
from novelvideo.brainclaw_contract import BrainClawProfile
from novelvideo.config import (
    get_newapi_structured_output_model_settings,
    get_newapi_text_pydantic_model,
)
from novelvideo.skills import ai_film_process
from novelvideo.shared.transient_retry import (
    is_retryable_transient,
    transient_retry_delay,
)
from novelvideo.utils.model_context_budget import (
    ModelContextBudgetExceeded,
    TextContextBudget,
    estimate_tokens,
)
from novelvideo.utils.source_language import (
    AssetLanguage,
    asset_language_instruction,
    detect_asset_language,
)

STAGE = "script_process"

#: 单段原文的字符预算。分段只影响调用次数，不改变覆盖范围。
#: 片段越小，单次调用的输出越不易触顶；片段越大，跨场上下文越完整。
#: 中文长稿的实测结论：3,000 字左右能让「理解账 + 逐场制作剧本」在默认输出
#: 预留内写完，再大就会出现截断重试。可用 ``SCRIPT_PROCESS_CHUNK_MAX_CHARS`` 覆盖。
SCRIPT_PROCESS_CHUNK_MAX_CHARS = 3000
#: 单段拆分下限：拆到这个大小仍失败，就不再继续拆（改降输出预留或报错）。
SCRIPT_PROCESS_MIN_CHUNK_CHARS = 600
#: 单次调用的输出预留（tokens）。比全局默认（``NEWAPI_TEXT_OUTPUT_TOKENS``=8192）
#: 更宽松，只作用于本阶段，避免抬高全局默认后挤占其他文本调用的输入预算。
SCRIPT_PROCESS_MAX_OUTPUT_TOKENS = 16384
#: 输出预留的下调下限。
SCRIPT_PROCESS_MIN_OUTPUT_TOKENS = 4096
#: 归并输入（分段摘要 + 事实清单）的字符上限（还会再受上下文预算约束）。
SCRIPT_PROCESS_DIGEST_MAX_INPUT_CHARS = 24000
#: 每段摘要在归并输入中的截断长度。
SCRIPT_PROCESS_DIGEST_SEGMENT_CHARS = 2000
#: 使用的模型路由（与逐行脚本标注共用剧本域文本模型）。
SCRIPT_PROCESS_MODEL_ENV = "LITERAL_BEAT_META_MODEL"

#: 使用同一份原文的并发段数上限（``SCRIPT_PROCESS_CONCURRENCY`` 可覆盖）。
#: 默认沿用部署统一的 ``STRUCTURED_LLM_CONCURRENCY``（网关是共享瓶颈）。
SCRIPT_PROCESS_CONCURRENCY_ENV = "SCRIPT_PROCESS_CONCURRENCY"

#: 上游瞬时故障（429/5xx/连接失败）的同尺寸重试次数；用尽后照常上抛，
#: 由协调器的阶段级重试兜底。
SCRIPT_PROCESS_TRANSIENT_ATTEMPTS_ENV = "SCRIPT_PROCESS_TRANSIENT_ATTEMPTS"
SCRIPT_PROCESS_TRANSIENT_ATTEMPTS = 3
#: 同尺寸重试的退避（秒）。比叶子调用默认的 5s/20s 更长：网关的
#: ``system_cpu_overloaded``（宿主机 CPU 超阈值）需要时间回落。
SCRIPT_PROCESS_TRANSIENT_BACKOFF_ENV = "SCRIPT_PROCESS_TRANSIENT_BACKOFF_SECONDS"
SCRIPT_PROCESS_TRANSIENT_BACKOFF_SECONDS = (20.0, 60.0, 120.0)
#: 连续多少次瞬时失败判定为「上游过载」，触发一次冷却。
SCRIPT_PROCESS_OVERLOAD_STREAK_ENV = "SCRIPT_PROCESS_OVERLOAD_STREAK"
SCRIPT_PROCESS_OVERLOAD_STREAK = 3
#: 过载冷却时长（秒）。冷却期间只放行一个在飞调用（有效并发降为 1）。
SCRIPT_PROCESS_OVERLOAD_COOLDOWN_ENV = "SCRIPT_PROCESS_OVERLOAD_COOLDOWN_SECONDS"
SCRIPT_PROCESS_OVERLOAD_COOLDOWN_SECONDS = 60.0

#: 预算规划时给协议封装留的余量（字节）。
_PLANNING_MARGIN_BYTES = 2048
#: 单次调用至少要能放下这么多原文/摘要字节；低于此值说明系统提示本身
#: 已经吃掉整个模型窗口，继续拆分只会把原文切成海量碎片，因此直接报错。
_MIN_BODY_BYTES = 512

_SPINE_LABELS = {
    "drama": "剧本/短剧（drama，逐行对白与场次头）",
    "narrated": "小说解说（narrated，以旁白叙述为主）",
}

#: 会被「拆小重试」消化的错误；其余错误照常冒泡给协调器处理。
_RECOVERABLE_HTTP_STATUS = (400, 413, 422)

_SCHEMA_BYTES: dict[str, int] = {}

SYSTEM_PROMPT_HEADER = """# 你是「通用AI影视制作流程包」的剧本处理执行者

你只负责流程包中的**阶段 1—2**：先对导入剧本做全文读审（建立事实账本与理解账），
再优化补全基础调度与光线，最终输出结构化制作剧本。

## 执行边界
- 严格遵循下方注入的流程包正文；正文与用户当前要求冲突时，以用户当前要求为准。
- 只处理本次给出的原文片段：不得增删原文的事件、人物、台词、因果与揭示顺序。
- 原文没有给出的事实一律不得脑补；确实缺失或存在歧义的，登记到 `gaps`。
- 不写“同上”“与上一段一致”这类空话；每一场的入口、动作、出口都要能独立阅读。
- 只做剧本层工作：不设计分镜、不写视频提示词、不生成资产、不决定模型或预算。
"""


class CharacterFact(BaseModel):
    name: str = Field(default="", description="人物名，按原文原样保留")
    aliases: list[str] = Field(default_factory=list, description="别名/称呼")
    role: str = Field(default="", description="在故事中的定位与关系")
    first_appearance: str = Field(default="", description="首次出现的位置/场次")
    notes: str = Field(default="", description="会改变执行的补充事实（伤势、状态、知情范围等）")


class SceneFact(BaseModel):
    name: str = Field(default="", description="场景名，按原文原样保留")
    time_of_day: str = Field(default="", description="时间/光线条件")
    description: str = Field(default="", description="空间与基础调度线索")


class ScriptProcessSegmentOutput(BaseModel):
    """单段原文的处理结果。"""

    understanding_delta: str = Field(
        default="",
        description="本段的全文理解账（题材/时代/人物关系/叙事层/时间线/揭示边界/歧义）",
    )
    production_script: str = Field(
        default="",
        description="本段的结构化制作剧本（逐场：谁→对谁/什么→做什么→触发→结果，含基础调度与光线）",
    )
    characters: list[CharacterFact] = Field(default_factory=list)
    scenes: list[SceneFact] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class ScriptProcessDigestOutput(BaseModel):
    """跨段归并后的全局结果。"""

    understanding: str = Field(
        default="",
        description="全剧理解账（题材/时代/人物关系/情绪/节奏；叙事层与时间线；开场/转折/高潮/结局钩子；会改变执行的歧义）",
    )
    characters: list[CharacterFact] = Field(default_factory=list)
    scenes: list[SceneFact] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


@dataclass
class ScriptProcessResult:
    understanding: str = ""
    characters: list[CharacterFact] = field(default_factory=list)
    scenes: list[SceneFact] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    production_script: str = ""
    chunks: int = 0
    degraded_chunks: int = 0
    source_sha256: str = ""
    source_chars: int = 0
    result_path: str = ""


# ── 环境与预算 ────────────────────────────────────────────────────────────────


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def default_chunk_max_chars() -> int:
    """分段字符预算（``SCRIPT_PROCESS_CHUNK_MAX_CHARS`` 可覆盖）。"""

    return _env_positive_int(
        "SCRIPT_PROCESS_CHUNK_MAX_CHARS", SCRIPT_PROCESS_CHUNK_MAX_CHARS
    )


def default_max_output_tokens() -> int:
    """单次输出预留（``SCRIPT_PROCESS_MAX_OUTPUT_TOKENS`` 可覆盖）。"""

    return _env_positive_int(
        "SCRIPT_PROCESS_MAX_OUTPUT_TOKENS", SCRIPT_PROCESS_MAX_OUTPUT_TOKENS
    )


def default_concurrency() -> int:
    """并发段数（``SCRIPT_PROCESS_CONCURRENCY``，默认沿用全局 LLM 并发）。"""

    from novelvideo.utils.bounded_concurrency import default_llm_concurrency

    return _env_positive_int(SCRIPT_PROCESS_CONCURRENCY_ENV, default_llm_concurrency())


def default_transient_attempts() -> int:
    """瞬时故障的同尺寸重试次数（``SCRIPT_PROCESS_TRANSIENT_ATTEMPTS``）。"""

    return _env_positive_int(
        SCRIPT_PROCESS_TRANSIENT_ATTEMPTS_ENV, SCRIPT_PROCESS_TRANSIENT_ATTEMPTS
    )


def default_transient_backoff() -> tuple[float, ...]:
    """同尺寸重试的退避序列（``SCRIPT_PROCESS_TRANSIENT_BACKOFF_SECONDS``，逗号分隔）。"""

    raw = os.environ.get(SCRIPT_PROCESS_TRANSIENT_BACKOFF_ENV, "").strip()
    if not raw:
        return SCRIPT_PROCESS_TRANSIENT_BACKOFF_SECONDS
    values: list[float] = []
    for part in raw.split(","):
        try:
            value = float(part.strip())
        except ValueError:
            return SCRIPT_PROCESS_TRANSIENT_BACKOFF_SECONDS
        if value < 0:
            return SCRIPT_PROCESS_TRANSIENT_BACKOFF_SECONDS
        values.append(value)
    return tuple(values) or SCRIPT_PROCESS_TRANSIENT_BACKOFF_SECONDS


def default_overload_streak() -> int:
    """判定上游过载所需的连续瞬时失败次数（``SCRIPT_PROCESS_OVERLOAD_STREAK``）。"""

    return _env_positive_int(
        SCRIPT_PROCESS_OVERLOAD_STREAK_ENV, SCRIPT_PROCESS_OVERLOAD_STREAK
    )


def default_overload_cooldown() -> float:
    """过载冷却时长（``SCRIPT_PROCESS_OVERLOAD_COOLDOWN_SECONDS``）。"""

    raw = os.environ.get(SCRIPT_PROCESS_OVERLOAD_COOLDOWN_ENV, "").strip()
    if not raw:
        return SCRIPT_PROCESS_OVERLOAD_COOLDOWN_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return SCRIPT_PROCESS_OVERLOAD_COOLDOWN_SECONDS
    return value if value > 0 else SCRIPT_PROCESS_OVERLOAD_COOLDOWN_SECONDS


def _schema_bytes(output_type: type[BaseModel]) -> int:
    key = getattr(output_type, "__name__", str(output_type))
    cached = _SCHEMA_BYTES.get(key)
    if cached is None:
        cached = estimate_tokens(
            json.dumps(output_type.model_json_schema(), ensure_ascii=False)
        )
        _SCHEMA_BYTES[key] = cached
    return cached


def context_budget() -> TextContextBudget | None:
    """本阶段使用的上下文预算（``NEWAPI_TEXT_CONTEXT_TOKENS=0`` 时为 ``None``）。"""

    return TextContextBudget.from_env(SCRIPT_PROCESS_MODEL_ENV)


def _is_recoverable(exc: BaseException) -> bool:
    """拆小重试能消化的错误：超窗、结构化输出失败、请求体过大。"""

    if isinstance(exc, (ModelContextBudgetExceeded, UnexpectedModelBehavior)):
        return True
    if isinstance(exc, ModelHTTPError):
        return getattr(exc, "status_code", None) in _RECOVERABLE_HTTP_STATUS
    return False


def _is_transient(exc: BaseException) -> bool:
    """上游瞬时故障：408/425/429/5xx 或连接失败。

    这类错误与片段大小无关，等待后原样重试即可（计费错误由
    :func:`is_retryable_transient` 直接排除）。折小片段在这里只会让请求数翻倍，
    在网关因 CPU 过载而拒绝请求时反而加压，所以本模块绝不走拆分分支。
    """

    if isinstance(
        exc, (ModelContextBudgetExceeded, UnexpectedModelBehavior, ContentFilterError)
    ):
        return False
    return is_retryable_transient(exc)


def _transient_reason(exc: BaseException) -> str:
    """日志里可读的瞬时故障摘要（状态码优先，便于区分 429 与 503）。"""

    from novelvideo.shared.transient_retry import exception_http_status

    status = exception_http_status(exc)
    if status is not None:
        return f"HTTP {status}"
    return type(exc).__name__


def _is_digest_degradable(exc: BaseException) -> bool:
    """归并出错时可以退化成确定性合并（而不是中断阶段）的错误。"""

    return (
        isinstance(exc, ContentFilterError)
        or _is_recoverable(exc)
        or _is_transient(exc)
    )


def _truncate_to_bytes(text: str, limit: int) -> str:
    """按 UTF-8 字节上限截断（守卫口径），用于可丢弃的补充上下文。"""

    if limit <= 0:
        return ""
    if estimate_tokens(text) <= limit:
        return text
    buf: list[str] = []
    size = 0
    for char in text:
        char_bytes = len(char.encode("utf-8"))
        if size + char_bytes > limit:
            break
        buf.append(char)
        size += char_bytes
    return "".join(buf) + "\n…（因长度限制已截断）"


def split_chunk_in_half(text: str) -> tuple[str, str]:
    """把一段原文按行边界折半；单行时按码点硬切，保证两半都非空。"""

    source = str(text or "")
    if len(source) < 2:
        raise ValueError("原文片段过短，无法继续拆分")

    lines = source.splitlines()
    if len(lines) >= 2:
        half = len(source) // 2
        consumed = 0
        cut = 0
        for index, line in enumerate(lines):
            consumed += len(line) + 1
            if consumed >= half:
                cut = index + 1
                break
        else:  # pragma: no cover - 循环一定会 break
            cut = len(lines)
        left = "\n".join(lines[:cut]).strip()
        right = "\n".join(lines[cut:]).strip()
        if left and right:
            return left, right

    mid = max(1, len(source) // 2)
    left = source[:mid].strip()
    right = source[mid:].strip()
    if not left or not right:
        raise ValueError("原文片段无法折半")
    return left, right


def _dedupe_characters(items: Iterable[CharacterFact]) -> list[CharacterFact]:
    merged: dict[str, CharacterFact] = {}
    for item in items:
        name = (item.name or "").strip()
        key = name.casefold()
        if not key:
            continue
        existing = merged.get(key)
        if existing is None:
            merged[key] = item.model_copy(deep=True)
            continue
        current = merged[key]
        aliases = list(dict.fromkeys([*current.aliases, *item.aliases]))
        merged[key] = current.model_copy(
            update={
                "aliases": aliases,
                "role": current.role or item.role,
                "first_appearance": current.first_appearance or item.first_appearance,
                "notes": "\n".join(
                    part for part in (current.notes, item.notes) if part.strip()
                ),
            }
        )
    return list(merged.values())


def _dedupe_scenes(items: Iterable[SceneFact]) -> list[SceneFact]:
    merged: dict[str, SceneFact] = {}
    for item in items:
        name = (item.name or "").strip()
        key = name.casefold()
        if not key:
            continue
        existing = merged.get(key)
        if existing is None:
            merged[key] = item.model_copy(deep=True)
            continue
        current = merged[key]
        merged[key] = current.model_copy(
            update={
                "time_of_day": current.time_of_day or item.time_of_day,
                "description": current.description or item.description,
            }
        )
    return list(merged.values())


def _dedupe_strings(items: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def chunk_source_text(
    text: str,
    *,
    max_chars: int = SCRIPT_PROCESS_CHUNK_MAX_CHARS,
) -> list[str]:
    """按字符预算把原文切成保持行序的片段。

    只在行边界处切分；单行超预算时硬切，保证不丢失任何原文。
    """

    limit = max(1, int(max_chars))
    chunks: list[str] = []
    current: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal current, size
        if current:
            block = "\n".join(current).strip()
            if block:
                chunks.append(block)
        current = []
        size = 0

    for line in str(text or "").splitlines():
        line_size = len(line) + 1
        if line_size > limit:
            flush()
            for start in range(0, len(line), limit):
                piece = line[start : start + limit].strip()
                if piece:
                    chunks.append(piece)
            continue
        if current and size + line_size > limit:
            flush()
        current.append(line)
        size += line_size
    flush()
    return chunks


T = TypeVar("T")


class _SegmentLimiter:
    """分段并发闸门，兼做「上游过载」降速。

    正常时按配置并发放行；连续 ``streak`` 次瞬时失败（429/5xx/连接失败）后进入
    ``cooldown_seconds`` 秒冷却，冷却期间新调用必须先拿到串行闸门，于是有效并发
    降到 1。这样在网关自己报 ``system_cpu_overloaded`` 时不会继续加压。

    ``acquire`` 返回本次是否持有了串行闸门，释放时原样传回 :meth:`release`
    （同步释放，取消路径上不会因为 await 再抛错）。
    """

    def __init__(
        self,
        limit: int,
        *,
        streak: int,
        cooldown_seconds: float,
    ) -> None:
        self._limit = max(1, int(limit))
        self._streak_threshold = max(1, int(streak))
        self._cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._consecutive = 0
        self._resume_at = 0.0
        self._semaphore = asyncio.Semaphore(self._limit)
        self._serial = asyncio.Semaphore(1)

    @staticmethod
    def _now() -> float:
        return asyncio.get_running_loop().time()

    @property
    def cooling_down(self) -> bool:
        """当前是否处于过载冷却中。"""

        return self._resume_at > self._now()

    def note_success(self) -> None:
        self._consecutive = 0

    def note_transient_failure(self) -> bool:
        """记一次瞬时失败；返回 ``True`` 表示本次触发了过载冷却。"""

        self._consecutive += 1
        if self._consecutive < self._streak_threshold or self._cooldown_seconds <= 0:
            return False
        self._consecutive = 0
        self._resume_at = self._now() + self._cooldown_seconds
        return True

    async def acquire(self) -> bool:
        serial = self.cooling_down
        if serial:
            await self._serial.acquire()
        try:
            await self._semaphore.acquire()
        except BaseException:
            if serial:
                self._serial.release()
            raise
        return serial

    def release(self, serial: bool) -> None:
        self._semaphore.release()
        if serial:
            self._serial.release()


class ScriptProcessWorkflow:
    """按流程包处理导入剧本，并把产物写回项目目录。"""

    def __init__(
        self,
        *,
        output_dir: str,
        spine_template: str = "drama",
        chunk_max_chars: int | None = None,
        min_chunk_chars: int | None = None,
        max_output_tokens: int | None = None,
        digest_max_input_chars: int | None = None,
        concurrency: int | None = None,
        transient_attempts: int | None = None,
        transient_backoff: tuple[float, ...] | None = None,
        overload_streak: int | None = None,
        overload_cooldown_seconds: float | None = None,
    ) -> None:
        self.output_dir = str(output_dir)
        self.spine_template = "narrated" if spine_template == "narrated" else "drama"
        self.chunk_max_chars = (
            int(chunk_max_chars) if chunk_max_chars else default_chunk_max_chars()
        )
        self.min_chunk_chars = (
            int(min_chunk_chars) if min_chunk_chars else SCRIPT_PROCESS_MIN_CHUNK_CHARS
        )
        self.max_output_tokens = (
            int(max_output_tokens) if max_output_tokens else default_max_output_tokens()
        )
        self.min_output_tokens = SCRIPT_PROCESS_MIN_OUTPUT_TOKENS
        self.digest_max_input_chars = (
            int(digest_max_input_chars)
            if digest_max_input_chars
            else SCRIPT_PROCESS_DIGEST_MAX_INPUT_CHARS
        )
        self.concurrency = max(1, int(concurrency) if concurrency else default_concurrency())
        self.transient_attempts = max(
            1,
            int(transient_attempts)
            if transient_attempts
            else default_transient_attempts(),
        )
        self.transient_backoff = (
            tuple(float(item) for item in transient_backoff)
            if transient_backoff
            else default_transient_backoff()
        )
        self.overload_streak = max(
            1, int(overload_streak) if overload_streak else default_overload_streak()
        )
        self.overload_cooldown_seconds = (
            float(overload_cooldown_seconds)
            if overload_cooldown_seconds
            else default_overload_cooldown()
        )
        self._output_tokens = self.max_output_tokens
        self._limiter = self._make_limiter()
        self._budget = context_budget()
        self._output_language: AssetLanguage = "zh"
        self._segment_agent: Agent | None = None
        self._digest_agent: Agent | None = None

    @property
    def output_tokens(self) -> int:
        """本次运行实际使用的输出预留（可能因超窗被下调）。"""

        return self._output_tokens

    def _make_limiter(self) -> _SegmentLimiter:
        """每次运行新建一个闸门：``asyncio.Semaphore`` 与事件循环绑定，不可跨跑复用。"""

        return _SegmentLimiter(
            self.concurrency,
            streak=self.overload_streak,
            cooldown_seconds=self.overload_cooldown_seconds,
        )

    # ── agents ────────────────────────────────────────────────────────────

    def _system_prompt(self, scope: str = "segment") -> str:
        codes = ai_film_process.stage_scope_parts(STAGE, scope)
        titles = "、".join(
            f"第 {code} 份：{ai_film_process.PART_TITLES.get(code, code)}"
            for code in codes
        )
        pack = ai_film_process.stage_scope_pack(STAGE, scope)
        header = (
            f"{SYSTEM_PROMPT_HEADER}\n"
            f"## 流程包正文（版本 {ai_film_process.PACK_VERSION}；本次注入 {titles}）"
        )
        if not pack:
            return f"{header}\n\n{asset_language_instruction(self._output_language)}"
        return (
            f"{header}\n{pack}\n\n"
            f"{asset_language_instruction(self._output_language)}"
        )

    @staticmethod
    def _scope_for(output_type: type[BaseModel]) -> str:
        return "digest" if output_type is ScriptProcessDigestOutput else "segment"

    def _build_agent(self, output_type: type[BaseModel], name: str) -> Agent:
        return Agent(
            get_newapi_text_pydantic_model(
                SCRIPT_PROCESS_MODEL_ENV,
                brainclaw_profile=BrainClawProfile.LITERAL_BEAT_METADATA,
                capability="text.generate.workflow",
            ),
            system_prompt=self._system_prompt(self._scope_for(output_type)),
            model_settings={
                **get_newapi_structured_output_model_settings(),
                "max_tokens": self.output_tokens,
            },
            output_type=output_type,
            # 内建输出重试会把上一次的完整响应重新塞进下一次请求；流程包系统
            # 提示已占掉大半窗口，重试必然超窗。改由本工作流「新会话 + 更小
            # 片段」重试。
            retries={"output": 0},
            name=name,
        )

    @property
    def segment_agent(self) -> Agent:
        if self._segment_agent is None:
            self._segment_agent = self._build_agent(
                ScriptProcessSegmentOutput, "剧本处理（分段）"
            )
        return self._segment_agent

    @property
    def digest_agent(self) -> Agent:
        if self._digest_agent is None:
            self._digest_agent = self._build_agent(
                ScriptProcessDigestOutput, "剧本处理（归并）"
            )
        return self._digest_agent

    # ── 预算规划 ──────────────────────────────────────────────────────────

    def _reserve_tokens(
        self,
        *,
        scope: str,
        output_type: type[BaseModel],
        scaffold_bytes: int,
    ) -> int:
        return (
            (self._budget.framing_tokens if self._budget else 0)
            + self.output_tokens
            + scaffold_bytes
            + estimate_tokens(self._system_prompt(scope))
            + _schema_bytes(output_type)
            + _PLANNING_MARGIN_BYTES
        )

    def _body_limit_bytes(
        self,
        *,
        scope: str,
        output_type: type[BaseModel],
        scaffold_bytes: int,
    ) -> int | None:
        """本次调用可放入原文/摘要的字节上限；``None`` 表示守卫已关闭。"""

        if self._budget is None:
            return None
        reserve = self._reserve_tokens(
            scope=scope, output_type=output_type, scaffold_bytes=scaffold_bytes
        )
        return self._budget.available_input_tokens(reserve)

    def _split_by_bytes(self, text: str, limit: int) -> list[str]:
        if self._budget is None or limit <= 0:
            return chunk_source_text(text, max_chars=self.chunk_max_chars)
        reserve = max(0, self._budget.context_tokens - limit)
        pieces = [
            piece.strip()
            for piece in self._budget.plan_chunks(text, reserve_tokens=reserve)
            if piece.strip()
        ]
        return pieces or chunk_source_text(text, max_chars=self.chunk_max_chars)

    def plan_segments(self, text: str, *, spine_label: str) -> list[str]:
        """把原文切成既满足字符预算、又满足上下文预算的片段序列。"""

        chunks = chunk_source_text(text, max_chars=self.chunk_max_chars)
        scaffold = self._segment_prompt(
            body="", index=1, total=1, spine_label=spine_label
        )
        limit = self._body_limit_bytes(
            scope="segment",
            output_type=ScriptProcessSegmentOutput,
            scaffold_bytes=estimate_tokens(scaffold),
        )
        if limit is None:
            return chunks
        if limit < _MIN_BODY_BYTES:
            raise ValueError(
                "模型上下文预算不足：流程包系统提示已占满窗口，"
                "请核实 NEWAPI_TEXT_CONTEXT_TOKENS / MODEL_CONTEXT_WINDOWS_JSON 与模型真实窗口。"
            )

        planned: list[str] = []
        for chunk in chunks:
            if estimate_tokens(chunk) <= limit:
                planned.append(chunk)
            else:
                planned.extend(self._split_by_bytes(chunk, limit))
        return planned

    def _shrink_output_reservation(self) -> bool:
        if self._output_tokens <= self.min_output_tokens:
            return False
        self._output_tokens = max(self.min_output_tokens, self._output_tokens // 2)
        self._segment_agent = None
        self._digest_agent = None
        return True

    # ── run ───────────────────────────────────────────────────────────────

    async def run(
        self,
        *,
        source_text: str,
        on_progress: Optional[Callable[[float, str], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> ScriptProcessResult:
        text = str(source_text or "").strip()
        if not text:
            raise ValueError("导入剧本为空，无法进行剧本处理")

        def progress(value: float, task: str) -> None:
            if on_progress:
                on_progress(value, task)

        def log(message: str) -> None:
            if on_log:
                on_log(message)

        self._output_language = detect_asset_language(text)
        self._segment_agent = None
        self._digest_agent = None
        self._output_tokens = self.max_output_tokens
        self._limiter = self._make_limiter()
        source_sha256 = sha256(text.encode("utf-8")).hexdigest()
        spine_label = _SPINE_LABELS.get(self.spine_template, self.spine_template)

        planned = self.plan_segments(text, spine_label=spine_label)
        if not planned:
            raise ValueError("导入剧本无法切分出有效内容")
        total = len(planned)
        log(
            f"[ScriptProcess] 流程包 {ai_film_process.PACK_VERSION}；"
            f"分段注入 {list(ai_film_process.stage_scope_parts(STAGE, 'segment'))}，"
            f"归并注入 {list(ai_film_process.stage_scope_parts(STAGE, 'digest'))}；"
            f"原文 {len(text)} 字，计划 {total} 段（每段 ≤{self.chunk_max_chars} 字，"
            f"并发 {self.concurrency}）"
        )

        completed = 0

        async def process(item: tuple[int, str]) -> list[tuple[ScriptProcessSegmentOutput, bool]]:
            nonlocal completed
            ordinal, chunk = item
            outputs = await self._resolve_chunk(
                chunk=chunk,
                ordinal=ordinal,
                total=total,
                spine_label=spine_label,
                log=log,
            )
            completed += 1
            progress(
                0.05 + (completed / total) * 0.85,
                f"处理第 {completed}/{total} 段（流程包第 02 份：剧本与基础调度）...",
            )
            return outputs

        groups = await self._map_segments(
            list(enumerate(planned, start=1)), process=process
        )

        segments: list[ScriptProcessSegmentOutput] = []
        degraded = 0
        for group in groups:
            for segment, is_degraded in group:
                segments.append(segment)
                if is_degraded:
                    degraded += 1

        progress(0.92, "归并全剧理解账...")
        understanding, characters, scenes, gaps = await self._digest(
            segments=segments,
            spine_label=spine_label,
            log=log,
        )

        production_script = self._assemble_production_script(
            understanding=understanding,
            segments=segments,
            total=len(segments),
            source_chars=len(text),
        )

        progress(0.98, "保存剧本处理产物...")
        result_path = script_process_store.save_script_process_result(
            self.output_dir,
            payload={
                "pack_version": ai_film_process.PACK_VERSION,
                "parts": list(ai_film_process.stage_parts(STAGE)),
                "injection": {
                    "segment_parts": list(
                        ai_film_process.stage_scope_parts(STAGE, "segment")
                    ),
                    "digest_parts": list(
                        ai_film_process.stage_scope_parts(STAGE, "digest")
                    ),
                    "chunk_max_chars": self.chunk_max_chars,
                    "max_output_tokens": self.output_tokens,
                    "planned_segments": total,
                    "concurrency": self.concurrency,
                },
                "spine_template": self.spine_template,
                "source_sha256": source_sha256,
                "source_chars": len(text),
                "chunks": len(segments),
                "degraded_chunks": degraded,
                "understanding": understanding,
                "characters": [item.model_dump() for item in characters],
                "scenes": [item.model_dump() for item in scenes],
                "gaps": list(gaps),
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            production_script=production_script,
        )
        progress(1.0, "完成")

        return ScriptProcessResult(
            understanding=understanding,
            characters=characters,
            scenes=scenes,
            gaps=list(gaps),
            production_script=production_script,
            chunks=len(segments),
            degraded_chunks=degraded,
            source_sha256=source_sha256,
            source_chars=len(text),
            result_path=str(result_path),
        )

    async def _retry_transient(
        self,
        call: Callable[[], Awaitable[T]],
        *,
        label: str,
        log: Callable[[str], None],
        same_size: bool = True,
    ) -> T:
        """上游瞬时故障时按配置退避、**保持片段大小**重试。

        ``call`` 必须是可安全重复的调用（重建请求即可）。用尽次数后原样上抛，
        交给调用方（分段走阶段级兜底，归并走确定性合并）。
        """

        attempts = max(1, self.transient_attempts)
        for attempt in range(1, attempts + 1):
            try:
                result = await call()
            except Exception as exc:  # noqa: BLE001 - 分类由下面的判定负责
                if attempt >= attempts or not _is_transient(exc):
                    raise
                if self._limiter.note_transient_failure():
                    log(
                        f"[ScriptProcess][WARN] 上游连续 {self.overload_streak} 次过载，"
                        f"暂停 {self.overload_cooldown_seconds:.0f}s 冷却"
                        f"（冷却期间并发降为 1）"
                    )
                delay = transient_retry_delay(attempt, self.transient_backoff)
                hint = "，保持原片段大小重试" if same_size else ""
                log(
                    f"[ScriptProcess][WARN] {label}命中上游瞬时故障"
                    f"（{_transient_reason(exc)}），{delay:.0f}s 后重试"
                    f"（{attempt}/{attempts}）{hint}"
                )
                await asyncio.sleep(delay)
                continue
            self._limiter.note_success()
            return result
        raise AssertionError("unreachable: 重试循环必然返回或抛出")  # pragma: no cover

    async def _map_segments(
        self,
        items: list[tuple[int, str]],
        *,
        process: Callable[
            [tuple[int, str]],
            Awaitable[list[tuple[ScriptProcessSegmentOutput, bool]]],
        ],
    ) -> list[list[tuple[ScriptProcessSegmentOutput, bool]]]:
        """按并发上限处理各段，返回与 ``items`` 同序的结果。

        与 :func:`novelvideo.utils.bounded_concurrency.map_bounded` 的差别在于
        失败语义：那一个把异常吞成 ``None``（适合「一段坏了不影响别段」的
        抽取任务），而这里除了逐段可恢复错误外，都应该立刻中止整条流水线
        （计费/出口被拒/用户取消），所以首个致命异常会取消其余在飞的调用并
        直接上抛，不浪费这些注定失败的请求。
        """

        limit = max(1, self.concurrency)
        if limit == 1:
            return [await process(item) for item in items]

        limiter = self._limiter

        async def one(item: tuple[int, str]):
            serial = await limiter.acquire()
            try:
                return await process(item)
            finally:
                limiter.release(serial)

        tasks = [asyncio.create_task(one(item)) for item in items]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _resolve_chunk(
        self,
        *,
        chunk: str,
        ordinal: int,
        total: int,
        spine_label: str,
        log: Callable[[str], None],
    ) -> list[tuple[ScriptProcessSegmentOutput, bool]]:
        """处理一段原文，必要时拆小/降输出预留重试。

        返回该段产出的有序结果（重试过程中的拆分片段按原文顺序排列）。
        只有「拆到下限、输出预留也降到底仍失败」或计费/出口类错误才会抛出。
        上游瞬时故障已在 :meth:`_retry_transient` 里同尺寸退避消化，不会走到
        下面的拆分分支。
        """

        try:
            segment = await self._retry_transient(
                lambda: self._process_segment(
                    chunk=chunk,
                    index=ordinal,
                    total=total,
                    spine_label=spine_label,
                ),
                label=f"第 {ordinal}/{total} 段",
                log=log,
            )
        except ContentFilterError as exc:
            log(
                f"[ScriptProcess][WARN] 第 {ordinal}/{total} 段被内容审核拦截，"
                f"已跳过该段并记录未决项：{exc}"
            )
            return [
                (
                    ScriptProcessSegmentOutput(
                        understanding_delta="",
                        production_script="",
                        gaps=[
                            f"第 {ordinal}/{total} 段未通过内容审核，"
                            "未生成制作剧本，需人工复核。"
                        ],
                    ),
                    True,
                )
            ]
        except Exception as exc:  # noqa: BLE001 - 先分类再决定是否重抛
            if not _is_recoverable(exc):
                raise
            if len(chunk) > self.min_chunk_chars:
                left, right = split_chunk_in_half(chunk)
                log(
                    f"[ScriptProcess][WARN] 第 {ordinal} 段超出模型上下文"
                    f"（{type(exc).__name__}），已拆分为 {len(left)}+{len(right)} 字重试"
                )
                left_outputs = await self._resolve_chunk(
                    chunk=left,
                    ordinal=ordinal,
                    total=total,
                    spine_label=spine_label,
                    log=log,
                )
                right_outputs = await self._resolve_chunk(
                    chunk=right,
                    ordinal=ordinal,
                    total=total,
                    spine_label=spine_label,
                    log=log,
                )
                return left_outputs + right_outputs
            if self._shrink_output_reservation():
                log(
                    f"[ScriptProcess][WARN] 第 {ordinal} 段仍超窗，"
                    f"已把单次输出预留下调到 {self.output_tokens} tokens 重试"
                )
                return await self._resolve_chunk(
                    chunk=chunk,
                    ordinal=ordinal,
                    total=total,
                    spine_label=spine_label,
                    log=log,
                )
            raise ModelContextBudgetExceeded(
                f"第 {ordinal}/{total} 段输出超窗：该段已缩到 {len(chunk)} 字、单次输出"
                f"预留降到 {self.output_tokens} tokens 仍无法完成，"
                f"重试整个阶段不会改变结果。请核实 {SCRIPT_PROCESS_MODEL_ENV} 的真实"
                f"上下文窗口（NEWAPI_TEXT_CONTEXT_TOKENS / MODEL_CONTEXT_WINDOWS_JSON），"
                f"或下调 SCRIPT_PROCESS_CHUNK_MAX_CHARS。原始错误：{exc}"
            ) from exc
        log(f"[ScriptProcess] 段 {ordinal}/{total} 处理完成")
        return [(segment, False)]

    async def _process_segment(
        self,
        *,
        chunk: str,
        index: int,
        total: int,
        spine_label: str,
    ) -> ScriptProcessSegmentOutput:
        prompt = self._segment_prompt(
            body=chunk, index=index, total=total, spine_label=spine_label
        )
        result = await self.segment_agent.run(prompt)
        return result.output

    def _segment_prompt(
        self, *, body: str, index: int, total: int, spine_label: str
    ) -> str:
        return f"""{asset_language_instruction(self._output_language)}

请按流程包第 02 份（剧本与基础调度）处理以下原文片段。

## 本次任务
- 项目类型: {spine_label}
- 覆盖范围: 第 {index}/{total} 段，约 {len(body)} 字
- 交付: 本段的读审理解、结构化制作剧本、人物/场景事实与未决项

## 原文片段
{body}

## 输出要求
- `understanding_delta`: 本段全文理解。至少覆盖：题材/时代/人物关系/情绪/节奏；现实、回忆、梦境等叙事层与时间线；人物别名与首次视觉出现；逐场“谁→对谁/什么→做什么→触发→结果”；全部声音的说话人、受话人与可听见对象；场景、道具、服装、伤势、位置与状态变化；秘密/谎言/猜测/真相及揭示边界；本段内的开场、转折、高潮、结局钩子；会改变执行的歧义及原文位置。
- `production_script`: 本段的结构化制作剧本。逐场写：场次头（地点/时间/内外景）→ 出场人物 → 逐拍“谁→对谁/什么→做什么→触发→结果” → 基础调度（走位、朝向、持物、接触） → 光线条件（世界光源方向/时段/明暗）。只补全原文蕴含的调度，不新增剧情。台词按原文逐字保留，但不要重复同一句，也不要粘贴候选资产菜单或推导过程。
- `characters`: 本段出现的人物；名称为原文姓名，别名列出原文用过的称呼。
- `scenes`: 本段场景；`description` 写空间与可见地标，供后续场景资产使用。
- `gaps`: 原文无法确定、且会影响后续执行的问题；没有则留空。
"""

    # ── 归并 ──────────────────────────────────────────────────────────────

    async def _digest(
        self,
        *,
        segments: list[ScriptProcessSegmentOutput],
        spine_label: str,
        log: Callable[[str], None],
    ) -> tuple[str, list[CharacterFact], list[SceneFact], list[str]]:
        if len(segments) == 1:
            segment = segments[0]
            return (
                segment.understanding_delta,
                list(segment.characters),
                list(segment.scenes),
                list(segment.gaps),
            )
        try:
            return await self._digest_via_model(
                segments=segments, spine_label=spine_label, log=log
            )
        except Exception as exc:  # noqa: BLE001 - 归并失败不该毁掉已经跑完的分段
            if not _is_digest_degradable(exc):
                raise
            log(
                f"[ScriptProcess][WARN] 归并调用不可用（{type(exc).__name__}），"
                f"改用分段结果确定性合并：{exc}"
            )
            return self._merge_without_model(segments)

    async def _digest_via_model(
        self,
        *,
        segments: list[ScriptProcessSegmentOutput],
        spine_label: str,
        log: Callable[[str], None],
    ) -> tuple[str, list[CharacterFact], list[SceneFact], list[str]]:
        scaffold = self._digest_prompt(
            blocks="", spine_label=spine_label, label="", unit="本批"
        )
        limit = self._body_limit_bytes(
            scope="digest",
            output_type=ScriptProcessDigestOutput,
            scaffold_bytes=estimate_tokens(scaffold),
        )
        limit = (
            self.digest_max_input_chars
            if limit is None
            else min(limit, self.digest_max_input_chars)
        )
        if limit < _MIN_BODY_BYTES:
            log(
                "[ScriptProcess][WARN] 归并调用的上下文预算不足，"
                "改用分段结果确定性合并"
            )
            return self._merge_without_model(segments)

        groups = self._digest_groups(segments, limit)
        if len(groups) == 1:
            digest = await self._run_digest_prompt(
                blocks=groups[0],
                spine_label=spine_label,
                label=(
                    f"下面是同一部剧本各段（共 {len(segments)} 段）的读审摘要与事实清单。"
                    "请归并成全剧唯一一份理解账与人物/场景表。"
                ),
                unit="全剧",
                log=log,
            )
            log("[ScriptProcess] 全剧理解账归并完成")
            return (
                digest.understanding,
                list(digest.characters),
                list(digest.scenes),
                list(digest.gaps),
            )

        partials: list[ScriptProcessDigestOutput] = []
        pending: deque[tuple[int, str]] = deque(enumerate(groups, start=1))
        while pending:
            group_index, blocks = pending.popleft()
            label = (
                f"下面是同一部剧本第 {group_index}/{len(groups)} 批"
                f"（全剧共 {len(segments)} 段）的读审摘要与事实清单。"
                "请只在本批范围内归并，不要臆造本批未给出的段落。"
            )
            try:
                digest = await self._run_digest_prompt(
                    blocks=blocks,
                    spine_label=spine_label,
                    label=label,
                    unit="本批",
                    log=log,
                )
            except ContentFilterError as exc:
                log(
                    f"[ScriptProcess][WARN] 第 {group_index} 批归并被内容审核拦截，"
                    f"已跳过该批：{exc}"
                )
                continue
            except Exception as exc:  # noqa: BLE001 - 先分类再决定是否重抛
                if not _is_recoverable(exc):
                    raise
                # 批次输出被截断是「本批塞了太多段」，把这一批拆小再来，
                # 不要因为一批失败就丢掉其它批已经归并好的结果。
                try:
                    left, right = split_chunk_in_half(blocks)
                except ValueError:
                    left = right = ""
                if (
                    not left
                    or not right
                    or estimate_tokens(left) < _MIN_BODY_BYTES
                    or estimate_tokens(right) < _MIN_BODY_BYTES
                ):
                    log(
                        f"[ScriptProcess][WARN] 第 {group_index} 批归并失败且无法再拆，"
                        f"已跳过该批：{exc}"
                    )
                    continue
                log(
                    f"[ScriptProcess][WARN] 第 {group_index} 批归并失败"
                    f"（{type(exc).__name__}），已拆成两个子批重试"
                )
                pending.extendleft([(group_index, right), (group_index, left)])
                continue
            partials.append(digest)
            log(f"[ScriptProcess] 分批归并 {group_index}/{len(groups)} 完成")

        if not partials:
            log("[ScriptProcess][WARN] 所有分批归并都未产出结果，改用确定性合并")
            return self._merge_without_model(segments)

        return await self._merge_partials_via_model(
            partials=partials, spine_label=spine_label, log=log
        )

    async def _merge_partials_via_model(
        self,
        *,
        partials: list[ScriptProcessDigestOutput],
        spine_label: str,
        log: Callable[[str], None],
    ) -> tuple[str, list[CharacterFact], list[SceneFact], list[str]]:
        blocks = "\n".join(
            [
                f"### 第 {index} 批理解\n{part.understanding.strip() or '（无）'}"
                f"\n### 第 {index} 批事实\n"
                f"{json.dumps(self._facts_payload(part.characters, part.scenes, part.gaps), ensure_ascii=False)}"
                for index, part in enumerate(partials, start=1)
            ]
        )
        scaffold = self._digest_prompt(
            blocks="", spine_label=spine_label, label="", unit="全剧"
        )
        limit = self._body_limit_bytes(
            scope="digest",
            output_type=ScriptProcessDigestOutput,
            scaffold_bytes=estimate_tokens(scaffold),
        )
        limit = (
            self.digest_max_input_chars
            if limit is None
            else min(limit, self.digest_max_input_chars)
        )
        if limit < _MIN_BODY_BYTES or estimate_tokens(blocks) > limit:
            log(
                "[ScriptProcess][WARN] 分批归并结果仍超出预算，改用确定性合并"
            )
            return self._merge_digest_partials(partials)
        digest = await self._run_digest_prompt(
            blocks=blocks,
            spine_label=spine_label,
            label=(
                "下面是同一部剧本的分批归并结果。请合并成全剧唯一一份理解账与人物/场景表，"
                "同一人物、同一场景各留一条。"
            ),
            unit="全剧",
            log=log,
        )
        log("[ScriptProcess] 分批归并结果已合并")
        return (
            digest.understanding,
            list(digest.characters),
            list(digest.scenes),
            list(digest.gaps),
        )

    async def _run_digest_prompt(
        self,
        *,
        blocks: str,
        spine_label: str,
        label: str,
        unit: str,
        log: Callable[[str], None],
    ) -> ScriptProcessDigestOutput:
        prompt = self._digest_prompt(
            blocks=blocks, spine_label=spine_label, label=label, unit=unit
        )
        result = await self._retry_transient(
            lambda: self.digest_agent.run(prompt),
            label="归并调用",
            log=log,
            same_size=False,
        )
        return result.output

    def _digest_prompt(
        self, *, blocks: str, spine_label: str, label: str, unit: str
    ) -> str:
        return f"""{asset_language_instruction(self._output_language)}

{label}

## 项目类型
{spine_label}

## 分段理解摘要与事实清单
{blocks}

## 输出要求
- `understanding`: {unit}理解账。覆盖题材/时代/人物关系/情绪/节奏；叙事层与时间线；主要人物别名与首次出现；开场、转折、高潮、结局/钩子；贯穿全剧的秘密/谎言/揭示边界；会改变执行的整体歧义。不要罗列分段顺序，要做归并去重。
- `characters`: {unit}人物表，同一人物只留一条，合并别名与关键事实。
- `scenes`: {unit}场景表，同一场景只留一条。
- `gaps`: {unit}未决项，去重后保留真正影响执行的问题。
"""

    def _digest_groups(
        self, segments: list[ScriptProcessSegmentOutput], limit: int
    ) -> list[str]:
        """把分段摘要打包成若干批，每批都不超过预算。"""

        blocks: list[str] = []
        for index, segment in enumerate(segments, start=1):
            summary = (segment.understanding_delta or "").strip()
            if summary:
                summary = summary[:SCRIPT_PROCESS_DIGEST_SEGMENT_CHARS]
            facts = json.dumps(
                self._facts_payload(segment.characters, segment.scenes, segment.gaps),
                ensure_ascii=False,
            )
            blocks.append(
                _truncate_to_bytes(
                    f"### 第 {index} 段理解\n{summary or '（无）'}"
                    f"\n### 第 {index} 段事实\n{facts}",
                    limit,
                )
            )

        groups: list[str] = []
        current: list[str] = []
        used = 0
        for block in blocks:
            block_bytes = estimate_tokens(block)
            if current and used + block_bytes > limit:
                groups.append("\n".join(current))
                current = []
                used = 0
            current.append(block)
            used += block_bytes
        if current:
            groups.append("\n".join(current))
        return groups

    @staticmethod
    def _facts_payload(
        characters: list[CharacterFact],
        scenes: list[SceneFact],
        gaps: list[str],
    ) -> dict[str, object]:
        return {
            "characters": [item.model_dump() for item in characters],
            "scenes": [item.model_dump() for item in scenes],
            "gaps": list(gaps),
        }

    @classmethod
    def _merge_without_model(
        cls, segments: list[ScriptProcessSegmentOutput]
    ) -> tuple[str, list[CharacterFact], list[SceneFact], list[str]]:
        understanding = "\n\n".join(
            part.strip() for part in (s.understanding_delta for s in segments) if part.strip()
        )
        characters = _dedupe_characters(
            item for segment in segments for item in segment.characters
        )
        scenes = _dedupe_scenes(item for segment in segments for item in segment.scenes)
        gaps = _dedupe_strings(gap for segment in segments for gap in segment.gaps)
        return understanding, characters, scenes, gaps

    @classmethod
    def _merge_digest_partials(
        cls, partials: list[ScriptProcessDigestOutput]
    ) -> tuple[str, list[CharacterFact], list[SceneFact], list[str]]:
        understanding = "\n\n".join(
            part.understanding.strip()
            for part in partials
            if part.understanding.strip()
        )
        characters = _dedupe_characters(
            item for part in partials for item in part.characters
        )
        scenes = _dedupe_scenes(item for part in partials for item in part.scenes)
        gaps = _dedupe_strings(gap for part in partials for gap in part.gaps)
        return understanding, characters, scenes, gaps

    @staticmethod
    def _assemble_production_script(
        *,
        understanding: str,
        segments: list[ScriptProcessSegmentOutput],
        total: int,
        source_chars: int,
    ) -> str:
        parts: list[str] = [
            "# 结构化制作剧本",
            "",
            (
                f"> 由「通用AI影视制作流程包」阶段 1—2 生成；"
                f"原文 {source_chars} 字，共 {total} 段，覆盖全部原文。"
            ),
            "",
        ]
        if understanding.strip():
            parts.extend(["## 全剧理解账", "", understanding.strip(), "", "---", ""])
        for index, segment in enumerate(segments, start=1):
            body = (segment.production_script or "").strip()
            parts.append(f"## 第 {index}/{total} 段")
            parts.append("")
            parts.append(body or "（本段未生成制作剧本，见 gaps。）")
            parts.append("")
        return "\n".join(parts).strip() + "\n"


__all__ = [
    "STAGE",
    "SCRIPT_PROCESS_CHUNK_MAX_CHARS",
    "SCRIPT_PROCESS_DIGEST_MAX_INPUT_CHARS",
    "SCRIPT_PROCESS_DIGEST_SEGMENT_CHARS",
    "SCRIPT_PROCESS_MAX_OUTPUT_TOKENS",
    "SCRIPT_PROCESS_MIN_CHUNK_CHARS",
    "SCRIPT_PROCESS_MIN_OUTPUT_TOKENS",
    "SCRIPT_PROCESS_MODEL_ENV",
    "SCRIPT_PROCESS_OVERLOAD_COOLDOWN_SECONDS",
    "SCRIPT_PROCESS_OVERLOAD_STREAK",
    "SCRIPT_PROCESS_TRANSIENT_ATTEMPTS",
    "SCRIPT_PROCESS_TRANSIENT_BACKOFF_SECONDS",
    "SYSTEM_PROMPT_HEADER",
    "CharacterFact",
    "SceneFact",
    "ScriptProcessDigestOutput",
    "ScriptProcessResult",
    "ScriptProcessSegmentOutput",
    "ScriptProcessWorkflow",
    "chunk_source_text",
    "context_budget",
    "default_chunk_max_chars",
    "default_max_output_tokens",
    "default_overload_cooldown",
    "default_overload_streak",
    "default_transient_attempts",
    "default_transient_backoff",
    "split_chunk_in_half",
]
