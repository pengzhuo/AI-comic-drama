"""Deterministic 剧本预处理（把导入的剧本变成系统能直接消费的形式）。

导入的剧本是给人看的，同一份场景元数据会有多种写法::

    1-1 纺织厂 夜 内                                  标准单行场景头
    1-1
    场景：纺织厂    夜 内        天气：闷热            场号独占一行 + 标注行
    场景：纺织厂    夜 内        天气：闷热            没有场号，只有标注行
    1-1 纺织厂 夜 内        天气：闷热                  单行 + 尾部制作标注

下游阶段（场景资产编译、逐行脚本）用 :func:`parse_scene_blocks` 确定场景边界，
并按「地点名」精确匹配场景资产，所以预处理先把文本规范成单行场景头，再交给技能
阶段。特点是**纯函数、幂等、不丢内容**：尾部的制作标注（``天气：…``）会被移到
自己的行上，既不会污染地点名，也不会被丢掉。

已经是标准格式的文本逐字节原样返回，``changed`` 为 ``False``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

from novelvideo.utils.screenplay_scene_parser import (
    ENGLISH_EPISODE_HEADER_RE,
    LABELED_LOCATION_RE,
    canonical_scene_heading,
    is_bare_scene_number_line,
    is_scene_start_line,
    split_scene_heading_attributes,
)

#: 汇报给界面/日志的样例条数上限（全文改动量仍会计数）。
MAX_REPORTED_CHANGES = 5

#: 集号行的宽松识别：导入稿常写「第 1 集【事故】」（数字两侧有空格），而解析器里
#: 的 ``EPISODE_HEADER_RE`` 只认「第1集」。这里只用于统计，不影响场景边界判定。
_EPISODE_LINE_RE = re.compile(
    r"^第\s*[一二三四五六七八九十百千万\d]+\s*[集章回](?:\s|$|[【\[（(：:·.—-])"
)


@dataclass
class PreprocessChange:
    """一处规范化改动，带原始行号，便于界面展示「处理前 → 处理后」。"""

    kind: str
    line_no: int
    before: str
    after: list[str] = field(default_factory=list)


@dataclass
class ScreenplayNormalization:
    text: str
    changed: bool
    changes: list[PreprocessChange]
    episodes: int
    scene_headings: int
    stats: dict[str, int]

    @property
    def samples(self) -> list[dict[str, object]]:
        return [
            {
                "kind": change.kind,
                "line_no": change.line_no,
                "before": change.before,
                "after": list(change.after),
            }
            for change in self.changes[:MAX_REPORTED_CHANGES]
        ]


def normalize_screenplay_text(text: str) -> ScreenplayNormalization:
    """把导入文本规范成系统可消费的形式（幂等、无 LLM、不丢内容）。"""

    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")

    replacements: dict[int, list[str]] = {}
    skipped: set[int] = set()
    changes: list[PreprocessChange] = []
    stats = {
        "split_headings_merged": 0,
        "headings_rewritten": 0,
        "notes_preserved": 0,
    }
    episodes = 0

    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue
        if _EPISODE_LINE_RE.match(line) or ENGLISH_EPISODE_HEADER_RE.match(line):
            episodes += 1
            continue

        if index in skipped:
            # 已经并入上一行的标注行（场号独占一行 + 标注行）不再单独处理。
            continue
        if is_bare_scene_number_line(line):
            # 场号独占一行：紧跟着的标注行才是地点/时间/内外景。
            lookahead = index + 1
            while lookahead < len(lines) and not lines[lookahead].strip():
                lookahead += 1
            if lookahead >= len(lines):
                continue
            metadata = lines[lookahead].strip()
            if not LABELED_LOCATION_RE.match(metadata):
                continue
            merged = f"{line} {metadata}"
            heading = canonical_scene_heading(merged)
            if not heading:
                continue
            _, note = split_scene_heading_attributes(merged)
            after = [heading] + ([note] if note else [])
            replacements[index] = after
            skipped.add(lookahead)
            stats["split_headings_merged"] += 1
            if note:
                stats["notes_preserved"] += 1
            changes.append(
                PreprocessChange(
                    kind="merge_split_heading",
                    line_no=index + 1,
                    before=merged,
                    after=after,
                )
            )
            continue

        heading = canonical_scene_heading(line)
        if heading is None:
            continue
        _, note = split_scene_heading_attributes(line)
        if heading == line and not note:
            continue
        after = [heading] + ([note] if note else [])
        replacements[index] = after
        stats["headings_rewritten"] += 1
        if note:
            stats["notes_preserved"] += 1
        changes.append(
            PreprocessChange(
                kind="rewrite_heading",
                line_no=index + 1,
                before=line,
                after=after,
            )
        )

    if not replacements and not skipped:
        return ScreenplayNormalization(
            text=normalized,
            changed=False,
            changes=[],
            episodes=episodes,
            scene_headings=_count_scene_headings(normalized),
            stats=stats,
        )

    rebuilt: list[str] = []
    for index, raw_line in enumerate(lines):
        if index in skipped:
            continue
        rebuilt.extend(replacements.get(index, [raw_line]))
    preprocessed = "\n".join(rebuilt)

    return ScreenplayNormalization(
        text=preprocessed,
        changed=preprocessed != normalized,
        changes=changes,
        episodes=episodes,
        scene_headings=_count_scene_headings(preprocessed),
        stats=stats,
    )


def _count_scene_headings(text: str) -> int:
    return sum(1 for line in (text or "").split("\n") if is_scene_start_line(line))
