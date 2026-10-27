"""中转站（上游 NewAPI 中继）真实账单读取。

捏驿网关是我们自己跑的 NewAPI 中继，它的 ``options.ModelRatio`` 里没有
``seedance-2.0-fast`` / ``LingShan-G2`` / ``DC-*-LLM`` 这些别名，于是落到默认
倍率计费，算出来的 ``quota`` 比上游实际收费高 30~200 倍。要让「捏账」与中转站
一致，只能直接向真正收钱的上游取账单：

- 任务（视频）：``GET /api/task/self`` 分页，每页 100。
- 通用日志（图片 / 音频 / 文本）：``GET /api/log/self``，按 ``request_id``
  精确查询（一次一条，命中即返回 1 条）。

设计约定
--------
- **只读**：本模块绝不写中转站。
- 凭据只用于 CLI 运行，不落库、不进 git。
- HTTP 传输可注入（``transport``），单测不触网。
- 中转站 access token 只有 15 分钟，遇到 401 自动重登一次再重试。
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import urlencode

logger = logging.getLogger("novelvideo.relay_billing")

#: ``transport(method, url, headers, json_body) -> (status, text)``
Transport = Callable[[str, str, dict[str, str], "dict[str, Any] | None"], "tuple[int, str]"]

#: 中转站分页上限就是 100；要继续翻页只能加 ``p``。
PAGE_SIZE = 100
#: 分页保险丝，防止上游返回异常数据时死循环。
MAX_PAGES = 5000

DEFAULT_RELAY_BASE_URLS = (
    "https://aicf.nanie168.com",
    "https://aigc.nanie168.com",
)


class RelayBillingError(RuntimeError):
    """中转站账单读取失败。"""


@dataclass(frozen=True)
class RelayEndpoint:
    """一个中转站地址 + 账号。``configured`` 为假时调用方应跳过。"""

    base_url: str
    username: str = ""
    password: str = ""
    label: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", str(self.base_url or "").strip().rstrip("/"))
        object.__setattr__(self, "username", str(self.username or "").strip())
        object.__setattr__(
            self, "label", str(self.label or "").strip() or self.base_url
        )

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.username and self.password)


@dataclass
class RelayStatus:
    """中转站 ``/api/status`` 里与计价/展示相关的字段。"""

    quota_per_unit: float = 0.0
    usd_exchange_rate: float = 0.0
    currency_symbol: str = ""
    display_in_currency: bool = False


@dataclass
class RelayBillingCache:
    """中转站账单缓存（``task_id`` / ``request_id`` → quota）。"""

    path: Path | None = None
    tasks: dict[str, int] = field(default_factory=dict)
    logs: dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path | None) -> "RelayBillingCache":
        cache = cls(path=Path(path) if path else None)
        if not cache.path or not cache.path.is_file():
            return cache
        try:
            raw = json.loads(cache.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 缓存坏了就重新拉，不影响对账
            logger.warning("中转站账单缓存不可读，忽略: %s", cache.path)
            return cache
        tasks = raw.get("tasks") if isinstance(raw, dict) else None
        logs = raw.get("logs") if isinstance(raw, dict) else None
        if isinstance(tasks, dict):
            cache.tasks = _as_quota_map(tasks)
        if isinstance(logs, dict):
            cache.logs = _as_quota_map(logs)
        return cache

    def save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"tasks": self.tasks, "logs": self.logs}),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 - 缓存只是加速，写失败不报错
            logger.warning("中转站账单缓存写入失败: %s", self.path)


@dataclass
class RelayQuotaSnapshot:
    """一次抓取的汇总结果（key 都是**上游**单据号）。"""

    tasks: dict[str, int] = field(default_factory=dict)
    logs: dict[str, int] = field(default_factory=dict)
    statuses: list[RelayStatus] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _as_quota_map(raw: dict[Any, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, value in raw.items():
        clean = str(key or "").strip()
        if not clean:
            continue
        try:
            out[clean] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def _load_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001 - 上游可能返回 HTML 错误页
        return None


def _items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        raw = data.get("items")
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


_http_client: Any = None
_http_lock = threading.Lock()


def _default_transport(
    method: str, url: str, headers: dict[str, str], payload: dict[str, Any] | None
) -> tuple[int, str]:
    """httpx 实现；网络异常折成 ``status=0``，交由调用方按 HTTP 错误处理。"""
    global _http_client
    try:
        import httpx
    except Exception as exc:  # pragma: no cover - httpx 是硬依赖
        return 0, json.dumps({"error": f"httpx unavailable: {exc}"})
    with _http_lock:
        if _http_client is None:
            _http_client = httpx.Client(timeout=30.0, follow_redirects=True)
    try:
        response = _http_client.request(method, url, headers=headers, json=payload)
        return response.status_code, response.text
    except Exception as exc:  # noqa: BLE001
        return 0, json.dumps({"error": str(exc)})


class RelayBillingClient:
    """单个中转站的账单读取客户端（同步，可多线程共用）。"""

    def __init__(
        self,
        endpoint: RelayEndpoint,
        *,
        transport: Transport | None = None,
        page_size: int = PAGE_SIZE,
    ) -> None:
        self.endpoint = endpoint
        self._transport: Transport = transport or _default_transport
        self.page_size = max(1, min(int(page_size or PAGE_SIZE), PAGE_SIZE))
        self._token = ""
        self._token_lock = threading.Lock()

    # ------------------------------------------------------------------ auth
    def login(self, *, force: bool = False) -> str:
        with self._token_lock:
            if self._token and not force:
                return self._token
            if not self.endpoint.configured:
                raise RelayBillingError(f"{self.endpoint.label}: 缺少中转站账号")
            url = self.endpoint.base_url + "/api/user/login"
            status, text = self._transport(
                "POST",
                url,
                {"Content-Type": "application/json", "Accept": "application/json"},
                {
                    "username": self.endpoint.username,
                    "password": self.endpoint.password,
                },
            )
            payload = _load_json(text)
            data = payload.get("data") if isinstance(payload, dict) else None
            token = str((data or {}).get("access_token") or "") if isinstance(data, dict) else ""
            if status != 200 or not token:
                raise RelayBillingError(
                    f"{self.endpoint.label}: 登录失败 HTTP {status} {text[:160]}"
                )
            self._token = token
            return token

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> Any:
        url = self.endpoint.base_url + path
        if params:
            url = f"{url}?{urlencode(params)}"
        for attempt in (0, 1):
            headers = {"Accept": "application/json"}
            if auth:
                headers["Authorization"] = f"Bearer {self.login()}"
            if body is not None:
                headers["Content-Type"] = "application/json"
            status, text = self._transport(method, url, headers, body)
            payload = _load_json(text)
            if auth and attempt == 0 and status == 401:
                # access token 只有 15 分钟，过期就重登一次再试。
                self.login(force=True)
                continue
            if status != 200:
                raise RelayBillingError(
                    f"{self.endpoint.label}: {path} HTTP {status} {text[:160]}"
                )
            if isinstance(payload, dict) and payload.get("success") is False:
                raise RelayBillingError(
                    f"{self.endpoint.label}: {path} {str(payload.get('message'))[:160]}"
                )
            if isinstance(payload, dict) and "data" in payload:
                return payload["data"]
            return payload
        raise RelayBillingError(f"{self.endpoint.label}: {path} 重试后仍失败")

    # ---------------------------------------------------------------- public
    def status(self) -> RelayStatus:
        """中转站的计价/展示口径（``quota_per_unit``、美元汇率、币种）。"""
        data = self._call("GET", "/api/status", auth=False)
        if not isinstance(data, dict):
            return RelayStatus()
        return RelayStatus(
            quota_per_unit=_as_float(data.get("quota_per_unit")),
            usd_exchange_rate=_as_float(data.get("usd_exchange_rate")),
            currency_symbol=str(data.get("custom_currency_symbol") or ""),
            display_in_currency=bool(data.get("display_in_currency")),
        )

    def task_quotas(self) -> dict[str, int]:
        """全部任务日志的 ``task_id → quota``（分页）。"""
        quotas: dict[str, int] = {}
        for page in range(MAX_PAGES):
            data = self._call(
                "GET",
                "/api/task/self",
                params={"p": page, "page_size": self.page_size},
            )
            items = _items(data)
            for item in items:
                task_id = str(item.get("task_id") or "").strip()
                if task_id:
                    quotas[task_id] = _as_int(item.get("quota"))
            if len(items) < self.page_size:
                break
        return quotas

    def log_quota(self, request_id: str) -> int | None:
        """单条通用日志的真实 quota；查不到返回 ``None``。"""
        data = self._call(
            "GET",
            "/api/log/self",
            params={"p": 0, "page_size": 1, "request_id": request_id},
        )
        for item in _items(data):
            if str(item.get("request_id") or "").strip() == request_id:
                return _as_int(item.get("quota"))
        return None

    def log_quotas_for(
        self,
        request_ids: Iterable[str],
        *,
        cache: RelayBillingCache | None = None,
        workers: int = 4,
        progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, int]:
        """批量取 ``request_id → quota``；命中缓存的直接跳过。"""
        wanted = [rid for rid in dict.fromkeys(str(x or "").strip() for x in request_ids) if rid]
        found: dict[str, int] = {}
        pending: list[str] = []
        for request_id in wanted:
            cached = cache.logs.get(request_id) if cache is not None else None
            if cached is None:
                pending.append(request_id)
            else:
                found[request_id] = cached
        if pending:

            def _one(request_id: str) -> tuple[str, int | None]:
                try:
                    return request_id, self.log_quota(request_id)
                except RelayBillingError as exc:
                    logger.warning("中转站日志查询失败: %s", exc)
                    return request_id, None

            with ThreadPoolExecutor(max_workers=max(1, int(workers or 1))) as pool:
                for index, (request_id, quota) in enumerate(pool.map(_one, pending), start=1):
                    if quota is not None:
                        found[request_id] = quota
                    if progress is not None and (index % 50 == 0 or index == len(pending)):
                        progress(index, len(pending))
        if cache is not None:
            cache.logs.update(found)
        return found


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def build_endpoints(
    base_urls: Sequence[str] | None,
    *,
    username: str,
    password: str,
) -> list[RelayEndpoint]:
    """按地址顺序去重构造端点；地址为空时用内置的两个上游。"""
    urls = [
        str(url or "").strip()
        for url in (base_urls or DEFAULT_RELAY_BASE_URLS)
        if str(url or "").strip()
    ]
    return [
        RelayEndpoint(base_url=url, username=username, password=password)
        for url in dict.fromkeys(urls)
    ]


def collect_quotas(
    endpoints: Sequence[RelayEndpoint],
    *,
    upstream_task_ids: Iterable[str] = (),
    upstream_log_ids: Iterable[str] = (),
    cache: RelayBillingCache | None = None,
    transport: Transport | None = None,
    workers: int = 4,
    progress: Callable[[int, int], None] | None = None,
) -> RelayQuotaSnapshot:
    """逐个上游抓任务 + 日志账单，合并成一张 ``上游单据号 → quota`` 表。"""
    snapshot = RelayQuotaSnapshot()
    if cache is not None:
        snapshot.tasks.update(cache.tasks)
        snapshot.logs.update(cache.logs)
    needed_logs = {
        str(rid or "").strip() for rid in upstream_log_ids if str(rid or "").strip()
    }
    del upstream_task_ids  # 任务日志一次拉全量，不需要逐个指定
    for endpoint in endpoints:
        if not endpoint.configured:
            snapshot.errors.append(f"{endpoint.label}: 缺少账号，已跳过")
            continue
        client = RelayBillingClient(endpoint, transport=transport)
        try:
            snapshot.statuses.append(client.status())
        except RelayBillingError as exc:
            snapshot.errors.append(str(exc))
        try:
            snapshot.tasks.update(client.task_quotas())
        except RelayBillingError as exc:
            snapshot.errors.append(str(exc))
        if needed_logs:
            missing = needed_logs.difference(snapshot.logs)
            if missing:
                try:
                    snapshot.logs.update(
                        client.log_quotas_for(
                            missing,
                            cache=cache,
                            workers=workers,
                            progress=progress,
                        )
                    )
                except RelayBillingError as exc:
                    snapshot.errors.append(str(exc))
        if cache is not None:
            cache.tasks.update(snapshot.tasks)
            cache.logs.update(snapshot.logs)
            cache.save()
    return snapshot
