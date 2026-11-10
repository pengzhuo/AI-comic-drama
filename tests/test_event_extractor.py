"""EventExtractor.extract_all_chapters 的有界并发与保序。"""

from __future__ import annotations

import asyncio

import pytest

from novelvideo.cognee.event_extractor import EventExtractor
from novelvideo.models import NovelEvent


def _event(chapter_num: int) -> NovelEvent:
    return NovelEvent(
        event_id=f"ch{chapter_num}_e1",
        chapter_num=chapter_num,
        description=f"d{chapter_num}",
        content=f"c{chapter_num}",
    )


class _FakeExtractor(EventExtractor):
    """记录在飞峰值；每条请求小睡以便观测并发。"""

    def __init__(self) -> None:
        self.max_in_flight = 0
        self._in_flight = 0
        self.seen: list[int] = []

    async def extract_events(self, chapter_num, chapter_content, on_log=None):
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            self.seen.append(chapter_num)
            await asyncio.sleep(0.03)
            return [_event(chapter_num)]
        finally:
            self._in_flight -= 1


async def test_extract_all_chapters_is_concurrent_and_ordered(monkeypatch):
    monkeypatch.setenv("STRUCTURED_LLM_CONCURRENCY", "4")
    extractor = _FakeExtractor()
    chapters = [{"number": n, "content": f"content {n}"} for n in (1, 2, 3, 4)]

    events = await extractor.extract_all_chapters(chapters)

    # 保序：合并顺序与原串行循环一致。
    assert [event.chapter_num for event in events] == [1, 2, 3, 4]
    # 旧的串行循环恒为 1；有界并发应 >= 2。
    assert extractor.max_in_flight >= 2


async def test_extract_all_chapters_reraises_first_error(monkeypatch):
    monkeypatch.setenv("STRUCTURED_LLM_CONCURRENCY", "4")

    class _Boom(EventExtractor):
        async def extract_events(self, chapter_num, chapter_content, on_log=None):
            if chapter_num == 2:
                raise RuntimeError("boom")
            return []

    with pytest.raises(RuntimeError):
        await _Boom().extract_all_chapters(
            [{"number": 1, "content": "a"}, {"number": 2, "content": "b"}]
        )
