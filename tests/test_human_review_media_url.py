"""``human_review`` 的媒体 URL 供给：OSS 预签名不可用时回退媒体中继。

本部署只配了 ``OSS_RELAY_*``（中继桶），没有 ``OSS_ENDPOINT/OSS_BUCKET/
OSS_ACCESS_KEY_*``，所以 :func:`presign_or_upload_output` 恒返回 ``None``。若不回退，
``human_review=True`` 的请求会直接抛「requires OSS presigned HTTP media URLs」，
真人素材审核这条路等于断的。
"""

import pytest

from novelvideo.generators.video_generator import HuimengVideoGenerator


def _generator():
    return HuimengVideoGenerator.__new__(HuimengVideoGenerator)


def test_human_review_prefers_the_oss_presigned_url(tmp_path, monkeypatch):
    image = tmp_path / "frame.png"
    image.write_bytes(b"frame")
    monkeypatch.setattr(
        "novelvideo.utils.oss_client.presign_or_upload_output",
        lambda path, **kwargs: "https://oss.example/frame.png",
    )
    relayed = []
    monkeypatch.setattr(
        "novelvideo.storage.media_relay.upload_media_bytes",
        lambda *args, **kwargs: relayed.append(args) or "https://relay.example/frame.png",
    )
    logs = []

    url = _generator()._to_upload_url(
        str(image), label="角色参考", log=logs.append, require_http_url=True
    )

    assert url == "https://oss.example/frame.png"
    assert relayed == []
    assert any("OSS URL" in line for line in logs)


def test_human_review_falls_back_to_the_media_relay(tmp_path, monkeypatch):
    image = tmp_path / "frame.png"
    image.write_bytes(b"frame")
    monkeypatch.setattr(
        "novelvideo.utils.oss_client.presign_or_upload_output",
        lambda path, **kwargs: None,
    )
    captured = {}

    def fake_upload_media_bytes(data, *, ext="png", resource_type="image", **kwargs):
        captured.update(data=data, ext=ext, resource_type=resource_type)
        return "https://relay.example/frame.png"

    monkeypatch.setattr(
        "novelvideo.storage.media_relay.upload_media_bytes", fake_upload_media_bytes
    )
    logs = []

    url = _generator()._to_upload_url(
        str(image), label="角色参考", log=logs.append, require_http_url=True
    )

    assert url == "https://relay.example/frame.png"
    assert captured == {"data": b"frame", "ext": "png", "resource_type": "image"}
    assert any("媒体中继" in line for line in logs)


def test_human_review_still_fails_when_no_transport_works(tmp_path, monkeypatch):
    image = tmp_path / "frame.png"
    image.write_bytes(b"frame")
    monkeypatch.setattr(
        "novelvideo.utils.oss_client.presign_or_upload_output",
        lambda path, **kwargs: None,
    )
    monkeypatch.setattr(
        "novelvideo.storage.media_relay.upload_media_bytes",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("relay down")),
    )

    with pytest.raises(ValueError, match="requires HTTP/HTTPS media URLs"):
        _generator()._to_upload_url(
            str(image), label="角色参考", log=lambda _m: None, require_http_url=True
        )


def test_video_media_uses_the_video_resource_type(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"video")
    monkeypatch.setattr(
        "novelvideo.utils.oss_client.presign_or_upload_output",
        lambda path, **kwargs: None,
    )
    captured = {}

    def fake_upload_media_bytes(data, *, ext="png", resource_type="image", **kwargs):
        captured.update(ext=ext, resource_type=resource_type)
        return "https://relay.example/clip.mp4"

    monkeypatch.setattr(
        "novelvideo.storage.media_relay.upload_media_bytes", fake_upload_media_bytes
    )

    url = _generator()._to_upload_url(
        str(clip), label="参考视频", log=lambda _m: None, require_http_url=True
    )

    assert url == "https://relay.example/clip.mp4"
    assert captured == {"ext": "mp4", "resource_type": "video"}
