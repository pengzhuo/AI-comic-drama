"""Boundary-aware speaker → character matching shared by every voice path.

A beat's ``speaker`` is either a character name (``许知意``) or a name plus an
identity suffix (``许知意_十六岁时期``). Matching with plain ``str.startswith``
lets a shorter name shadow a longer one: ``系统`` swallowed
``系统提示音_系统界面形态``, so the dialogue path dubbed the line with 系统's
voice while the video path demanded 系统提示音's own sample and refused to
generate ("缺少参考声线"). Every path now shares this module so the identity
that speaks and the identity that must own the reference sample agree.
"""

from __future__ import annotations

from typing import Any, Iterable

IDENTITY_SEPARATOR = "_"


def _text(value: Any) -> str:
    return str(value or "").strip()


def speaker_character_name(value: str) -> str:
    """Return the display name of a speaker string (``名前`` of ``名前_身份``)."""

    text = _text(value)
    if IDENTITY_SEPARATOR in text:
        return text.split(IDENTITY_SEPARATOR, 1)[0]
    return text


def speaker_matches_character_name(speaker: str, character_name: str) -> bool:
    """Return whether ``speaker`` addresses ``character_name`` and nothing longer.

    The identity suffix must be separated by ``_``, so ``系统`` never matches
    ``系统提示音_系统界面形态`` even though it is a literal string prefix of it.
    """

    speaker_text = _text(speaker)
    name = _text(character_name)
    if not speaker_text or not name:
        return False
    if speaker_text == name or speaker_text.startswith(f"{name}{IDENTITY_SEPARATOR}"):
        return True
    return speaker_character_name(speaker_text) == name


def select_character_for_speaker(speaker: str, characters: Iterable[Any]) -> Any | None:
    """Return the character a speaker refers to, preferring the longest name.

    ``characters`` is any iterable of objects exposing ``name``; the returned
    value is one of those objects or ``None`` when nothing matches.
    """

    speaker_text = _text(speaker)
    if not speaker_text:
        return None
    best: Any | None = None
    best_length = -1
    for character in characters:
        name = _text(getattr(character, "name", ""))
        if not name or len(name) <= best_length:
            continue
        if speaker_matches_character_name(speaker_text, name):
            best = character
            best_length = len(name)
    return best
