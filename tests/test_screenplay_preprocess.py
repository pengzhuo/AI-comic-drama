"""剧本预处理：把导入剧本规范化成系统可消费的形式。

确定性、幂等、不丢内容——尾部制作标注（``天气：…``）移到独立行而不是留在地点名
里；已经是规范格式的文本逐字节原样返回。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from novelvideo import screenplay_preprocess
from novelvideo.utils.screenplay_normalize import normalize_screenplay_text

TWO_LINE_EPISODE = (
    "第 1 集【事故】\n"
    "\n"
    "1-1\n"
    "\n"
    "场景：中爪哇 · 苏尔约集团纺织厂 三号线车间    夜 内        天气：闷热，车间内无风\n"
    "\n"
    "人物：班邦（34岁，三号线挡车工）、瓦万（50岁，车间主任）\n"
    "\n"
    "△ 夜班最后一小时。\n"
    "\n"
    "班邦：（急 高声）主任！三号机的罩子还没装回去！\n"
    "\n"
    "1-2\n"
    "\n"
    "场景：日惹 · 哥打格德 阿雅的蜡染作坊 院子    夜 外        天气：晴，闷热，无风\n"
    "\n"
    "阿雅：（平静）喂？……是我。\n"
)


def test_split_heading_becomes_one_canonical_line_and_keeps_the_note():
    result = normalize_screenplay_text(TWO_LINE_EPISODE)

    assert result.changed is True
    assert result.episodes == 1
    assert result.scene_headings == 2
    assert result.stats["split_headings_merged"] == 2
    assert result.stats["notes_preserved"] == 2

    lines = result.text.split("\n")
    assert "1-1 中爪哇 · 苏尔约集团纺织厂 三号线车间 夜 内" in lines
    assert "天气：闷热，车间内无风" in lines
    assert "1-2 日惹 · 哥打格德 阿雅的蜡染作坊 院子 夜 外" in lines
    # 地点名里不允许残留标注与场号行。
    assert not any(
        line.startswith("1-") and "天气：" in line for line in lines
    )
    assert "1-1" not in lines
    # 正文一字不丢。
    assert "班邦：（急 高声）主任！三号机的罩子还没装回去！" in lines
    assert "人物：班邦（34岁，三号线挡车工）、瓦万（50岁，车间主任）" in lines
    assert "第 1 集【事故】" in lines


def test_normalization_is_idempotent():
    once = normalize_screenplay_text(TWO_LINE_EPISODE)
    twice = normalize_screenplay_text(once.text)

    assert twice.changed is False
    assert twice.text == once.text


def test_already_canonical_text_is_byte_identical():
    text = "1-1 急诊部 夜 内\n\n△医生停下。\n\n杜晨：老板，结账。\n"
    result = normalize_screenplay_text(text)

    assert result.text == text
    assert result.changed is False
    assert result.changes == []


def test_inline_heading_note_moves_to_its_own_line():
    text = "1-1 纺织厂 三号线车间 夜 内        天气：空调冷，灯管惨白\n△ 灯管闪了一下。\n"
    result = normalize_screenplay_text(text)

    lines = result.text.split("\n")
    assert lines[0] == "1-1 纺织厂 三号线车间 夜 内"
    assert lines[1] == "天气：空调冷，灯管惨白"
    assert "△ 灯管闪了一下。" in lines
    assert result.stats["headings_rewritten"] == 1


def test_mixed_interior_exterior_collapses_without_inventing_a_time():
    text = "60-3 阿雅作坊 屋里 → 屋外    日 内—外        天气：阴转晴\n"
    result = normalize_screenplay_text(text)

    assert result.text.split("\n")[0] == "60-3 阿雅作坊 屋里 → 屋外 日 内"


def test_prose_novel_is_left_alone():
    text = "第 1 章 起风了\n\n他推开门，风灌进来。\n\n第 2 章 归途\n\n她没说话。\n"
    result = normalize_screenplay_text(text)

    assert result.text == text
    assert result.changed is False
    assert result.episodes == 2
    assert result.scene_headings == 0


def test_dialogue_and_date_lines_are_not_scene_headings():
    text = (
        "1-1 急诊部 夜 内\n"
        "时间：2026 年 3 月 18 日 23:00 前后 · 中爪哇 / 日惹\n"
        "班邦：（急）预报说天气：晴，可走廊还在滴水。\n"
    )
    result = normalize_screenplay_text(text)

    assert result.text == text
    assert result.changed is False


def test_samples_describe_the_change_for_the_ui():
    result = normalize_screenplay_text(TWO_LINE_EPISODE)

    sample = result.samples[0]
    assert sample["kind"] == "merge_split_heading"
    assert sample["line_no"] == 3
    assert "天气：" in str(sample["before"])
    assert sample["after"] == ["1-1 中爪哇 · 苏尔约集团纺织厂 三号线车间 夜 内", "天气：闷热，车间内无风"]


# ── 产物读写 ─────────────────────────────────────────────────────────────────


def test_artifacts_round_trip(tmp_path: Path) -> None:
    assert screenplay_preprocess.has_preprocess_result(tmp_path) is False
    assert screenplay_preprocess.load_preprocess_result(tmp_path) is None
    assert screenplay_preprocess.read_normalized_novel(tmp_path) == ""

    screenplay_preprocess.save_preprocess_result(
        tmp_path, payload={"changed": True}, normalized_text="1-1 车间 夜 内\n"
    )

    assert screenplay_preprocess.has_preprocess_result(tmp_path) is True
    assert screenplay_preprocess.read_normalized_novel(tmp_path) == "1-1 车间 夜 内\n"
    payload = screenplay_preprocess.load_preprocess_result(tmp_path)
    assert payload is not None and payload["changed"] is True
    assert payload["version"] == screenplay_preprocess.RESULT_VERSION

    screenplay_preprocess.clear_preprocess_artifacts(tmp_path)
    assert screenplay_preprocess.has_preprocess_result(tmp_path) is False
    assert screenplay_preprocess.read_normalized_novel(tmp_path) == ""


def test_original_backup_is_written_once(tmp_path: Path) -> None:
    novel = tmp_path / "novel.txt"
    novel.write_text("原始文本", encoding="utf-8")

    first = screenplay_preprocess.backup_original_novel(tmp_path, "原始文本")
    assert first is not None and first.read_text(encoding="utf-8") == "原始文本"

    second = screenplay_preprocess.backup_original_novel(tmp_path, "换过的文本")
    assert second is None
    assert screenplay_preprocess.original_novel_path(tmp_path).read_text(
        encoding="utf-8"
    ) == "原始文本"


def test_load_imported_novel_content_prefers_the_normalized_text(tmp_path: Path) -> None:
    from novelvideo.novel_source import (
        load_imported_novel_content,
        load_raw_imported_novel_content,
        novel_source_digest,
    )

    novel = tmp_path / "novel.txt"
    novel.write_text("1-1\n场景：车间 夜 内\n", encoding="utf-8")
    raw_digest = novel_source_digest(tmp_path)

    screenplay_preprocess.save_preprocess_result(
        tmp_path, payload={}, normalized_text="1-1 车间 夜 内\n"
    )

    assert load_raw_imported_novel_content(tmp_path) == "1-1\n场景：车间 夜 内\n"
    assert load_imported_novel_content(tmp_path) == "1-1 车间 夜 内\n"
    assert novel_source_digest(tmp_path) != raw_digest


# ── 存量项目：分集正文同步 ───────────────────────────────────────────────────


class _FakeStore:
    def __init__(self, contents: dict[int, str]) -> None:
        self.contents = dict(contents)
        self.saved: list[tuple[int, str]] = []
        self.closed = False

    async def load_episode_content(self, number: int):
        return self.contents.get(number)

    async def save_episode_content(self, number: int, content: str) -> None:
        self.contents[number] = content
        self.saved.append((number, content))

    async def close(self) -> None:
        self.closed = True


def _patch_episode_sync(monkeypatch: pytest.MonkeyPatch, store: _FakeStore) -> None:
    from novelvideo.cognee import chapter_detector
    from novelvideo.task_backend.runners import graph_build

    class _Detector:
        def detect(self, text: str):
            # 两份文本各自「识别」出两集，编号一致。
            return [
                SimpleNamespace(number=1, content=text + "\n[ep1]"),
                SimpleNamespace(number=2, content=text + "\n[ep2]"),
            ]

    monkeypatch.setattr(chapter_detector, "ChapterDetector", _Detector)

    async def _load_store(_ctx):
        return store

    monkeypatch.setattr(graph_build, "_load_store", _load_store)


async def test_episode_sync_replaces_untouched_episodes_and_skips_edited_ones(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from novelvideo.task_backend.runners.screenplay_preprocess import (
        _sync_episode_contents,
    )

    store = _FakeStore({1: "原文\n[ep1]", 2: "用户改过的正文"})
    _patch_episode_sync(monkeypatch, store)

    updated, skipped = await _sync_episode_contents(SimpleNamespace(), "原文", "规范文")

    assert (updated, skipped) == (1, 1)
    assert store.saved == [(1, "规范文\n[ep1]")]
    assert store.contents[2] == "用户改过的正文"
    assert store.closed is True


async def test_episode_sync_no_store_when_chapters_do_not_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from novelvideo.cognee import chapter_detector
    from novelvideo.task_backend.runners import graph_build
    from novelvideo.task_backend.runners.screenplay_preprocess import (
        _sync_episode_contents,
    )

    class _NoChapters:
        def detect(self, _text: str):
            return []

    monkeypatch.setattr(chapter_detector, "ChapterDetector", _NoChapters)
    opened: list[bool] = []

    async def _load_store(_ctx):
        opened.append(True)
        return _FakeStore({})

    monkeypatch.setattr(graph_build, "_load_store", _load_store)

    assert await _sync_episode_contents(SimpleNamespace(), "a", "b") == (0, 0)
    assert opened == []
