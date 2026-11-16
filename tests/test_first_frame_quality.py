"""首帧（render）画质口径：单格出 2K、质量 high。

首帧是视频的第 0 帧，它的分辨率与质量会直接成为整条成片的清晰度上限：1K 首帧喂给
720p 视频模型，模型没有可用细节可参考，成片就是「糊」的。这里把口径钉住，避免以后有人
把单格规格悄悄改回 1K / medium。
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.m09


def test_single_cell_render_modes_use_the_configured_image_size():
    from novelvideo.generators import nanobanana_grid as grid

    mode_keys = {
        grid.SINGLE_CELL_RENDER_MODE_KEY,
        *grid.SINGLE_CELL_RENDER_MODE_BY_ASPECT.values(),
    }
    for mode_key in mode_keys:
        config = grid.REGEN_MODE_CONFIGS[mode_key]
        assert config["image_size"] == grid.SINGLE_CELL_IMAGE_SIZE
        # label 里的规格字样也要跟着走，免得 UI 显示 1K、实际出 2K。
        assert grid.SINGLE_CELL_IMAGE_SIZE in config["label"]


def test_single_cell_image_size_defaults_to_2k():
    from novelvideo.generators.nanobanana_grid import SINGLE_CELL_IMAGE_SIZE

    expected = os.environ.get("DRAMACLAW_SINGLE_CELL_IMAGE_SIZE", "").strip() or "2K"
    assert SINGLE_CELL_IMAGE_SIZE == expected
    assert expected == "2K"


def test_sketch_stays_cheap_while_render_goes_high():
    from novelvideo import config

    # 草图只是给模型看构图的草稿，保持 low；首帧质量默认 high。
    assert config.OPENAI_SKETCH_IMAGE_QUALITY == os.environ.get(
        "OPENAI_SKETCH_IMAGE_QUALITY", "low"
    )
    assert config.OPENAI_IMAGE_QUALITY == os.environ.get("OPENAI_IMAGE_QUALITY", "high")
    assert config.OPENAI_IMAGE_QUALITY == "high"
