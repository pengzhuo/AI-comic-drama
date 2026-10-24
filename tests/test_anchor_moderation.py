"""首帧真人隐私审核的降级阶梯。

上游对 i2v 首帧（``content[0]``）查真人、对参考图槽位不查，所以「多参考兜底」能
过审却会漂移。这里验证阶梯：原图 → 模糊 → 逐档下采样，且低档位是惰性的。
"""

import asyncio
import json

import pytest
from PIL import Image

from novelvideo.generators import anchor_moderation as am
from novelvideo.generators.video_generator import (
    FALLBACK_MULTI_REFERENCE_LOG,
    NewApiVideoError,
    NewApiVideoGenerator,
)


@pytest.fixture
def anchor_png(tmp_path):
    path = tmp_path / "beat_01.png"
    Image.new("RGB", (1024, 1536), (40, 60, 90)).save(path)
    return str(path)


PRIVACY_REJECTION = (
    "The request failed because the input image 'content[0]' may contain "
    "real person. (InputImageSensitiveContentDetected.PrivacyInformation)"
)


def test_privacy_rejection_matches_the_upstream_error_string():
    assert am.is_privacy_information_rejection(PRIVACY_REJECTION)
    assert not am.is_privacy_information_rejection(
        "InputImageSensitiveContentDetected.Violence"
    )


def test_extract_upstream_code_unwraps_newapi_nesting():
    inner = {
        "error": {"code": "InputImageSensitiveContentDetected.PrivacyInformation"}
    }
    middle = json.dumps(
        {"code": "fail_to_fetch_task", "message": json.dumps(inner)}
    )
    outer = json.dumps({"code": "fail_to_fetch_task", "message": middle})
    text = f"HTTP 400: {outer}"

    assert (
        am.extract_upstream_code(text)
        == "InputImageSensitiveContentDetected.PrivacyInformation"
    )
    assert am.is_privacy_information_rejection(text)
    assert am.describe_upstream_rejection(text).startswith("真人隐私审核")


def test_describe_upstream_rejection_surfaces_non_privacy_codes():
    assert (
        am.describe_upstream_rejection(
            'HTTP 400: {"code":"invalid_parameter","message":"ratio must be one of"}'
        )
        == "invalid_parameter"
    )
    # 只有外层包装码时退回原文摘要，而不是谎报一个没有信息量的 code。
    plain = 'HTTP 400: {"code":"fail_to_fetch_task","message":"boom"}'
    assert "fail_to_fetch_task" in am.describe_upstream_rejection(plain)


def test_extract_upstream_code_falls_back_to_escaped_json():
    # 真实线上报文是「JSON 里再放 JSON 字符串」的多层转义，未必能被 json.loads
    # 一次吃下；正则回退必须同样认得 \\"code\\" 这种形态。
    escaped = (
        'HTTP 400: {"code":"fail_to_fetch_task","message":"{\\"code\\":'
        '\\"fail_to_fetch_task\\",\\"message\\":\\"{\\\\\\"error\\\\\\":'
        '{\\\\\\"code\\\\\\":\\\\\\"InputImageSensitiveContentDetected.'
        'PrivacyInformation\\\\\\"}}\\"}"}'
    )

    assert (
        am.extract_upstream_code(escaped)
        == "InputImageSensitiveContentDetected.PrivacyInformation"
    )
    assert am.is_privacy_information_rejection(escaped)


def test_plan_keeps_the_original_first_and_loses_quality_gradually(anchor_png):
    variants = am.plan_anchor_variants(anchor_png)

    assert variants[0].is_original
    assert [v.kind for v in variants[1:]] == [
        am.BLUR_KIND,
        am.DOWNSCALE_KIND,
        am.DOWNSCALE_KIND,
        am.DOWNSCALE_KIND,
        am.BLUR_DOWNSCALE_KIND,
        am.BLUR_KIND,
        am.BLUR_KIND,
    ]
    downscales = [v.width for v in variants if v.kind == am.DOWNSCALE_KIND]
    assert downscales == [384, 320, 256]
    # 模糊档半径递增：先是 2.5 的轻糊，末尾是两档更强的重糊。
    blurs = [v.blur_radius for v in variants if v.kind == am.BLUR_KIND]
    assert blurs == [am.GAUSSIAN_BLUR_RADIUS, *am.HEAVY_BLUR_RADII]


def test_plan_drops_downscale_rungs_wider_than_the_source(tmp_path):
    path = tmp_path / "small.png"
    Image.new("RGB", (200, 300), (0, 0, 0)).save(path)

    variants = am.plan_anchor_variants(str(path))

    # 图比最宽的下采样档还窄：下采样档（含组合档）全被剔除，只剩模糊档。
    assert [v.kind for v in variants] == [
        am.ORIGINAL_KIND,
        am.BLUR_KIND,
        am.BLUR_KIND,
        am.BLUR_KIND,
    ]
    assert not [v for v in variants if v.width]


def test_plan_has_no_transform_rungs_for_remote_or_inline_sources():
    assert len(am.plan_anchor_variants("https://cdn.example/beat.png")) == 1
    assert len(am.plan_anchor_variants("data:image/png;base64,AAAA")) == 1


def test_materialize_original_does_not_copy(anchor_png):
    original = am.AnchorVariant(kind=am.ORIGINAL_KIND, label="首帧原图")

    assert am.materialize_anchor_variant(anchor_png, original) == anchor_png


def test_materialize_blur_keeps_size_but_writes_a_new_file(anchor_png):
    variant = am.plan_anchor_variants(anchor_png)[1]

    produced = am.materialize_anchor_variant(anchor_png, variant)

    assert produced != anchor_png
    with Image.open(anchor_png) as source, Image.open(produced) as blurred:
        assert blurred.size == source.size
        # 模糊确实改变了像素（纯色图也一样：边缘被平滑后仍全同，但写出的文件独立）。
        assert blurred.getpixel((0, 0)) is not None


def test_materialize_downscale_limits_the_long_edge_proportionally(anchor_png):
    variant = next(
        v
        for v in am.plan_anchor_variants(anchor_png)
        if v.kind == am.DOWNSCALE_KIND and v.width == 320
    )

    produced = am.materialize_anchor_variant(anchor_png, variant)

    with Image.open(produced) as scaled:
        assert scaled.size == (320, 480)


def test_materialize_blur_downscale_applies_both_transforms(anchor_png):
    variant = next(
        v
        for v in am.plan_anchor_variants(anchor_png)
        if v.kind == am.BLUR_DOWNSCALE_KIND
    )

    produced = am.materialize_anchor_variant(anchor_png, variant)

    with Image.open(produced) as combo:
        assert combo.size == (320, 480)


def _stub_generator(monkeypatch, responses):
    """Build a ``NewApiVideoGenerator`` whose ``_post_json`` follows ``responses``."""

    generator = NewApiVideoGenerator.__new__(NewApiVideoGenerator)
    calls: list[dict] = []

    async def fake_post_json(url, body, *, headers=None):
        calls.append(body)
        outcome = responses[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(generator, "_post_json", fake_post_json)
    return generator, calls


def test_submit_candidates_only_materializes_a_lazy_rung_when_reached(monkeypatch):
    generator, calls = _stub_generator(
        monkeypatch,
        [NewApiVideoError(PRIVACY_REJECTION, status_code=400), {"task_id": "t1"}],
    )
    built: list[str] = []

    async def _lazy():
        built.append("built")
        return {"prompt": "blurred anchor"}

    submitted, payload = asyncio.run(
        generator._submit_video_candidates(
            "http://gw/video/generations",
            [{"prompt": "original anchor"}, _lazy],
            labels=["首帧原图", "首帧高斯模糊 2.5"],
        )
    )

    assert submitted == {"task_id": "t1"}
    assert payload == {"prompt": "blurred anchor"}
    assert built == ["built"]
    assert calls == [{"prompt": "original anchor"}, {"prompt": "blurred anchor"}]


def test_submit_candidates_skips_lazy_rungs_once_the_first_wins(monkeypatch):
    generator, calls = _stub_generator(monkeypatch, [{"task_id": "t1"}])
    built: list[str] = []

    async def _lazy():
        built.append("built")
        return {"prompt": "blurred anchor"}

    submitted, payload = asyncio.run(
        generator._submit_video_candidates(
            "http://gw/video/generations",
            [{"prompt": "original anchor"}, _lazy],
            labels=["首帧原图", "首帧高斯模糊 2.5"],
        )
    )

    assert submitted == {"task_id": "t1"}
    assert payload == {"prompt": "original anchor"}
    assert built == []
    assert len(calls) == 1


def test_submit_candidates_reports_which_rung_was_used(monkeypatch):
    generator, _ = _stub_generator(
        monkeypatch,
        [NewApiVideoError(PRIVACY_REJECTION, status_code=400), {"task_id": "t1"}],
    )
    logs: list[str] = []

    asyncio.run(
        generator._submit_video_candidates(
            "http://gw/video/generations",
            [{"prompt": "original"}, {"prompt": "blurred"}],
            labels=["首帧原图", "首帧高斯模糊 2.5"],
            on_log=logs.append,
        )
    )

    assert any("首帧原图被上游拒绝" in line for line in logs)
    assert any("首帧高斯模糊 2.5" in line for line in logs)
    # 降级日志必须带上游原因，而不是只有一句 HTTP 400。
    assert any("真人隐私审核" in line for line in logs)


def test_submit_candidates_keeps_the_multi_reference_fallback_label(monkeypatch):
    generator, _ = _stub_generator(
        monkeypatch,
        [
            NewApiVideoError(PRIVACY_REJECTION, status_code=400),
            NewApiVideoError(PRIVACY_REJECTION, status_code=400),
            {"task_id": "t1"},
        ],
    )
    logs: list[str] = []

    asyncio.run(
        generator._submit_video_candidates(
            "http://gw/video/generations",
            [{"prompt": "original"}, {"prompt": "blurred"}, {"prompt": "multi-ref"}],
            labels=["首帧原图", "首帧高斯模糊 2.5", FALLBACK_MULTI_REFERENCE_LOG],
            on_log=logs.append,
        )
    )

    assert any(FALLBACK_MULTI_REFERENCE_LOG in line for line in logs)
    # 兜底档本身已是整句，前面不该再叠一层「已按兜底方案提交：」。
    assert any(
        line.startswith("已降级采用：首帧改为参考图1") for line in logs
    )
    assert not any("已降级采用：已按兜底" in line for line in logs)


def test_submit_candidates_raises_when_the_last_rung_is_rejected(monkeypatch):
    generator, _ = _stub_generator(
        monkeypatch,
        [NewApiVideoError(PRIVACY_REJECTION, status_code=400)] * 2,
    )

    with pytest.raises(NewApiVideoError) as excinfo:
        asyncio.run(
            generator._submit_video_candidates(
                "http://gw/video/generations",
                [{"prompt": "original"}, {"prompt": "blurred"}],
                labels=["首帧原图", "首帧高斯模糊 2.5"],
            )
        )

    assert excinfo.value.status_code == 400


def test_submit_candidates_raises_immediately_on_unrelated_client_error(monkeypatch):
    generator, calls = _stub_generator(
        monkeypatch,
        [NewApiVideoError('HTTP 400: {"code":"invalid_parameter"}', status_code=400)],
    )
    logs: list[str] = []

    with pytest.raises(NewApiVideoError, match="invalid_parameter"):
        asyncio.run(
            generator._submit_video_candidates(
                "http://gw/video/generations",
                [{"prompt": "original"}, {"prompt": "blurred"}],
                labels=["首帧原图", "首帧高斯模糊 2.5"],
                on_log=logs.append,
            )
        )

    # 非隐私 4xx 既不该烧掉后续档位，也不该伪装成首帧审核降级。
    assert len(calls) == 1
    assert not any("自动降级重试" in line for line in logs)
