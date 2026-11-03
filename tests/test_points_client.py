"""漫剧伴点预扣客户端（``points_client``）。"""

from __future__ import annotations

import json

import pytest

from novelvideo import points_client


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    points_client._CACHE.clear()
    monkeypatch.delenv(points_client.ENV_CONFIG_PATH, raising=False)
    yield
    points_client._CACHE.clear()


def _config(tmp_path, **overrides) -> points_client.PointsConfig:
    path = tmp_path / "dopoints.json"
    payload = {
        "enabled": True,
        "base_url": "https://example.test/api/aistory/dopoints.php",
        "sign_key": "test-sign-key",
        "points_per_cny": 100,
        "timeout_seconds": 3,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return points_client._read_config(path)


# ---------------------------------------------------------------------------
# 签名 / 计价
# ---------------------------------------------------------------------------


def test_sign_is_md5_phone_plus_key_lowercase_hex():
    # 黄金向量：md5("13800138000" + "test-sign-key")，小写 hex。
    assert points_client.sign("13800138000", "test-sign-key") == (
        "551dbaefada4a3e48ab80b8b7653c6d1"
    )


def test_points_for_cny_rounds_to_nearest_point():
    assert points_client.points_for_cny(230.81) == 23081
    assert points_client.points_for_cny(0.001) == 0
    assert points_client.points_for_cny(None) == 0


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def test_load_reads_config_file(tmp_path, monkeypatch):
    path = tmp_path / "dopoints.json"
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "base_url": "https://example.test/dopoints.php",
                "sign_key": "abc",
                "points_per_cny": 200,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(points_client.ENV_CONFIG_PATH, str(path))
    config = points_client.load()
    assert config.enabled is True
    assert config.base_url == "https://example.test/dopoints.php"
    assert config.sign_key == "abc"
    assert config.points_per_cny == 200
    assert config.configured is True


def test_load_without_file_is_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv(points_client.ENV_CONFIG_PATH, str(tmp_path / "missing.json"))
    config = points_client.load()
    assert config.enabled is False
    # 缺省地址仍指向漫剧伴点接口；密钥回落到 studio 的 client_secret。
    assert config.base_url == points_client.DEFAULT_BASE_URL


def test_load_hot_reloads_on_mtime_change(tmp_path, monkeypatch):
    path = tmp_path / "dopoints.json"
    monkeypatch.setenv(points_client.ENV_CONFIG_PATH, str(path))
    path.write_text(json.dumps({"enabled": True, "sign_key": "a"}), encoding="utf-8")
    assert points_client.load().sign_key == "a"
    path.write_text(json.dumps({"enabled": True, "sign_key": "b"}), encoding="utf-8")
    import os

    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
    assert points_client.load().sign_key == "b"


# ---------------------------------------------------------------------------
# operate()
# ---------------------------------------------------------------------------


def _client(config, transport):
    return points_client.PointsClient(config=config, transport=transport)


def test_operate_posts_form_with_sign(tmp_path):
    seen: dict = {}

    def transport(method, url, headers, body):
        seen.update({"method": method, "url": url, "headers": headers, "body": body})
        return 200, json.dumps({"code": 0, "message": "操作成功", "data": []})

    config = _config(tmp_path)
    result = _client(config, transport).charge(
        phone="13800138000", amount=23081, projects_uuid="01M5TESTPROJECT0000000000"
    )
    assert result["code"] == 0
    assert seen["method"] == "POST"
    assert seen["url"] == config.base_url
    assert seen["body"] == {
        "phone": "13800138000",
        "amount": "23081",
        "optype": "1",
        "projectsuuid": "01M5TESTPROJECT0000000000",
        "sign": "551dbaefada4a3e48ab80b8b7653c6d1",
    }


def test_operate_business_failure_raises_not_enough_with_message(tmp_path):
    def transport(method, url, headers, body):
        return 200, json.dumps(
            {"code": 1, "message": "伴点不足，请先充值", "data": None}
        )

    with pytest.raises(points_client.PointsNotEnough) as excinfo:
        _client(_config(tmp_path), transport).charge(
            phone="13800138000", amount=100, projects_uuid="p1"
        )
    assert str(excinfo.value) == "伴点不足，请先充值"


def test_operate_http_error_raises_unavailable(tmp_path):
    def transport(method, url, headers, body):
        return 503, "<html>bad gateway</html>"

    with pytest.raises(points_client.PointsUnavailable):
        _client(_config(tmp_path), transport).charge(
            phone="13800138000", amount=100, projects_uuid="p1"
        )


def test_operate_network_error_raises_unavailable(tmp_path):
    def transport(method, url, headers, body):
        return 0, json.dumps({"error": "timeout"})

    with pytest.raises(points_client.PointsUnavailable):
        _client(_config(tmp_path), transport).charge(
            phone="13800138000", amount=100, projects_uuid="p1"
        )


def test_operate_non_json_raises_unavailable(tmp_path):
    def transport(method, url, headers, body):
        return 200, "not json"

    with pytest.raises(points_client.PointsUnavailable):
        _client(_config(tmp_path), transport).charge(
            phone="13800138000", amount=100, projects_uuid="p1"
        )


def test_operate_without_sign_key_raises_unavailable():
    config = points_client.PointsConfig(
        enabled=True, base_url="https://example.test/dopoints.php", sign_key=""
    )

    def transport(method, url, headers, body):  # pragma: no cover - 不该被调用
        raise AssertionError("unconfigured client must not touch the network")

    with pytest.raises(points_client.PointsUnavailable):
        _client(config, transport).charge(
            phone="13800138000", amount=100, projects_uuid="p1"
        )


def test_refund_uses_add_optype(tmp_path):
    seen: dict = {}

    def transport(method, url, headers, body):
        seen.update(body)
        return 200, json.dumps({"code": 0, "message": "操作成功", "data": []})

    _client(_config(tmp_path), transport).refund(
        phone="13800138000", amount=100, projects_uuid="p1"
    )
    assert seen["optype"] == "2"
