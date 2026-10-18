"""画幅整链一致性：草图 / 首帧 / 视频三段必须同画幅。

回归的是「2:3 草图 → 2:3 首帧 → 9:16 视频」这条错配链：视频模型要自己把
2:3 的首帧补成 9:16 画布，补出来的部分经常变成凭空长出的身体/衣物。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from novelvideo.api.routes.generation_helpers import _single_render_mode_from_sketch
from novelvideo.generators.nanobanana_grid import (
    DEFAULT_SKETCH_ASPECT_RATIO,
    REGEN_MODE_CONFIGS,
    SKETCH_DEFAULT_MODE_KEY,
    get_sketch_default_mode_key,
    get_sketch_nxn_modes,
)


def _make_sketch(root: Path, episode: int, beat: int, size: tuple[int, int]) -> None:
    directory = root / "sketches" / f"ep{episode:03d}"
    directory.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size).save(directory / f"beat_{beat:02d}.png")


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((720, 1280), "1x1_9-16"),
        ((1200, 1800), "1x1_2-3"),
        ((1920, 1080), "1x1_16-9"),
    ],
)
def test_single_render_mode_follows_the_sketch_aspect(
    tmp_path: Path, size: tuple[int, int], expected: str
) -> None:
    _make_sketch(tmp_path, 2, 1, size)

    assert _single_render_mode_from_sketch(str(tmp_path), 2, [1]) == expected


def test_single_render_mode_tolerates_a_near_9_16_sketch(tmp_path: Path) -> None:
    """绘图工具常见的 ±2% 尺寸偏差不能把 9:16 草图判成 2:3。"""

    _make_sketch(tmp_path, 2, 1, (1080, 1900))

    assert _single_render_mode_from_sketch(str(tmp_path), 2, [1]) == "1x1_9-16"


def test_single_render_mode_returns_none_without_a_readable_sketch(tmp_path: Path) -> None:
    directory = tmp_path / "sketches" / "ep002"
    directory.mkdir(parents=True)
    (directory / "beat_01.png").write_bytes(b"not-an-image")

    assert _single_render_mode_from_sketch(str(tmp_path), 2, [1]) is None


def test_sketch_default_aspect_is_vertical_video() -> None:
    assert DEFAULT_SKETCH_ASPECT_RATIO == "9:16"
    assert SKETCH_DEFAULT_MODE_KEY == "5x5_9-16_sketch"
    assert REGEN_MODE_CONFIGS[SKETCH_DEFAULT_MODE_KEY]["aspect_ratio"] == "9:16"


def test_sketch_default_mode_key_falls_back_when_the_aspect_is_absent() -> None:
    assert get_sketch_default_mode_key("9:16") == "5x5_9-16_sketch"
    assert get_sketch_default_mode_key("4:5") == "5x5_2-3_sketch"


def test_every_sketch_grid_size_has_a_9_16_variant() -> None:
    modes = get_sketch_nxn_modes("9:16")

    assert [capacity for capacity, *_ in modes] == [1, 4, 9, 16, 25]
    assert all(
        REGEN_MODE_CONFIGS[mode_key]["aspect_ratio"] == "9:16"
        for _, mode_key, *_ in modes
    )
