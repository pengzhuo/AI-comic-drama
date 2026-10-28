"""AICF 素材库适配器 + 视频生成里的「素材库优先」档位。

上游对 i2v 首帧（``content[0]``）查真人隐私，但把同一张图先登记进素材库再用
``asset://`` 引用就会放行。这里覆盖：登记/轮询/台账复用、不可用时的错误分类
（调用方据此回退降质阶梯）、以及生成器里那一档候选的构造与回退。
"""

import asyncio
import json
from pathlib import Path

import pytest

from novelvideo.storage import asset_library as al
from novelvideo.generators.video_generator import (
    ASSET_LIBRARY_FIRST_FRAME_LOG,
    NewApiVideoGenerator,
)

ACTIVE_PAYLOAD = {
    "id": "asset-20261028122009-ntgtc",
    "uri": "asset://asset-20261028122009-ntgtc",
    "name": "nn_1027_1-frames-ep001-beat_45",
    "group_id": "group-20260923232146-7b275",
    "asset_type": "Image",
    "status": "Active",
}


class _FakeHttp:
    """按序吐出 HTTP 结果；记录每次调用，便于断言「有没有重复登记」。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, *, headers, json_body, timeout):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "json": json_body}
        )
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _client(monkeypatch, responses, *, timeout_seconds=60, clock=None):
    config = al.AssetLibraryConfig(
        base_url="https://aicf.example",
        token="sk-test-token-000000000000000000000000000000000000",
        timeout_seconds=timeout_seconds,
    )
    http = _FakeHttp(responses)
    client = al.AssetLibraryClient(
        config,
        http=http,
        sleep=lambda _seconds: None,
        clock=clock or (lambda: 0.0),
    )
    return client, http


def _write_image(path: Path, payload: bytes = b"\x89PNG\r\n\x1a\nfake") -> str:
    path.write_bytes(payload)
    return str(path)


# --------------------------------------------------------------------------- #
# 配置解析
# --------------------------------------------------------------------------- #


def test_config_without_token_is_not_usable(monkeypatch, tmp_path):
    monkeypatch.setattr("novelvideo.config.AICF_ASSET_BASE_URL", "https://aicf.example")
    monkeypatch.setattr("novelvideo.config.AICF_ASSET_TOKEN", "")
    monkeypatch.setattr("novelvideo.config.AICF_ASSET_LIBRARY_ENABLED", "")
    monkeypatch.setattr(
        "novelvideo.config.AICF_ASSET_LIBRARY_CONFIG_PATH",
        str(tmp_path / "missing.json"),
    )

    config = al.load_asset_library_config()

    assert config.token == ""
    assert not config.usable
    assert not al.asset_library_enabled()


def test_config_reads_env_then_file(monkeypatch, tmp_path):
    config_path = tmp_path / "aicf_asset_library.json"
    config_path.write_text(
        json.dumps(
            {
                "token": "sk-from-file-0000000000000000000000000000000000",
                "enabled": True,
                "timeout_seconds": 7,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("novelvideo.config.AICF_ASSET_TOKEN", "")
    monkeypatch.setattr("novelvideo.config.AICF_ASSET_LIBRARY_ENABLED", "")
    monkeypatch.setattr("novelvideo.config.AICF_ASSET_BASE_URL", "")
    monkeypatch.setattr(
        "novelvideo.config.AICF_ASSET_LIBRARY_CONFIG_PATH", str(config_path)
    )

    config = al.load_asset_library_config()

    assert config.token.startswith("sk-from-file")
    assert config.enabled is True
    assert config.timeout_seconds == 7
    assert config.usable
    # 没配 base_url 时回落到 AICF 官方地址。
    assert config.base_url == al.DEFAULT_BASE_URL


def test_config_env_disables_even_with_a_file_token(monkeypatch, tmp_path):
    config_path = tmp_path / "aicf_asset_library.json"
    config_path.write_text(json.dumps({"token": "sk-file"}), encoding="utf-8")
    monkeypatch.setattr(
        "novelvideo.config.AICF_ASSET_LIBRARY_ENABLED", "0"
    )
    monkeypatch.setattr(
        "novelvideo.config.AICF_ASSET_LIBRARY_CONFIG_PATH", str(config_path)
    )
    monkeypatch.setattr("novelvideo.config.AICF_ASSET_TOKEN", "")
    monkeypatch.setattr("novelvideo.config.AICF_ASSET_BASE_URL", "")

    config = al.load_asset_library_config()

    assert config.enabled is False
    assert not config.usable


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #


def test_register_returns_the_upstream_uri(monkeypatch):
    client, http = _client(monkeypatch, [(200, ACTIVE_PAYLOAD)])

    record = client.register("https://relay.example/beat_45.png", name="beat_45")

    assert record.uri == "asset://asset-20261028122009-ntgtc"
    assert record.group_id == "group-20260923232146-7b275"
    assert record.is_active
    call = http.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://aicf.example/v1/assets"
    assert call["json"] == {"url": "https://relay.example/beat_45.png", "asset_type": "Image", "name": "beat_45"}
    assert call["headers"]["Authorization"].startswith("Bearer sk-test-token")


def test_wait_until_active_polls_processing_to_active(monkeypatch):
    client, http = _client(
        monkeypatch,
        [
            (200, {**ACTIVE_PAYLOAD, "status": "Processing", "uri": ""}),
            (200, ACTIVE_PAYLOAD),
        ],
    )

    record = client.wait_until_active("asset-20261028122009-ntgtc")

    assert record.is_active
    assert [call["method"] for call in http.calls] == ["GET", "GET"]


def test_wait_until_active_times_out_as_unavailable(monkeypatch):
    client, _ = _client(
        monkeypatch,
        [(200, {**ACTIVE_PAYLOAD, "status": "Processing"})],
        timeout_seconds=0,
    )

    with pytest.raises(al.AssetLibraryUnavailable):
        client.wait_until_active("asset-20261028122009-ntgtc")


def test_failed_asset_is_reported_as_registration_failure(monkeypatch):
    client, _ = _client(
        monkeypatch, [(200, {**ACTIVE_PAYLOAD, "status": "Failed"})]
    )

    with pytest.raises(al.AssetRegistrationFailed):
        client.wait_until_active("asset-20261028122009-ntgtc")


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, {"error": {"message": "Invalid token"}}, al.AssetLibraryUnavailable),
        (403, {"error": {"message": "forbidden"}}, al.AssetLibraryUnavailable),
        (503, {"error": {"message": "asset not configured"}}, al.AssetLibraryUnavailable),
        (
            400,
            {"error": {"message": "MissingParameter.Filter.GroupType"}},
            al.AssetRegistrationFailed,
        ),
    ],
)
def test_error_classification_drives_the_fallback(monkeypatch, status, body, expected):
    client, _ = _client(monkeypatch, [(status, body)])

    with pytest.raises(expected):
        client.register("https://relay.example/a.png")


def test_network_failure_is_unavailable(monkeypatch):
    client, _ = _client(monkeypatch, [OSError("connection reset")])

    with pytest.raises(al.AssetLibraryUnavailable):
        client.register("https://relay.example/a.png")


def test_delete_reports_missing_asset(monkeypatch):
    client, _ = _client(monkeypatch, [(404, {"error": {"message": "not found"}})])

    assert client.delete("asset-gone") is False


def test_register_missing_id_is_a_registration_failure(monkeypatch):
    client, _ = _client(monkeypatch, [(200, {"status": "Active"})])

    with pytest.raises(al.AssetRegistrationFailed):
        client.register("https://relay.example/a.png")


# --------------------------------------------------------------------------- #
# 台账 + ensure_asset_uri
# --------------------------------------------------------------------------- #


def test_source_key_tracks_content_not_path(tmp_path):
    first = _write_image(tmp_path / "a.png", b"same-bytes")
    second = _write_image(tmp_path / "b.png", b"same-bytes")
    other = _write_image(tmp_path / "c.png", b"different")

    assert al.source_key(first) == al.source_key(second)
    assert al.source_key(first) != al.source_key(other)
    assert al.source_key("https://cdn.example/x.png").startswith("ref:")


def test_ensure_asset_uri_registers_once_then_reuses_the_ledger(tmp_path, monkeypatch):
    image = _write_image(tmp_path / "beat_45.png", b"frame-bytes")
    ledger = al.AssetLibraryLedger(tmp_path / "asset_library.jsonl")
    client, http = _client(monkeypatch, [(200, ACTIVE_PAYLOAD)])
    logs: list[str] = []

    first = al.ensure_asset_uri(
        image,
        public_url="https://relay.example/beat_45.png",
        name="beat_45",
        log=logs.append,
        ledger=ledger,
        client=client,
    )
    # 第二次：换一个 client（模拟新进程），http 一次都不该再打。
    reused_client, reused_http = _client(monkeypatch, [])
    second = al.ensure_asset_uri(
        image,
        public_url="https://relay.example/beat_45.png",
        name="beat_45",
        log=logs.append,
        ledger=ledger,
        client=reused_client,
    )

    assert first == second == "asset://asset-20261028122009-ntgtc"
    assert len(http.calls) == 1
    assert reused_http.calls == []
    assert any("台账命中" in line for line in logs)
    assert any("已登记素材库" in line for line in logs)


def test_ensure_asset_uri_requires_configuration(tmp_path, monkeypatch):
    image = _write_image(tmp_path / "beat_45.png")
    ledger = al.AssetLibraryLedger(tmp_path / "asset_library.jsonl")
    client, _ = _client(monkeypatch, [])
    client.config = al.AssetLibraryConfig(base_url="https://aicf.example", token="")

    with pytest.raises(al.AssetLibraryUnavailable):
        al.ensure_asset_uri(image, public_url="https://relay.example/a.png", client=client, ledger=ledger)


def test_ledger_ignores_failed_records(tmp_path):
    ledger = al.AssetLibraryLedger(tmp_path / "asset_library.jsonl")
    key = "sha256:deadbeef"
    ledger.record(
        key,
        al.AssetRecord(asset_id="a1", uri="asset://a1", status="Failed"),
    )

    assert ledger.lookup(key) is None


def test_list_ledger_assets_lists_every_registration(tmp_path):
    ledger = al.AssetLibraryLedger(tmp_path / "asset_library.jsonl")
    ledger.record("sha256:1", al.AssetRecord(asset_id="a1", uri="asset://a1", status="Active"))
    ledger.record("sha256:2", al.AssetRecord(asset_id="a2", uri="asset://a2", status="Active"))

    items = al.list_ledger_assets(ledger)

    assert [item["asset_id"] for item in items] == ["a1", "a2"]


# --------------------------------------------------------------------------- #
# 生成器里的「素材库优先」档位
# --------------------------------------------------------------------------- #


def _asset_candidate(monkeypatch, *, log, wire=None):
    async def fake_relay(image_value, *, default_ext="png"):
        return "https://relay.example/beat_45.png"

    monkeypatch.setattr(
        NewApiVideoGenerator, "_relay_frame_input", staticmethod(fake_relay)
    )
    metadata = {"image_url": "https://relay.example/beat_45.png", "ratio": "9:16"}
    payload = {"prompt": "本镜只使用 @图片1", "metadata": dict(metadata)}
    return NewApiVideoGenerator._asset_library_first_frame_candidate_factory(
        image_path="/data/output/local/nn_1027_1/frames/ep001/beat_45.png",
        asset_name="nn_1027_1-frames-ep001-beat_45",
        payload=payload,
        metadata=metadata,
        wire=wire or (lambda candidate, meta: candidate),
        log=log,
    )


def test_asset_candidate_rewrites_the_first_frame_to_the_asset_uri(monkeypatch, tmp_path):
    monkeypatch.setattr(
        al,
        "ensure_asset_uri",
        lambda source, **kwargs: "asset://asset-20261028122009-ntgtc",
    )
    logs: list[str] = []
    build = _asset_candidate(monkeypatch, log=logs.append)

    body = asyncio.run(build())

    assert body["metadata"]["image_url"] == "asset://asset-20261028122009-ntgtc"
    # 提示词与其它参数原样保留。
    assert body["prompt"] == "本镜只使用 @图片1"
    assert body["metadata"]["ratio"] == "9:16"


def test_asset_candidate_falls_back_to_the_original_frame(monkeypatch):
    def boom(source, **kwargs):
        raise al.AssetLibraryUnavailable("素材库未配置")

    monkeypatch.setattr(al, "ensure_asset_uri", boom)
    logs: list[str] = []
    build = _asset_candidate(monkeypatch, log=logs.append)

    body = asyncio.run(build())

    assert body["metadata"]["image_url"] == "https://relay.example/beat_45.png"
    assert any("素材库登记不可用" in line for line in logs)


def test_asset_name_is_derived_from_the_frame_path():
    name = NewApiVideoGenerator._asset_name_for_frame(
        "/data/output/local/nn_1027_1/frames/ep001/beat_45.png"
    )

    assert name == "nn_1027_1-frames-ep001-beat_45"


def test_asset_name_survives_paths_without_an_extension():
    # ``Path(name).suffix`` 为空串时 ``endswith("")`` 恒真，早期实现会把整段名字吞掉。
    assert (
        NewApiVideoGenerator._asset_name_for_frame(
            "/data/output/local/nn_1027_1/frames/ep001/beat_45"
        )
        == "nn_1027_1-frames-ep001-beat_45"
    )
    assert NewApiVideoGenerator._asset_name_for_frame("") == "first-frame"


def test_asset_library_rung_sits_before_the_quality_ladder(monkeypatch):
    """素材库档位必须排在降质阶梯之前：保首帧、保画质。"""

    generator = NewApiVideoGenerator.__new__(NewApiVideoGenerator)
    calls: list[dict] = []

    async def fake_post_json(url, body, *, headers=None):
        calls.append(body)
        if len(calls) == 1:
            from novelvideo.generators.video_generator import NewApiVideoError

            raise NewApiVideoError(
                "The input image 'content[0]' may contain real person. "
                "(InputImageSensitiveContentDetected.PrivacyInformation)",
                status_code=400,
            )
        return {"task_id": "t1"}

    monkeypatch.setattr(generator, "_post_json", fake_post_json)

    monkeypatch.setattr(
        al,
        "ensure_asset_uri",
        lambda source, **kwargs: "asset://asset-20261028122009-ntgtc",
    )

    async def fake_relay(image_value, *, default_ext="png"):
        return "https://relay.example/beat_45.png"

    monkeypatch.setattr(
        NewApiVideoGenerator, "_relay_frame_input", staticmethod(fake_relay)
    )

    metadata = {"image_url": "https://relay.example/beat_45.png"}
    payload = {"prompt": "p", "metadata": dict(metadata)}
    ladder_built: list[str] = []

    async def ladder_rung():
        ladder_built.append("ladder")
        return {"prompt": "p", "metadata": {"image_url": "https://relay.example/blur.png"}}

    logs: list[str] = []
    submitted, used = asyncio.run(
        generator._submit_video_candidates(
            "http://gw/video/generations",
            [
                {"prompt": "p", "metadata": dict(metadata)},
                NewApiVideoGenerator._asset_library_first_frame_candidate_factory(
                    image_path="/data/output/local/nn_1027_1/frames/ep001/beat_45.png",
                    asset_name="beat_45",
                    payload=payload,
                    metadata=metadata,
                    wire=lambda candidate, meta: candidate,
                    log=logs.append,
                ),
                ladder_rung,
            ],
            labels=["首帧原图", ASSET_LIBRARY_FIRST_FRAME_LOG, "首帧高斯模糊 2.5"],
            on_log=logs.append,
        )
    )

    assert submitted == {"task_id": "t1"}
    assert used["metadata"]["image_url"] == "asset://asset-20261028122009-ntgtc"
    # 素材库过关后，降质档位根本没被构造。
    assert ladder_built == []
    assert any(ASSET_LIBRARY_FIRST_FRAME_LOG in line for line in logs)


def _privacy_error():
    from novelvideo.generators.video_generator import NewApiVideoError

    return NewApiVideoError(
        "The input image 'content[0]' may contain real person. "
        "(InputImageSensitiveContentDetected.PrivacyInformation)",
        status_code=400,
    )


def test_asset_rung_falls_through_when_the_gateway_does_not_accept_asset_uri(monkeypatch):
    """网关/上游给出**别的** 4xx（例如不认 asset://）时，必须继续降级而不是打死整镜。"""

    from novelvideo.generators.video_generator import NewApiVideoError

    generator, candidates, ladder_built, logs = _chain_with_responses(
        monkeypatch,
        [
            _privacy_error(),
            NewApiVideoError(
                'HTTP 400: {"code":"InvalidParameter","message":"image url is invalid"}',
                status_code=400,
            ),
        ],
    )

    submitted, used = asyncio.run(
        generator._submit_video_candidates(
            "http://gw/video/generations",
            candidates,
            labels=["首帧原图", ASSET_LIBRARY_FIRST_FRAME_LOG, "首帧高斯模糊 2.5"],
            on_log=logs.append,
        )
    )

    assert submitted == {"task_id": "t1"}
    assert used["metadata"]["image_url"] == "https://relay.example/blur.png"
    assert ladder_built == ["ladder"]
    assert any("继续降级重试" in line for line in logs)


def _chain_with_responses(monkeypatch, responses):
    """返回 (generator, candidates, ladder_built, logs)，提交响应按 ``responses`` 依次给出。

    最后一档之后一律返回成功响应，方便断言「降级到了哪一档」。
    """

    generator = NewApiVideoGenerator.__new__(NewApiVideoGenerator)
    calls: list[dict] = []
    ladder_built: list[str] = []
    logs: list[str] = []
    outcomes = list(responses) + [{"task_id": "t1"}]

    async def fake_post_json(url, body, *, headers=None):
        calls.append(body)
        outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(generator, "_post_json", fake_post_json)
    monkeypatch.setattr(
        al, "ensure_asset_uri", lambda source, **kwargs: "asset://asset-probe"
    )

    async def fake_relay(image_value, *, default_ext="png"):
        return "https://relay.example/beat_45.png"

    monkeypatch.setattr(
        NewApiVideoGenerator, "_relay_frame_input", staticmethod(fake_relay)
    )

    metadata = {"image_url": "https://relay.example/beat_45.png"}
    payload = {"prompt": "p", "metadata": dict(metadata)}
    asset_rung = NewApiVideoGenerator._asset_library_first_frame_candidate_factory(
        image_path="/data/output/local/nn_1027_1/frames/ep001/beat_45.png",
        asset_name="beat_45",
        payload=payload,
        metadata=metadata,
        wire=lambda candidate, meta: candidate,
        log=logs.append,
    )

    async def ladder_rung():
        ladder_built.append("ladder")
        return {
            "prompt": "p",
            "metadata": {"image_url": "https://relay.example/blur.png"},
        }

    return (
        generator,
        [{"prompt": "p", "metadata": dict(metadata)}, asset_rung, ladder_rung],
        ladder_built,
        logs,
    )


def test_asset_rung_survives_a_server_error(monkeypatch):
    from novelvideo.generators.video_generator import NewApiVideoError

    generator, candidates, ladder_built, logs = _chain_with_responses(
        monkeypatch,
        [
            _privacy_error(),
            NewApiVideoError("HTTP 503: upstream busy", status_code=503),
        ],
    )

    submitted, used = asyncio.run(
        generator._submit_video_candidates(
            "http://gw/video/generations",
            candidates,
            labels=["首帧原图", ASSET_LIBRARY_FIRST_FRAME_LOG, "首帧高斯模糊 2.5"],
            on_log=logs.append,
        )
    )

    assert submitted == {"task_id": "t1"}
    assert used["metadata"]["image_url"] == "https://relay.example/blur.png"
    assert ladder_built == ["ladder"]


def test_plain_rung_still_raises_on_a_non_privacy_rejection(monkeypatch):
    """对照：普通档位（非锦上添花）遇到别的 4xx 仍然直接抛出，不被这次改动放宽。"""

    from novelvideo.generators.video_generator import NewApiVideoError

    generator = NewApiVideoGenerator.__new__(NewApiVideoGenerator)
    calls: list[dict] = []

    async def fake_post_json(url, body, *, headers=None):
        calls.append(body)
        raise NewApiVideoError(
            'HTTP 400: {"code":"InvalidParameter","message":"ratio must be one of"}',
            status_code=400,
        )

    monkeypatch.setattr(generator, "_post_json", fake_post_json)

    async def ladder_rung():
        return {"prompt": "p", "metadata": {"image_url": "https://relay.example/x.png"}}

    with pytest.raises(NewApiVideoError):
        asyncio.run(
            generator._submit_video_candidates(
                "http://gw/video/generations",
                [{"prompt": "p"}, ladder_rung],
                labels=["首帧原图", "首帧高斯模糊 2.5"],
                on_log=lambda _m: None,
            )
        )


def _plain_input_rejection():
    """线上应用通路的真实形态：只剩光杆 ``InputImageSensitiveContentDetected``。"""

    from novelvideo.generators.video_generator import NewApiVideoError

    return NewApiVideoError(
        'HTTP 400: {"code":"fail_to_fetch_task","message":"{\\"code\\":'
        '\\"InputImageSensitiveContentDetected\\",\\"message\\":'
        '\\"The request failed because the input image \'content[0]\' may '
        'contain sensitive information.\\"}"}',
        status_code=400,
    )


def test_plain_input_rejection_still_reaches_the_asset_rung(monkeypatch):
    """这类拒绝在线上最常见；素材库档必须挨得到，否则整条兜底是死代码。"""

    generator, candidates, ladder_built, logs = _chain_with_responses(
        monkeypatch, [_plain_input_rejection()]
    )

    submitted, used = asyncio.run(
        generator._submit_video_candidates(
            "http://gw/video/generations",
            candidates,
            labels=["首帧原图", ASSET_LIBRARY_FIRST_FRAME_LOG, "首帧高斯模糊 2.5"],
            on_log=logs.append,
        )
    )

    assert submitted == {"task_id": "t1"}
    assert used["metadata"]["image_url"] == "asset://asset-probe"
    # 素材库过关：降质阶梯一次都不该被构造（保首帧、保画质）。
    assert ladder_built == []
    assert any("改用素材库 asset:// 引用重试" in line for line in logs)


def test_plain_input_rejection_does_not_degrade_after_the_asset_rung(
    monkeypatch,
):
    """素材库档自己也被判「输入图敏感」时收手：糊一版并不能让内容变得不敏感。"""

    from novelvideo.generators.video_generator import NewApiVideoError

    generator, candidates, ladder_built, logs = _chain_with_responses(
        monkeypatch, [_plain_input_rejection(), _plain_input_rejection()]
    )

    with pytest.raises(NewApiVideoError) as excinfo:
        asyncio.run(
            generator._submit_video_candidates(
                "http://gw/video/generations",
                candidates,
                labels=["首帧原图", ASSET_LIBRARY_FIRST_FRAME_LOG, "首帧高斯模糊 2.5"],
                on_log=logs.append,
            )
        )

    assert "素材库 asset:// 档也没被接受" in str(excinfo.value)
    assert ladder_built == []


def test_plain_input_rejection_raises_without_an_asset_rung(monkeypatch):
    """素材库没开/没有素材库档时行为不变：直接抛出，不再白烧降质。"""

    from novelvideo.generators.video_generator import NewApiVideoError

    generator = NewApiVideoGenerator.__new__(NewApiVideoGenerator)
    ladder_built: list[str] = []

    async def fake_post_json(url, body, *, headers=None):
        raise _plain_input_rejection()

    monkeypatch.setattr(generator, "_post_json", fake_post_json)

    async def ladder_rung():
        ladder_built.append("ladder")
        return {"prompt": "p", "metadata": {"image_url": "https://relay.example/b.png"}}

    with pytest.raises(NewApiVideoError):
        asyncio.run(
            generator._submit_video_candidates(
                "http://gw/video/generations",
                [{"prompt": "p"}, ladder_rung],
                labels=["首帧原图", "首帧高斯模糊 2.5"],
                on_log=lambda _m: None,
            )
        )

    assert ladder_built == []
