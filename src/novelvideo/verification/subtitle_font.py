"""Subtitle font resolution and CJK glyph coverage checks.

Burned-in subtitles are rendered by libass, which resolves the ``FontName`` from
the subtitle style through fontconfig. When that family is missing from the
image — or resolves to a font without CJK glyphs — every Chinese character is
drawn as a tofu box. The film still encodes successfully, so the failure stays
silent until somebody watches it.

:func:`resolve_subtitle_font` therefore resolves a candidate family up front and
verifies that the episode's own subtitle text is covered by that font's charset
*before* the expensive concat/burn pass runs. ``fc-match``/``fc-query`` ship with
fontconfig; on machines without them (developer boxes) the check degrades to
"unavailable" instead of failing the compose.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache

#: Candidate subtitle families, best first. All are OFL-licensed. The first
#: family that resolves *and* covers the episode's text wins; if none covers
#: everything, the one with the best coverage is used and the gap is reported.
SUBTITLE_FONT_CANDIDATES: tuple[str, ...] = (
    "Noto Sans CJK SC",
    "Noto Sans SC",
    "Source Han Sans SC",
    "Noto Serif CJK SC",
    "WenQuanYi Zen Hei",
    "WenQuanYi Micro Hei",
    "DejaVu Sans",
)

#: 字幕里真正需要字形覆盖的范围。CJK 统一表意文字 + 常用中文标点/全角符号。
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3000, 0x303F),  # CJK 标点
    (0x3400, 0x4DBF),  # 扩展 A
    (0x4E00, 0x9FFF),  # 基本区
    (0xF900, 0xFAFF),  # 兼容表意
    (0xFF00, 0xFFEF),  # 全角/半角形式
)

_FONTCOMMAND_TIMEOUT_SECONDS = 15


class SubtitleFontError(RuntimeError):
    """No installed font can render the episode's subtitle text."""


@dataclass(frozen=True)
class SubtitleFont:
    """A resolved subtitle family plus how well it covers the episode text."""

    family: str
    file: str
    missing: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing


def _run(command: list[str]) -> str | None:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_FONTCOMMAND_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


@lru_cache(maxsize=1)
def fontconfig_available() -> bool:
    """Return whether the fontconfig CLI is usable on this machine."""

    return _run(["fc-match", "-f", "%{file}", "DejaVu Sans"]) is not None


def parse_charset(spec: str) -> tuple[tuple[int, int], ...]:
    """Parse an ``fc-match``/``fc-query`` charset string into codepoint ranges.

    fontconfig separates ranges with **spaces** (``"20-7e a0-103 4e00-9fef"``);
    commas appear in some other tools' output. Splitting on only one of the two
    silently collapses the whole spec into a single bogus range, which then makes
    every character look "missing" and turns the coverage check into a false
    alarm — the module-level bug this split is here to prevent.
    """

    ranges: list[tuple[int, int]] = []
    for token in re.split(r"[,\s]+", str(spec or "")):
        token = token.strip()
        if not token:
            continue
        low, _, high = token.partition("-")
        try:
            start = int(low, 16)
        except ValueError:
            continue
        try:
            end = int(high, 16) if high else start
        except ValueError:
            end = start
        if end < start:
            start, end = end, start
        ranges.append((start, end))
    return tuple(ranges)


def covers(charset: tuple[tuple[int, int], ...], codepoint: int) -> bool:
    return any(start <= codepoint <= end for start, end in charset)


@lru_cache(maxsize=64)
def _match_family(family: str) -> tuple[str, tuple[tuple[int, int], ...]] | None:
    """Resolve ``family`` to ``(font_file, charset)`` in a single call.

    ``fc-match`` accepts the same ``%{charset}`` specifier as ``fc-query``
    (~2000 ranges for Noto Sans CJK), so one process gives both halves and the
    "resolve path, then query that path" round trip disappears.
    """

    output = _run(["fc-match", "-f", "%{file}\t%{charset}", family])
    if output is None:
        return None
    path, _, charset_spec = output.partition("\t")
    path = path.strip()
    if not path or path == "DejaVuSans.ttf":
        # fc-match always answers something; a bare filename means fontconfig
        # fell back to its built-in default rather than matching a family.
        return None
    return path, parse_charset(charset_spec)


def _unique_characters(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for character in str(text or ""):
        if character.strip() and character not in seen:
            seen[character] = None
    return list(seen)


def _is_cjk(codepoint: int) -> bool:
    return any(start <= codepoint <= end for start, end in _CJK_RANGES)


def matching_file_for_family(family: str) -> str | None:
    """Return the font file fontconfig resolves ``family`` to, if any."""

    match = _match_family(family)
    return match[0] if match else None


def resolve_subtitle_font(
    text: str,
    *,
    candidates: tuple[str, ...] = SUBTITLE_FONT_CANDIDATES,
) -> SubtitleFont | None:
    """Pick a subtitle family that can actually draw ``text``.

    Returns ``None`` when the platform has no fontconfig CLI (dev machines), in
    which case callers keep the static style untouched. Raises
    :class:`SubtitleFontError` when fontconfig *is* available but no candidate
    covers the CJK text — that is the tofu-box case the compose step must refuse
    to ship.
    """

    if not fontconfig_available():
        return None

    characters = _unique_characters(text)
    if not characters:
        for family in candidates:
            match = _match_family(family)
            if match is not None:
                return SubtitleFont(family=family, file=match[0])
        return None

    cjk_characters = [c for c in characters if _is_cjk(ord(c))]
    best: SubtitleFont | None = None

    for family in candidates:
        match = _match_family(family)
        if match is None:
            continue
        font_file, charset = match
        missing = tuple(c for c in characters if not covers(charset, ord(c)))
        candidate = SubtitleFont(family=family, file=font_file, missing=missing)
        if not missing:
            return candidate
        if best is None or len(candidate.missing) < len(best.missing):
            best = candidate

    if cjk_characters and (best is None or len(best.missing) >= len(characters)):
        raise SubtitleFontError(
            "没有任何已安装字体能渲染本期字幕的中文（会烧成豆腐块）。"
            f"候选字体: {', '.join(candidates)}；"
            "请安装 fonts-noto-cjk 后重试（见 Dockerfile）。"
        )
    return best


__all__ = [
    "SUBTITLE_FONT_CANDIDATES",
    "SubtitleFont",
    "SubtitleFontError",
    "covers",
    "fontconfig_available",
    "matching_file_for_family",
    "parse_charset",
    "resolve_subtitle_font",
]
