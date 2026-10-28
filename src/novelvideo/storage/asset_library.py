"""AICF 素材库适配器：真人输入图先登记，再用 ``asset://`` 引用。

背景
----
上游 Ark 对 i2v 的**首帧位**（``content[0]``）单独做真人隐私审核：首帧里只要有可
识别的真人脸就 4xx 拒绝::

    InputImageSensitiveContentDetected.PrivacyInformation
    the input image 'content[0]' may contain real person

``reference_images`` 槽位**不做**这项审核 —— 这正是旧通路只能「把脸糊掉 / 逐档下采样」
硬闯、或者把首帧挪到参考位（换来画面漂移）的原因。

2026-10-27/28 线上实测（``nn_1027_1`` / 第 1 集 Beat 45，同一张首帧 render、同一提示词、
前后相差几分钟）::

    first_frame = 媒体中继直链        -> 400 InputImageSensitiveContentDetected.PrivacyInformation
    first_frame = asset://asset-...   -> 200 通过（严格首帧锁定，画质零损失）

同一次事故里这张首帧在原通路上被输入档拒了 21 次（模糊 2.5 / 384 / 320 / 256 / 组合 /
模糊 6 / 模糊 10 全档位），所以素材库不是「锦上添花」，而是把降质阶梯整体替换掉。

登记实测约 2 秒到 ``Active``（文档写的预处理上限 2 小时对渲染图不成立），没有人工审核
中间态。因此本模块把「登记 → 轮询 Active → 台账复用」收成一个入口。

硬约束
------
``asset://`` 是**账号级**素材：登记用的 token 必须与「真正生成视频那条 channel 的上游
key」属于同一个 AICF 账号，否则登记出来的素材在生成请求里不可见（白登记）。见
``docs/agents/asset-library.md``。

配置
----
优先级：进程环境变量 > JSON 配置文件。

环境变量（``config.AICF_ASSET_*``）::

    AICF_ASSET_BASE_URL            默认 https://aicf.nanie168.com
    AICF_ASSET_TOKEN               Authorization: Bearer <token>
    AICF_ASSET_LIBRARY_ENABLED     1/0；留空 = 有 token 就启用
    AICF_ASSET_LIBRARY_CONFIG_PATH 配置文件路径，默认 <STATE_DIR>/aicf_asset_library.json
    AICF_ASSET_TIMEOUT_SECONDS     轮询 Active 的上限，默认 60

配置文件（便于不重建容器就开关/换 token）::

    {"enabled": true, "base_url": "...", "token": "sk-...", "timeout_seconds": 60}
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: 上游素材引用前缀。
ASSET_URI_PREFIX = "asset://"

DEFAULT_BASE_URL = "https://aicf.nanie168.com"
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_POLL_INTERVAL_SECONDS = 2.0

ACTIVE_STATUS = "active"
FAILED_STATUSES = frozenset({"failed", "error"})

_TRUTHY = frozenset({"1", "true", "yes", "on", "enabled"})
_FALSY = frozenset({"0", "false", "no", "off", "disabled"})


class AssetLibraryError(RuntimeError):
    """素材库相关错误的基类。"""


class AssetLibraryUnavailable(AssetLibraryError):
    """未配置、鉴权失败、网络或上游 5xx —— 调用方应当回退既有通路。"""


class AssetRegistrationFailed(AssetLibraryError):
    """登记请求被上游按业务拒绝（非鉴权/非 5xx），重试同一张图没有意义。"""


@dataclass(frozen=True)
class AssetLibraryConfig:
    """素材库连接配置。``enabled`` 为 ``None`` 时按「有没有 token」推断。"""

    base_url: str = ""
    token: str = ""
    enabled: Optional[bool] = None
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

    @property
    def usable(self) -> bool:
        if self.enabled is False:
            return False
        return bool(self.base_url.strip()) and bool(self.token.strip())


@dataclass(frozen=True)
class AssetRecord:
    """一条上游素材记录（字段缺失时留空，不臆造）。"""

    asset_id: str
    uri: str
    status: str = ""
    group_id: str = ""
    source_url: str = ""
    name: str = ""
    created_at: float = 0.0

    @property
    def is_active(self) -> bool:
        return self.status.strip().lower() == ACTIVE_STATUS

    @property
    def is_failed(self) -> bool:
        return self.status.strip().lower() in FAILED_STATUSES

    @classmethod
    def from_payload(
        cls,
        payload: Any,
        *,
        source_url: str = "",
        name: str = "",
    ) -> "AssetRecord":
        data = payload if isinstance(payload, dict) else {}
        nested = data.get("data")
        if isinstance(nested, dict):
            data = {**data, **nested}
        asset_id = str(data.get("id") or data.get("asset_id") or "").strip()
        uri = str(data.get("uri") or "").strip()
        if not uri and asset_id:
            uri = f"{ASSET_URI_PREFIX}{asset_id}"
        created = data.get("create_time") or data.get("created_at") or 0
        try:
            created_at = float(created or 0)
        except (TypeError, ValueError):
            created_at = 0.0
        return cls(
            asset_id=asset_id,
            uri=uri,
            status=str(data.get("status") or "").strip(),
            group_id=str(data.get("group_id") or "").strip(),
            source_url=source_url or str(data.get("url") or "").strip(),
            name=name or str(data.get("name") or "").strip(),
            created_at=created_at,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "uri": self.uri,
            "status": self.status,
            "group_id": self.group_id,
            "source_url": self.source_url,
            "name": self.name,
            "created_at": self.created_at,
        }

    @classmethod
    def from_json(cls, payload: Any) -> "AssetRecord":
        data = payload if isinstance(payload, dict) else {}
        created = data.get("created_at") or 0
        try:
            created_at = float(created or 0)
        except (TypeError, ValueError):
            created_at = 0.0
        return cls(
            asset_id=str(data.get("asset_id") or "").strip(),
            uri=str(data.get("uri") or "").strip(),
            status=str(data.get("status") or "").strip(),
            group_id=str(data.get("group_id") or "").strip(),
            source_url=str(data.get("source_url") or "").strip(),
            name=str(data.get("name") or "").strip(),
            created_at=created_at,
        )


def _parse_tristate(value: Any) -> Optional[bool]:
    text = str(value if value is not None else "").strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return None


def asset_library_config_path() -> Path:
    """素材库配置文件路径（env 覆盖 > ``<STATE_DIR>/aicf_asset_library.json``）。"""

    from novelvideo import config as app_config

    override = str(getattr(app_config, "AICF_ASSET_LIBRARY_CONFIG_PATH", "") or "").strip()
    if override:
        return Path(override)
    state_dir = str(getattr(app_config, "STATE_DIR", "") or "").strip() or "."
    return Path(state_dir) / "aicf_asset_library.json"


def load_asset_library_config() -> AssetLibraryConfig:
    """读素材库配置。环境变量优先，其次 JSON 配置文件；都读不到就是「未配置」。"""

    from novelvideo import config as app_config

    base_url = str(getattr(app_config, "AICF_ASSET_BASE_URL", "") or "").strip()
    token = str(getattr(app_config, "AICF_ASSET_TOKEN", "") or "").strip()
    enabled = _parse_tristate(getattr(app_config, "AICF_ASSET_LIBRARY_ENABLED", ""))
    timeout_seconds = int(
        getattr(app_config, "AICF_ASSET_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
        or DEFAULT_TIMEOUT_SECONDS
    )

    path = asset_library_config_path()
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("素材库配置文件无法解析：%s（%s）", path, exc)
            raw = None
        if isinstance(raw, dict):
            base_url = base_url or str(raw.get("base_url") or "").strip()
            token = token or str(raw.get("token") or "").strip()
            if enabled is None:
                enabled = _parse_tristate(raw.get("enabled"))
            try:
                file_timeout = int(raw.get("timeout_seconds") or 0)
            except (TypeError, ValueError):
                file_timeout = 0
            if file_timeout > 0:
                timeout_seconds = file_timeout

    return AssetLibraryConfig(
        base_url=base_url or DEFAULT_BASE_URL,
        token=token,
        enabled=enabled,
        timeout_seconds=max(1, timeout_seconds),
    )


def asset_library_enabled() -> bool:
    """素材库是否可用（配置齐 + 未被显式关闭）。"""

    return load_asset_library_config().usable


def asset_library_ledger_path() -> Path:
    """台账路径（与素材库配置文件同目录，便于一起备份/清理）。"""

    from novelvideo import config as app_config

    state_dir = str(getattr(app_config, "STATE_DIR", "") or "").strip() or "."
    override = str(getattr(app_config, "AICF_ASSET_LIBRARY_LEDGER_PATH", "") or "").strip()
    return Path(override) if override else Path(state_dir) / "asset_library.jsonl"


@dataclass
class AssetLibraryLedger:
    """``源图标识 -> asset://`` 台账。

    一台账一行 JSONL，**只追加**：同一张图重投时直接复用已登记的素材，既省一次登记，
    也避免把同一个素材在账号里堆很多份。源图内容变了（hash 变了）自然落成新行。
    """

    path: Path
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def lookup(self, key: str) -> Optional[AssetRecord]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("素材库台账读取失败：%s（%s）", self.path, exc)
            return None
        latest: Optional[AssetRecord] = None
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or str(entry.get("key") or "") != key:
                continue
            latest = AssetRecord.from_json(entry.get("asset"))
        if latest is None or not latest.uri or latest.is_failed:
            return None
        return latest

    def record(self, key: str, record: AssetRecord) -> None:
        entry = {
            "key": key,
            "recorded_at": time.time(),
            "asset": record.to_json(),
        }
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError as exc:
                logger.warning("素材库台账写入失败：%s（%s）", self.path, exc)

    def entries(self) -> list[dict[str, Any]]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        items: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                items.append(entry)
        return items


HttpCall = Callable[..., tuple[int, Any]]


def _httpx_call(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json_body: Any = None,
    timeout: float = 30.0,
) -> tuple[int, Any]:
    import httpx

    with httpx.Client(timeout=timeout) as client:
        response = client.request(method, url, headers=headers, json=json_body)
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text
        return response.status_code, body


class AssetLibraryClient:
    """``/v1/assets`` 的最小客户端（登记 / 查询 / 轮询 / 删除）。"""

    def __init__(
        self,
        config: AssetLibraryConfig,
        *,
        http: Optional[HttpCall] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._http = http or _httpx_call
        self._sleep = sleep
        self._clock = clock

    @property
    def _base(self) -> str:
        return self.config.base_url.strip().rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.token.strip()}",
            "Content-Type": "application/json",
        }

    def _call(self, method: str, path: str, json_body: Any = None) -> tuple[int, Any]:
        try:
            return self._http(
                method,
                f"{self._base}{path}",
                headers=self._headers(),
                json_body=json_body,
                timeout=float(self.config.timeout_seconds),
            )
        except AssetLibraryError:
            raise
        except Exception as exc:  # 网络/DNS/超时：都按「素材库不可用」处理
            raise AssetLibraryUnavailable(f"素材库请求失败：{method} {path}: {exc}") from exc

    @staticmethod
    def _message(status: int, body: Any, fallback: str) -> str:
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])
            if body.get("message"):
                return str(body["message"])
        text = str(body or "").strip()
        return text[:300] if text else fallback

    def _raise_for_status(self, status: int, body: Any, action: str) -> None:
        if 200 <= status < 300:
            return
        message = self._message(status, body, f"HTTP {status}")
        if status in (401, 403, 408, 429) or status >= 500:
            # 鉴权/限流/服务端问题：换一张图也未必好，交给上层回退并等人处理。
            raise AssetLibraryUnavailable(f"素材库{action}不可用（HTTP {status}）：{message}")
        raise AssetRegistrationFailed(f"素材库{action}被拒（HTTP {status}）：{message}")

    def register(self, url: str, *, name: str = "", asset_type: str = "Image") -> AssetRecord:
        """用公网 HTTP(S) URL 登记素材，返回上游记录（状态多为 ``Processing``）。"""

        payload: dict[str, Any] = {"url": str(url or "").strip(), "asset_type": asset_type}
        if name:
            payload["name"] = name
        status, body = self._call("POST", "/v1/assets", payload)
        self._raise_for_status(status, body, "登记")
        record = AssetRecord.from_payload(body, source_url=payload["url"], name=name)
        if not record.asset_id:
            raise AssetRegistrationFailed(f"素材库登记返回缺少 id：{str(body)[:200]}")
        return record

    def get(self, asset_id: str) -> AssetRecord:
        status, body = self._call("GET", f"/v1/assets/{asset_id}")
        self._raise_for_status(status, body, "查询")
        return AssetRecord.from_payload(body)

    def delete(self, asset_id: str) -> bool:
        status, body = self._call("DELETE", f"/v1/assets/{asset_id}")
        if status in (404,):
            return False
        self._raise_for_status(status, body, "删除")
        return True

    def wait_until_active(
        self,
        asset_id: str,
        *,
        timeout: Optional[float] = None,
        interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> AssetRecord:
        """轮询到 ``Active``。实测渲染图约 2 秒；超时按「不可用」抛出，让上层回退。"""

        deadline = self._clock() + float(
            timeout if timeout is not None else self.config.timeout_seconds
        )
        record = AssetRecord(asset_id=asset_id, uri=f"{ASSET_URI_PREFIX}{asset_id}")
        while True:
            record = self.get(asset_id)
            if record.is_active:
                return record
            if record.is_failed:
                raise AssetRegistrationFailed(
                    f"素材库素材处理失败：{asset_id}（status={record.status}）"
                )
            if self._clock() >= deadline:
                raise AssetLibraryUnavailable(
                    f"素材库素材未在时限内就绪：{asset_id}（status={record.status}）"
                )
            self._sleep(interval)

    def ensure_asset(self, url: str, *, name: str = "") -> AssetRecord:
        """登记并等到 ``Active``，返回可引用的 ``asset://`` 记录。"""

        record = self.register(url, name=name)
        if record.is_active:
            return record
        active = self.wait_until_active(record.asset_id)
        return AssetRecord(
            asset_id=active.asset_id or record.asset_id,
            uri=active.uri or record.uri,
            status=active.status,
            group_id=active.group_id or record.group_id,
            source_url=record.source_url,
            name=record.name,
            created_at=active.created_at or record.created_at,
        )


def source_key(source: str) -> str:
    """源图标识：本地文件按内容 sha256，远程/内联按字符串 sha256。"""

    text = str(source or "").strip()
    try:
        path = Path(text)
        is_file = path.is_file()
    except OSError:
        is_file = False
    if is_file:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return f"sha256:{digest}"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"ref:{digest}"


def _default_public_url(source: str) -> str:
    text = str(source or "").strip()
    if text.startswith(("http://", "https://")):
        return text
    from novelvideo.storage.media_relay import upload_image_file

    return upload_image_file(text)


def ensure_asset_uri(
    source: str,
    *,
    public_url: str = "",
    name: str = "",
    log: Optional[Callable[[str], None]] = None,
    ledger: Optional[AssetLibraryLedger] = None,
    client: Optional[AssetLibraryClient] = None,
) -> str:
    """把一张图登记成上游素材，返回 ``asset://`` URI（命中台账则不重复登记）。

    ``source`` 可以是本地路径或公网 URL；``public_url`` 是上游能抓到的地址
    （本地路径没给时会走媒体中继上传）。任何失败都抛 :class:`AssetLibraryError`，
    调用方据此回退既有通路。
    """

    def note(message: str) -> None:
        if log is not None:
            log(message)
        else:
            logger.info("%s", message)

    config = client.config if client is not None else load_asset_library_config()
    if not config.usable:
        raise AssetLibraryUnavailable(
            "素材库未配置（缺 AICF_ASSET_TOKEN 或 AICF_ASSET_LIBRARY_ENABLED=0）"
        )

    key = source_key(source)
    book = ledger if ledger is not None else AssetLibraryLedger(asset_library_ledger_path())
    cached = book.lookup(key)
    if cached is not None:
        note(f"素材库台账命中，复用 {cached.uri}")
        return cached.uri

    fetch_url = str(public_url or "").strip() or _default_public_url(source)
    api = client if client is not None else AssetLibraryClient(config)
    asset_name = str(name or "").strip() or f"frame-{key.split(':', 1)[-1][:12]}"
    record = api.ensure_asset(fetch_url, name=asset_name)
    if not record.uri:
        raise AssetRegistrationFailed(f"素材库登记未返回 uri：{record.asset_id}")
    book.record(key, record)
    note(f"首帧已登记素材库：{record.uri}（{asset_name}）")
    return record.uri


def list_ledger_assets(ledger: Optional[AssetLibraryLedger] = None) -> list[dict[str, Any]]:
    """列出台账里登记过的素材（上游 ``GET /v1/assets`` 列表接口当前不可用）。"""

    book = ledger if ledger is not None else AssetLibraryLedger(asset_library_ledger_path())
    items: list[dict[str, Any]] = []
    for entry in book.entries():
        asset = entry.get("asset")
        record = AssetRecord.from_json(asset)
        if not record.asset_id:
            continue
        items.append(
            {
                "key": str(entry.get("key") or ""),
                "recorded_at": float(entry.get("recorded_at") or 0),
                **record.to_json(),
            }
        )
    return items


def _main(argv: Optional[list[str]] = None) -> int:
    """轻量运维入口：``python -m novelvideo.storage.asset_library``。"""

    import argparse

    parser = argparse.ArgumentParser(description="AICF 素材库台账与配置排查")
    parser.add_argument("--list", action="store_true", help="列出本地台账中的素材")
    parser.add_argument("--status", action="store_true", help="打印当前配置（token 只显示前后几位）")
    parser.add_argument("--delete", metavar="ASSET_ID", help="从上游删除一个素材")
    args = parser.parse_args(argv)

    if args.status or not (args.list or args.delete):
        config = load_asset_library_config()
        token = config.token
        preview = f"{token[:6]}...{token[-4:]}" if len(token) > 12 else ("(未配置)" if not token else "(过短)")
        print(f"base_url      : {config.base_url}")
        print(f"token         : {preview}（{len(token)} 位）")
        print(f"enabled       : {config.enabled}")
        print(f"usable        : {config.usable}")
        print(f"config_path   : {asset_library_config_path()}")
        print(f"ledger_path   : {asset_library_ledger_path()}")
        if not args.list and not args.delete:
            return 0

    if args.list:
        items = list_ledger_assets()
        print(f"台账素材：{len(items)} 条")
        for item in items:
            print(
                f"  {item['asset_id']}  {item['status']:<12} "
                f"{item['name'] or '-'}  {item['uri']}"
            )

    if args.delete:
        api = AssetLibraryClient(load_asset_library_config())
        removed = api.delete(args.delete)
        print(f"删除 {args.delete}：{'ok' if removed else 'not found'}")
    return 0


if __name__ == "__main__":  # pragma: no cover - 运维入口
    raise SystemExit(_main())
