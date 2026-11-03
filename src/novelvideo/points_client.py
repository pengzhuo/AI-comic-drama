"""漫剧伴点预扣（``dopoints.php``）。

「一键成片」在分镜写完、第一批花钱的图/视频开跑之前，先把这一集的**售价**
（成本 × 该客户倍率，¥）按 **100 伴点 = ¥1** 折算成整数伴点，调上游
``POST /api/aistory/dopoints.php`` **预扣客户伴点**：扣成功才放行，失败把接口
``message`` 原样交给客户。

接口约定::

    phone         手机号（11 位）
    amount        整数正数（伴点）
    optype        1=减少 2=增加
    projectsuuid  项目唯一 ID（我们的项目 id）
    sign          ``md5(phone + 密钥)`` 小写 hex（与 studio_access.expected_code 同算法）

响应**恒为 HTTP 200**，成败只看 body::

    {"code": 0, "message": "操作成功", "data": []}           成功
    {"code": 1, "message": "伴点不足，请先充值", "data": null}  业务失败

所以 ``code != 0`` 也必须当失败处理（见 :class:`PointsNotEnough`）。

设计约定
--------
- **只扣不退**：``operate`` 只发一次请求，不重试、不自动冲正；重复确认由调用方
  用台账挡（同一集只扣一次）。
- 配置默认 ``<DATA_ROOT>/dopoints.json``（本部署 = ``/data/dopoints.json``），可用
  ``DOPOINTS_CONFIG_PATH`` 覆盖；按 mtime 缓存，改文件即时生效，无需重启。
- **缺省关闭**：配置文件不存在或 ``enabled=false`` 时调用方跳过扣费。密钥留空时
  沿用 studio 的 ``client_secret``（签名算法与 ``studio_access.expected_code`` 一致）。
- HTTP 传输可注入（``transport``），单测不触网。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("novelvideo.points_client")

ENV_CONFIG_PATH = "DOPOINTS_CONFIG_PATH"
DEFAULT_BASE_URL = "https://ai.nanie168.com/api/aistory/dopoints.php"
#: 100 伴点 = ¥1。
DEFAULT_POINTS_PER_CNY = 100.0
DEFAULT_TIMEOUT_SECONDS = 15.0
#: 成片后按实际用量多退少补：``off`` 只预扣不定数；``compose`` 成片完成后结算。
DEFAULT_SETTLE = "off"
#: 允许的差额比例（%）：差额在预扣的该比例以内就当已结清，不补不退。
DEFAULT_SETTLE_TOLERANCE_PCT = 0.0
#: 结算前跑 relay 对账的预算（秒）；超时按「算不出来」处理（放行）。
DEFAULT_SETTLE_BUDGET_SECONDS = 20.0
SETTLE_MODES = ("off", "compose")

#: ``optype``：1=减少（扣伴点），2=增加（冲正/补发）。
OP_DEDUCT = 1
OP_ADD = 2

#: ``transport(method, url, headers, form_body) -> (status, text)``
Transport = Callable[[str, str, dict[str, str], "dict[str, Any] | None"], "tuple[int, str]"]


class PointsError(RuntimeError):
    """伴点操作失败。"""


class PointsNotEnough(PointsError):
    """业务失败（HTTP 200 但 ``code != 0``）。

    ``str(exc)`` 就是接口的 ``message``（如「伴点不足，请先充值」），直接给客户看。
    """


class PointsUnavailable(PointsError):
    """网络/超时/5xx/坏 JSON/未配置。

    与业务失败分开，调用方要 **fail-closed**：不继续花钱，也不要把它当成
    「客户欠费」去展示。
    """


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass
class PointsConfig:
    """``/data/dopoints.json`` 的内容（缺字段用默认值）。"""

    enabled: bool = False
    base_url: str = DEFAULT_BASE_URL
    sign_key: str = ""
    points_per_cny: float = DEFAULT_POINTS_PER_CNY
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    settle: str = DEFAULT_SETTLE
    settle_tolerance_pct: float = DEFAULT_SETTLE_TOLERANCE_PCT
    settle_budget_seconds: float = DEFAULT_SETTLE_BUDGET_SECONDS
    path: Path | None = None
    mtime: float = 0.0

    def __post_init__(self) -> None:
        self.base_url = str(self.base_url or "").strip() or DEFAULT_BASE_URL
        self.sign_key = str(self.sign_key or "").strip()
        try:
            rate = float(self.points_per_cny)
        except (TypeError, ValueError):
            rate = DEFAULT_POINTS_PER_CNY
        self.points_per_cny = rate if rate > 0 else DEFAULT_POINTS_PER_CNY
        try:
            timeout = float(self.timeout_seconds)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_SECONDS
        self.timeout_seconds = timeout if timeout > 0 else DEFAULT_TIMEOUT_SECONDS
        mode = str(self.settle or "").strip().lower()
        self.settle = mode if mode in SETTLE_MODES else DEFAULT_SETTLE
        try:
            tolerance = float(self.settle_tolerance_pct)
        except (TypeError, ValueError):
            tolerance = DEFAULT_SETTLE_TOLERANCE_PCT
        self.settle_tolerance_pct = tolerance if tolerance > 0 else 0.0
        try:
            budget = float(self.settle_budget_seconds)
        except (TypeError, ValueError):
            budget = DEFAULT_SETTLE_BUDGET_SECONDS
        self.settle_budget_seconds = budget if budget > 0 else DEFAULT_SETTLE_BUDGET_SECONDS

    @property
    def configured(self) -> bool:
        """地址与密钥都在才能发请求。"""

        return bool(self.base_url and self.sign_key)

    @property
    def settle_enabled(self) -> bool:
        """成片后是否按实际用量多退少补。"""

        return bool(self.enabled and self.settle == "compose")


def config_path() -> Path:
    """伴点配置路径：``DOPOINTS_CONFIG_PATH`` > ``<DATA_ROOT>/dopoints.json``。"""

    override = str(os.environ.get(ENV_CONFIG_PATH, "") or "").strip()
    if override:
        return Path(override)
    from novelvideo.config import DATA_ROOT

    return Path(DATA_ROOT) / "dopoints.json"


def _default_config() -> dict[str, Any]:
    """出厂默认：**关闭**，密钥留空（回落到 studio 的 client_secret）。"""

    return {
        "enabled": False,
        "base_url": DEFAULT_BASE_URL,
        "sign_key": "",
        "points_per_cny": DEFAULT_POINTS_PER_CNY,
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
        "settle": DEFAULT_SETTLE,
        "settle_tolerance_pct": DEFAULT_SETTLE_TOLERANCE_PCT,
        "settle_budget_seconds": DEFAULT_SETTLE_BUDGET_SECONDS,
    }


_LOCK = threading.RLock()
_CACHE: dict[str, tuple[float, PointsConfig]] = {}


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def load(*, force: bool = False) -> PointsConfig:
    """读取伴点配置（按 mtime 缓存）；文件不存在时返回默认（关闭）。"""

    path = config_path()
    key = str(path)
    mtime = _safe_mtime(path)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None and not force and cached[0] == mtime:
            return cached[1]
        config = _read_config(path)
        _CACHE[key] = (mtime, config)
        return config


def _read_config(path: Path) -> PointsConfig:
    raw = _default_config()
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 坏文件不能把闸门整个挂掉
            logger.warning("伴点配置不可读，用默认值: %s (%s)", path, exc)
            payload = None
        if isinstance(payload, dict):
            raw.update(payload)
    config = PointsConfig(
        enabled=bool(raw.get("enabled", False)),
        base_url=str(raw.get("base_url") or DEFAULT_BASE_URL),
        sign_key=str(raw.get("sign_key") or ""),
        points_per_cny=raw.get("points_per_cny", DEFAULT_POINTS_PER_CNY),
        timeout_seconds=raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
        settle=raw.get("settle", DEFAULT_SETTLE),
        settle_tolerance_pct=raw.get(
            "settle_tolerance_pct", DEFAULT_SETTLE_TOLERANCE_PCT
        ),
        settle_budget_seconds=raw.get(
            "settle_budget_seconds", DEFAULT_SETTLE_BUDGET_SECONDS
        ),
        path=path,
        mtime=_safe_mtime(path),
    )
    if not config.sign_key:
        config.sign_key = _studio_secret()
    return config


def _studio_secret() -> str:
    """签名密钥缺省取 studio 的 ``client_secret``（同算法）。"""

    try:
        from novelvideo import studio_access

        return str(studio_access.client_secret() or "").strip()
    except Exception:  # noqa: BLE001 - 拿不到密钥就当未配置
        logger.debug("读取 studio client_secret 失败", exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# 计价与签名
# ---------------------------------------------------------------------------


def sign(phone: str, key: str) -> str:
    """``md5(phone + 密钥)`` 小写 hex。"""

    return hashlib.md5(f"{phone}{key}".encode("utf-8")).hexdigest()


def points_per_cny() -> float:
    """当前换算比例（默认 100）。"""

    return load().points_per_cny


def points_for_cny(cny: float | None, config: PointsConfig | None = None) -> int:
    """把金额（¥）折算成整数伴点；算不出返回 0。"""

    if cny is None:
        return 0
    rate = (config or load()).points_per_cny
    try:
        return int(round(float(cny) * rate))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_http_client: Any = None
_http_lock = threading.Lock()


def _default_transport(
    method: str, url: str, headers: dict[str, str], form_body: dict[str, Any] | None
) -> tuple[int, str]:
    """httpx 实现（表单 POST）；网络异常折成 ``status=0``，交由调用方按失败处理。"""

    global _http_client
    try:
        import httpx
    except Exception as exc:  # pragma: no cover - httpx 是硬依赖
        return 0, json.dumps({"error": f"httpx unavailable: {exc}"})
    with _http_lock:
        if _http_client is None:
            _http_client = httpx.Client(follow_redirects=True)
        client = _http_client
    try:
        response = client.request(
            method,
            url,
            headers=headers,
            data=form_body,
            timeout=load().timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - 超时/连接失败都算「暂时不可用」
        logger.warning("伴点接口请求失败 %s: %s", url, exc)
        return 0, json.dumps({"error": f"{type(exc).__name__}: {exc}"})
    return int(response.status_code), response.text


def _load_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001 - 上游可能返回 HTML 错误页
        return None


class PointsClient:
    """``dopoints.php`` 客户端。"""

    def __init__(
        self,
        config: PointsConfig | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.config = config or load()
        self._transport: Transport = transport or _default_transport

    def operate(
        self,
        *,
        phone: str,
        amount: int,
        optype: int,
        projects_uuid: str,
    ) -> dict[str, Any]:
        """调一次接口，返回原始 JSON。

        - ``code == 0`` → 返回 body
        - ``code != 0`` → :class:`PointsNotEnough`（``str(exc)`` = 接口 message）
        - 非 200 / 非 JSON / 缺 code / 传输失败 → :class:`PointsUnavailable`
        """

        config = self.config
        if not config.configured:
            raise PointsUnavailable("伴点接口未配置")
        phone_text = str(phone or "").strip()
        if not phone_text:
            raise PointsUnavailable("伴点接口缺少手机号")
        try:
            amount_int = int(amount)
            optype_int = int(optype)
        except (TypeError, ValueError) as exc:
            raise PointsUnavailable(f"伴点参数非法: {exc}") from exc
        if amount_int <= 0:
            raise PointsUnavailable("伴点数量必须为正整数")

        fields = {
            "phone": phone_text,
            "amount": str(amount_int),
            "optype": str(optype_int),
            "projectsuuid": str(projects_uuid or "").strip(),
            "sign": sign(phone_text, config.sign_key),
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        status, text = self._transport("POST", config.base_url, headers, fields)
        if status != 200:
            raise PointsUnavailable(f"伴点接口 HTTP {status or '不可达'}")
        payload = _load_json(text)
        if not isinstance(payload, dict):
            raise PointsUnavailable("伴点接口返回不是 JSON")
        try:
            code = int(payload.get("code"))
        except (TypeError, ValueError) as exc:
            raise PointsUnavailable("伴点接口返回缺少 code") from exc
        message = str(payload.get("message") or "").strip()
        if code != 0:
            raise PointsNotEnough(message or "伴点操作失败")
        return payload

    def charge(self, *, phone: str, amount: int, projects_uuid: str) -> dict[str, Any]:
        """扣伴点（``optype=1``）。"""

        return self.operate(
            phone=phone,
            amount=amount,
            optype=OP_DEDUCT,
            projects_uuid=projects_uuid,
        )

    def refund(self, *, phone: str, amount: int, projects_uuid: str) -> dict[str, Any]:
        """加伴点（``optype=2``，冲正用；当前不自动调用）。"""

        return self.operate(
            phone=phone,
            amount=amount,
            optype=OP_ADD,
            projects_uuid=projects_uuid,
        )


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_POINTS_PER_CNY",
    "DEFAULT_SETTLE",
    "DEFAULT_SETTLE_BUDGET_SECONDS",
    "DEFAULT_SETTLE_TOLERANCE_PCT",
    "DEFAULT_TIMEOUT_SECONDS",
    "ENV_CONFIG_PATH",
    "OP_ADD",
    "OP_DEDUCT",
    "PointsClient",
    "PointsConfig",
    "PointsError",
    "PointsNotEnough",
    "PointsUnavailable",
    "SETTLE_MODES",
    "Transport",
    "config_path",
    "load",
    "points_for_cny",
    "points_per_cny",
    "sign",
]
