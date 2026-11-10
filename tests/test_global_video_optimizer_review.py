"""全局视频优化：``review_and_fix`` 的有界并发与就地更新语义。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from novelvideo.agents import global_video_optimizer as gvo


class _FakeReviewAgent:
    """记录在飞峰值；每条请求小睡以便观测并发。"""

    def __init__(self) -> None:
        self.max_in_flight = 0
        self._in_flight = 0
        self.prompts: list[str] = []

    async def run(self, payload):
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            task, _image = payload
            self.prompts.append(task)
            await asyncio.sleep(0.05)
            return SimpleNamespace(
                output=gvo.ReviewResult(needs_fix=True, reason="r", prompt="FIXED")
            )
        finally:
            self._in_flight -= 1


def _optimizer(fake: _FakeReviewAgent) -> gvo.GlobalVideoPromptOptimizer:
    optimizer = gvo.GlobalVideoPromptOptimizer()
    optimizer._review_agent = fake
    optimizer._compress_image = lambda _path, **_kw: b"jpeg"  # type: ignore[method-assign]
    return optimizer


async def test_review_and_fix_runs_concurrently_in_place(tmp_path: Path) -> None:
    for num in (1, 2, 3):
        (tmp_path / f"beat_{num:02d}.png").write_bytes(b"x")

    fake = _FakeReviewAgent()
    optimizer = _optimizer(fake)

    results = [
        {"beat_number": num, "video_mode": "first_frame", "prompt": f"p{num}"}
        for num in (1, 2, 3)
    ]
    beats = [
        {"beat_number": num, "visual_description": f"v{num}", "narration_segment": "n"}
        for num in (1, 2, 3)
    ]

    out = await optimizer.review_and_fix(results, beats, str(tmp_path), {}, log_fn=None)

    # 同一列表就地更新，prompt 被改写。
    assert out is results
    assert [item["prompt"] for item in results] == ["FIXED", "FIXED", "FIXED"]
    # 三个 beat 各审一次。
    assert len(fake.prompts) == 3
    # 默认并发 2 → 至少两个请求同时在飞（旧的串行循环恒为 1）。
    assert fake.max_in_flight >= 2


async def test_review_and_fix_skips_missing_sketch(tmp_path: Path) -> None:
    fake = _FakeReviewAgent()
    optimizer = _optimizer(fake)

    results = [{"beat_number": 1, "video_mode": "first_frame", "prompt": "p1"}]
    beats = [{"beat_number": 1, "visual_description": "v", "narration_segment": "n"}]

    out = await optimizer.review_and_fix(results, beats, str(tmp_path), {}, log_fn=None)

    assert out[0]["prompt"] == "p1"
    assert fake.prompts == []
