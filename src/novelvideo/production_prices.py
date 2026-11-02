"""制作成本单价表与定价。

「一键成片」在动手前要报一个**上游真金白银**（¥）的预估价，CE 本身没有任何
积分账本或价格表（``LocalCreditQuote`` 恒返回 0），所以价格必须由本模块提供。

价格来源优先级（高 → 低）::

    1. ``rules``         手工规则（``/data/production-prices.json`` / 设置页），
                         永不被同步覆盖。
    2. ``actual_rules``  用已对账的真实账单反推出来的 ``¥/单位``（实测口径）。
    3. ``synced``        上游中转站 ``/api/pricing`` 的定价表达式，本地求值。
    4. 未命中            进 ``unpriced`` 清单，**绝不静默按 0 计**。

配置文件默认 ``<DATA_ROOT>/production-prices.json``（本部署 = ``/data/production-prices.json``），
可用 ``PRODUCTION_PRICES_PATH`` 覆盖；按 mtime 缓存，改文件即时生效，无需重启。

计费身份沿用 ``api/routes/model_credits.py`` 已经规约好的元组::

    kind ∈ image|video|audio|text|feature
    (model, feature_key, params{size,quality,resolution,video_input})
    quantity + metrics{call_count,item_count,duration_seconds,billable_chars,...}

由于金额是「预估」，本模块只做纯计算，**不触网**；同步上游价目是显式的运维动作
（``sync_prices``），结果快照进配置文件。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger("novelvideo.production_prices")

ENV_CONFIG_PATH = "PRODUCTION_PRICES_PATH"

#: 手工规则 / 实际反推规则共用的计费单位 → （metrics 键，每几个单位记一次价）。
#: ``price`` 一律是「每一单位的人民币金额」，例如 ``1k_token`` 表示每 1000 token。
UNITS: dict[str, tuple[str, float]] = {
    "call": ("call_count", 1.0),
    "item": ("item_count", 1.0),
    "second": ("duration_seconds", 1.0),
    "char": ("billable_chars", 1.0),
    "1k_char": ("billable_chars", 1000.0),
    "10k_char": ("billable_chars", 10000.0),
    "1k_token": ("tokens", 1000.0),
    "1k_prompt_token": ("prompt_tokens", 1000.0),
    "1k_completion_token": ("completion_tokens", 1000.0),
}

#: 表达式里的简写变量（NewAPI ``tiered_expr`` 口径）。
_EXPR_SHORTHANDS: dict[str, str] = {
    "p": "prompt_tokens",
    "c": "completion_tokens",
    "cr": "prompt_cached_tokens",
    "cc": "prompt_cache_creation_tokens",
    "img": "image_tokens",
    "img_cr": "image_cached_tokens",
}


class PriceError(RuntimeError):
    """单价表读取/求值失败。"""


class ExprError(PriceError):
    """上游定价表达式无法求值。"""


# ---------------------------------------------------------------------------
# Action / 结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionRequest:
    """一次将要发生的计费动作（预估侧构造，不触网）。"""

    kind: str
    model: str = ""
    feature_key: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    quantity: int = 1
    metrics: dict[str, Any] = field(default_factory=dict)
    label: str = ""
    episode: int | None = None


@dataclass
class PricedAction:
    """定价结果。``cny is None`` 表示未定价（进 unpriced 清单）。"""

    action: ActionRequest
    unit: str = "call"
    quantity: float = 0.0
    unit_price: float | None = None
    cny: float | None = None
    rule_id: str | None = None
    source: str = "unpriced"  # manual | actual | synced | unpriced
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        action = self.action
        return {
            "kind": action.kind,
            "model": action.model,
            "feature_key": action.feature_key,
            "label": action.label or action.feature_key or action.model or action.kind,
            "params": dict(action.params),
            "episode": action.episode,
            "unit": self.unit,
            "quantity": round(self.quantity, 4),
            "unit_price": self.unit_price,
            "cny": self.cny,
            "rule_id": self.rule_id,
            "source": self.source,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------


def _default_config() -> dict[str, Any]:
    """出厂默认：**不含任何单价**（未定价就如实进 unpriced）。

    价格靠「同步上游价目」或「用账单标定」填；换算假设给保守默认值，可在设置页改。
    """

    return {
        "version": 1,
        "currency": "CNY",
        "contingency_pct": 15.0,
        "markup_default": None,
        "client_markups": {},
        "assumptions": {
            "chars_per_beat": 60,
            "beats_per_episode": 40,
            "video_seconds_per_beat": 5.0,
            "text_tokens_per_char": 1.5,
            "text_output_token_ratio": 0.35,
            "audio_tokens_per_char": 1.0,
            "image_calls_per_beat": 1.0,
        },
        "aliases": {
            "seedance-2.0-fast": "aicf:doubao-seedance-2.0-fast",
        },
        "rules": [],
        "actual_rules": [],
        "synced": {"fetched_at": "", "relays": [], "models": {}},
    }


def config_path() -> Path:
    """单价表路径：``PRODUCTION_PRICES_PATH`` > ``<DATA_ROOT>/production-prices.json``。"""

    override = str(os.environ.get(ENV_CONFIG_PATH, "") or "").strip()
    if override:
        return Path(override)
    from novelvideo.config import DATA_ROOT

    return Path(DATA_ROOT) / "production-prices.json"


# ---------------------------------------------------------------------------
# 定价表达式求值器（安全：不用 eval，只认白名单语法）
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<num>\d+(?:\.\d+)?|\.\d+)
  | (?P<str>"[^"]*")
  | (?P<op><=|>=|==|!=|&&|\|\||[?:()+\-*/,<>])
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.VERBOSE,
)


def _tokenize(expr: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(expr):
        match = _TOKEN_RE.match(expr, pos)
        if match is None:
            raise ExprError(f"无法识别的字符 {expr[pos:pos + 12]!r} @ {pos}")
        pos = match.end()
        kind = match.lastgroup or ""
        if kind == "ws":
            continue
        tokens.append((kind, match.group()))
    return tokens


class _Evaluator:
    """递归下降求值：支持三元、比较、逻辑、四则与 ``u()`` / ``tier()``。"""

    def __init__(self, tokens: list[tuple[str, str]], variables: dict[str, Any]) -> None:
        self.tokens = tokens
        self.pos = 0
        self.variables = variables

    # -- 词法游标 ---------------------------------------------------------
    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _next(self) -> tuple[str, str]:
        token = self._peek()
        if token is None:
            raise ExprError("表达式意外结束")
        self.pos += 1
        return token

    def _accept_op(self, *ops: str) -> str | None:
        token = self._peek()
        if token is not None and token[0] == "op" and token[1] in ops:
            self.pos += 1
            return token[1]
        return None

    # -- 语法 -------------------------------------------------------------
    def parse(self) -> float:
        value = self._ternary()
        if self._peek() is not None:
            raise ExprError(f"表达式末尾有多余 token: {self._peek()}")
        return _as_number(value)

    def _ternary(self) -> Any:
        cond = self._or()
        if self._accept_op("?"):
            then_value = self._ternary()
            if self._accept_op(":") is None:
                raise ExprError("三元表达式缺 ':'")
            else_value = self._ternary()
            return then_value if _truthy(cond) else else_value
        return cond

    def _or(self) -> Any:
        value = self._and()
        while self._accept_op("||"):
            right = self._and()
            value = 1.0 if (_truthy(value) or _truthy(right)) else 0.0
        return value

    def _and(self) -> Any:
        value = self._cmp()
        while self._accept_op("&&"):
            right = self._cmp()
            value = 1.0 if (_truthy(value) and _truthy(right)) else 0.0
        return value

    def _cmp(self) -> Any:
        value = self._add()
        while True:
            op = self._accept_op("==", "!=", "<", ">", "<=", ">=")
            if op is None:
                return value
            right = self._add()
            value = _compare(op, value, right)

    def _add(self) -> Any:
        value = self._mul()
        while True:
            op = self._accept_op("+", "-")
            if op is None:
                return value
            right = self._mul()
            left_num, right_num = _as_number(value), _as_number(right)
            value = left_num + right_num if op == "+" else left_num - right_num

    def _mul(self) -> Any:
        value = self._unary()
        while True:
            op = self._accept_op("*", "/")
            if op is None:
                return value
            right = self._unary()
            left_num, right_num = _as_number(value), _as_number(right)
            if op == "*":
                value = left_num * right_num
            else:
                if right_num == 0:
                    raise ExprError("表达式里出现除以 0")
                value = left_num / right_num

    def _unary(self) -> Any:
        if self._accept_op("-"):
            return -_as_number(self._unary())
        if self._accept_op("+"):
            return _as_number(self._unary())
        return self._primary()

    def _primary(self) -> Any:
        token = self._next()
        kind, text = token
        if kind == "num":
            return float(text)
        if kind == "str":
            return text[1:-1]
        if kind == "op" and text == "(":
            value = self._ternary()
            if self._accept_op(")") is None:
                raise ExprError("括号没有闭合")
            return value
        if kind == "ident":
            return self._call_or_var(text)
        raise ExprError(f"表达式里出现意外 token {text!r}")

    def _call_or_var(self, name: str) -> Any:
        if self._accept_op("(") is None:
            return self._variable(name)
        args: list[Any] = []
        if self._accept_op(")") is None:
            args.append(self._ternary())
            while self._accept_op(","):
                args.append(self._ternary())
            if self._accept_op(")") is None:
                raise ExprError(f"{name}() 参数没闭合")
        lowered = name.lower()
        if lowered == "u":
            if not args:
                raise ExprError("u() 需要一个参数")
            return self._variable(str(args[0]))
        if lowered == "tier":
            # tier("label", expr)：标签只用于展示，值就是第二个参数。
            return args[-1] if args else 0.0
        if lowered in {"min", "max"}:
            numbers = [_as_number(arg) for arg in args]
            if not numbers:
                raise ExprError(f"{name}() 至少需要一个参数")
            return min(numbers) if lowered == "min" else max(numbers)
        raise ExprError(f"不支持的函数 {name}()")

    def _variable(self, name: str) -> Any:
        if name in self.variables:
            return self.variables[name]
        shorthand = _EXPR_SHORTHANDS.get(name)
        if shorthand and shorthand in self.variables:
            return self.variables[shorthand]
        return 0.0


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value)
    return bool(float(value))


def _as_number(value: Any) -> float:
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError as exc:
            raise ExprError(f"字符串 {value!r} 不能参与算术") from exc
    return float(value)


def _compare(op: str, left: Any, right: Any) -> float:
    if isinstance(left, str) or isinstance(right, str):
        left_str, right_str = str(left), str(right)
        if op == "==":
            return 1.0 if left_str == right_str else 0.0
        if op == "!=":
            return 1.0 if left_str != right_str else 0.0
        raise ExprError(f"字符串不支持比较符 {op}")
    left_num, right_num = float(left), float(right)
    table = {
        "==": left_num == right_num,
        "!=": left_num != right_num,
        "<": left_num < right_num,
        ">": left_num > right_num,
        "<=": left_num <= right_num,
        ">=": left_num >= right_num,
    }
    return 1.0 if table[op] else 0.0


def evaluate_expr(expr: str, variables: dict[str, Any]) -> float:
    """安全求值一条上游定价表达式；失败抛 :class:`ExprError`。"""

    tokens = _tokenize(expr)
    if not tokens:
        raise ExprError("空表达式")
    return _Evaluator(tokens, variables).parse()


# ---------------------------------------------------------------------------
# 单价表
# ---------------------------------------------------------------------------


@dataclass
class PriceBook:
    """一份单价表（``/data/production-prices.json`` 的内存视图）。"""

    raw: dict[str, Any] = field(default_factory=_default_config)
    path: Path | None = None
    mtime: float = 0.0

    # -- 基础字段 ---------------------------------------------------------
    @property
    def currency(self) -> str:
        return str(self.raw.get("currency") or "CNY")

    @property
    def contingency_pct(self) -> float:
        try:
            return float(self.raw.get("contingency_pct") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @property
    def assumptions(self) -> dict[str, Any]:
        value = self.raw.get("assumptions")
        return value if isinstance(value, dict) else {}

    @property
    def aliases(self) -> dict[str, str]:
        value = self.raw.get("aliases")
        return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}

    @property
    def rules(self) -> list[dict[str, Any]]:
        return _as_dict_list(self.raw.get("rules"))

    @property
    def actual_rules(self) -> list[dict[str, Any]]:
        return _as_dict_list(self.raw.get("actual_rules"))

    @property
    def synced(self) -> dict[str, Any]:
        value = self.raw.get("synced")
        return value if isinstance(value, dict) else {}

    @property
    def synced_models(self) -> dict[str, dict[str, Any]]:
        value = self.synced.get("models")
        return value if isinstance(value, dict) else {}

    @property
    def client_markups(self) -> dict[str, dict[str, Any]]:
        value = self.raw.get("client_markups")
        return value if isinstance(value, dict) else {}

    def relay_rate(self, label: str) -> float:
        for relay in self.synced.get("relays") or []:
            if isinstance(relay, dict) and str(relay.get("label")) == label:
                try:
                    return float(relay.get("usd_cny_rate") or 0.0) or _fallback_rate()
                except (TypeError, ValueError):
                    return _fallback_rate()
        return _fallback_rate()

    def relay_quota_per_unit(self, label: str) -> float:
        for relay in self.synced.get("relays") or []:
            if isinstance(relay, dict) and str(relay.get("label")) == label:
                try:
                    return float(relay.get("quota_per_unit") or 0.0) or 500000.0
                except (TypeError, ValueError):
                    return 500000.0
        return 500000.0

    # -- 加价倍率 ---------------------------------------------------------
    def resolve_markup(self, phone: str) -> tuple[float | None, str]:
        """按客户手机号取加价倍率；返回 ``(倍率, 来源)``，没配就 ``(None, "")``。"""

        clean = normalize_phone(phone)
        if clean:
            entry = self.client_markups.get(clean)
            if isinstance(entry, dict):
                markup = _as_positive_float(entry.get("markup"))
                if markup is not None:
                    return markup, "client"
            elif isinstance(entry, (int, float)):
                markup = _as_positive_float(entry)
                if markup is not None:
                    return markup, "client"
        default = _as_positive_float(self.raw.get("markup_default"))
        if default is not None:
            return default, "default"
        return None, ""

    # -- 定价 -------------------------------------------------------------
    def price(self, action: ActionRequest) -> PricedAction:
        """按优先级定价一个动作。"""

        manual = _match_rule(self.rules, action)
        if manual is not None:
            return _price_with_rule(action, manual, source="manual")

        actual = _match_rule(self.actual_rules, action, key="unit")
        if actual is not None:
            return _price_with_rule(action, actual, source="actual")

        synced = self._price_synced(action)
        if synced is not None:
            return synced

        return PricedAction(
            action=action,
            unit="call",
            quantity=float(action.quantity or 0),
            unit_price=None,
            cny=None,
            source="unpriced",
            note="未配置单价：设置页里加规则、同步上游价目或先跑一次账单标定",
        )

    # -- 同步价目求值 -----------------------------------------------------
    def _resolve_synced_model(self, model: str) -> tuple[str, dict[str, Any]] | None:
        models = self.synced_models
        if not models or not model:
            return None
        alias = self.aliases.get(model)
        if alias and alias in models:
            return alias, models[alias]
        # 没有显式别名：按 (relay:name) 精确、再去掉站点前缀精确、最后归一化匹配。
        if model in models:
            return model, models[model]
        for key, entry in models.items():
            if key.split(":", 1)[-1] == model:
                return key, entry
        normalized = _normalize_name(model)
        for key, entry in models.items():
            if _normalize_name(key.split(":", 1)[-1]) == normalized:
                return key, entry
        return None

    def _price_synced(self, action: ActionRequest) -> PricedAction | None:
        resolved = self._resolve_synced_model(action.model)
        if resolved is None:
            return None
        key, entry = resolved
        label = key.split(":", 1)[0] if ":" in key else ""
        rate = self.relay_rate(label)
        quota_per_unit = self.relay_quota_per_unit(label)
        variables = _synced_variables(action)
        try:
            if int(entry.get("quota_type") or 0) == 1:
                # 固定价模型：model_price 是「每次调用」的美元价。
                usd = _as_positive_float(entry.get("model_price"))
                if usd is None:
                    return None
                return _priced(
                    action,
                    unit="call",
                    quantity=float(action.metrics.get("call_count") or action.quantity or 0),
                    unit_price=usd * rate,
                    source="synced",
                    rule_id=f"synced:{key}",
                    note="上游固定价（每次调用）",
                )
            expr = str(entry.get("billing_expr") or "").strip()
            if expr:
                usd = evaluate_expr(expr, variables)
                unit, quantity = _synced_unit_quantity(action, entry, variables)
                price_cny = usd * rate
                return _priced(
                    action,
                    unit=unit,
                    quantity=quantity,
                    unit_price=price_cny / quantity if quantity else price_cny,
                    cny=price_cny,
                    source="synced",
                    rule_id=f"synced:{key}",
                    note=f"上游定价表达式（{label or 'relay'}）",
                )
            ratio = _as_positive_float(entry.get("model_ratio"))
            tokens = _as_number(variables.get("tokens"))
            if ratio is not None and tokens > 0:
                quota = tokens * ratio
                usd = quota / quota_per_unit if quota_per_unit else 0.0
                return _priced(
                    action,
                    unit="1k_token",
                    quantity=tokens / 1000.0,
                    unit_price=usd * rate / (tokens / 1000.0) if tokens else 0.0,
                    cny=usd * rate,
                    source="synced",
                    rule_id=f"synced:{key}",
                    note=f"上游倍率价（{label or 'relay'}）",
                )
        except ExprError as exc:
            logger.warning("上游定价表达式求值失败 %s: %s", key, exc)
            return None
        return None


def _priced(
    action: ActionRequest,
    *,
    unit: str,
    quantity: float,
    source: str,
    rule_id: str,
    unit_price: float | None = None,
    cny: float | None = None,
    note: str = "",
) -> PricedAction:
    if cny is None and unit_price is not None:
        cny = unit_price * quantity
    return PricedAction(
        action=action,
        unit=unit,
        quantity=quantity,
        unit_price=unit_price,
        cny=cny,
        rule_id=rule_id,
        source=source,
        note=note,
    )


def _fallback_rate() -> float:
    try:
        from novelvideo.config import USD_CNY_RATE

        return float(USD_CNY_RATE or 7.3)
    except Exception:  # noqa: BLE001
        return 7.3


def _synced_variables(action: ActionRequest) -> dict[str, Any]:
    variables: dict[str, Any] = {}
    for key, value in action.params.items():
        variables[str(key)] = value
    metrics = action.metrics
    variables["seconds"] = _as_number(
        metrics.get("duration_seconds", metrics.get("output_duration_seconds", 0.0))
    )
    variables["tokens"] = _as_number(metrics.get("tokens", 0.0))
    variables["videos"] = _as_number(metrics.get("call_count", action.quantity or 0))
    variables["prompt_tokens"] = _as_number(metrics.get("prompt_tokens", 0.0))
    variables["completion_tokens"] = _as_number(metrics.get("completion_tokens", 0.0))
    variables["prompt_cached_tokens"] = _as_number(metrics.get("prompt_cached_tokens", 0.0))
    variables["prompt_cache_creation_tokens"] = _as_number(
        metrics.get("prompt_cache_creation_tokens", 0.0)
    )
    variables["image_tokens"] = _as_number(metrics.get("image_tokens", 0.0))
    variables["image_cached_tokens"] = _as_number(metrics.get("image_cached_tokens", 0.0))
    return variables


def _synced_unit_quantity(
    action: ActionRequest, entry: dict[str, Any], variables: dict[str, Any]
) -> tuple[str, float]:
    """猜一个「人类可读」的单位与数量，只为展示（金额以表达式为准）。"""

    schema = entry.get("usage_schema")
    keys = list(schema.keys()) if isinstance(schema, dict) else []
    if "seconds" in keys or "seconds" in variables:
        return "second", _as_number(variables.get("seconds"))
    if "tokens" in keys or _as_number(variables.get("tokens")) > 0:
        return "1k_token", _as_number(variables.get("tokens")) / 1000.0
    return "call", float(action.metrics.get("call_count") or action.quantity or 0)


def _match_rule(
    rules: Iterable[dict[str, Any]], action: ActionRequest, *, key: str = "match"
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_score: tuple[int, int, int, int] | None = None
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        match = rule.get(key)
        if not isinstance(match, dict):
            if key != "match":
                # actual_rules 用扁平字段（kind/model/unit/price），没有 match 块。
                match = rule
            else:
                continue
        if not _rule_matches(match, action):
            continue
        score = _rule_score(match)
        if best_score is None or score > best_score:
            best, best_score = rule, score
    return best


def _rule_matches(match: dict[str, Any], action: ActionRequest) -> bool:
    kind = match.get("kind")
    if kind and str(kind) != action.kind:
        return False
    model = match.get("model")
    if model and str(model) != "*" and str(model) != action.model:
        return False
    feature_key = match.get("feature_key")
    if feature_key and str(feature_key) != action.feature_key:
        return False
    params = match.get("params")
    if isinstance(params, dict):
        for key, expected in params.items():
            if str(action.params.get(key, "")) != str(expected):
                return False
    return True


def _rule_score(match: dict[str, Any]) -> tuple[int, int, int, int]:
    params = match.get("params")
    param_count = len(params) if isinstance(params, dict) else 0
    model = str(match.get("model") or "")
    return (
        param_count,
        1 if match.get("feature_key") else 0,
        1 if model and model != "*" else 0,
        1 if match.get("kind") else 0,
    )


def _price_with_rule(
    action: ActionRequest, rule: dict[str, Any], *, source: str
) -> PricedAction:
    unit = str(rule.get("unit") or "call").strip() or "call"
    unit_price = _as_positive_float(rule.get("price"))
    quantity = _unit_quantity(action, unit)
    if unit_price is None or quantity is None:
        return PricedAction(
            action=action,
            unit=unit,
            quantity=quantity or 0.0,
            unit_price=unit_price,
            cny=None,
            rule_id=str(rule.get("id") or "") or None,
            source="unpriced",
            note="规则命中但缺少单位数量（metrics 不全）",
        )
    return PricedAction(
        action=action,
        unit=unit,
        quantity=quantity,
        unit_price=unit_price,
        cny=unit_price * quantity,
        rule_id=str(rule.get("id") or rule.get("rule_id") or "") or None,
        source=source,
        note=str(rule.get("note") or ""),
    )


def _unit_quantity(action: ActionRequest, unit: str) -> float | None:
    spec = UNITS.get(unit)
    if spec is None:
        return None
    metric_key, per = spec
    value = _as_number(action.metrics.get(metric_key))
    if metric_key == "duration_seconds" and value <= 0:
        value = _as_number(action.metrics.get("output_duration_seconds"))
    if value <= 0 and metric_key in {"call_count", "item_count"}:
        value = float(action.quantity or 0)
    if value <= 0:
        return None
    return value / per


# ---------------------------------------------------------------------------
# 配置读写（mtime 缓存）
# ---------------------------------------------------------------------------

_LOCK = threading.RLock()
_CACHE: dict[str, tuple[float, PriceBook]] = {}


def load(*, force: bool = False) -> PriceBook:
    """读取单价表（按 mtime 缓存）；文件不存在时返回默认表。"""

    path = config_path()
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None and not force and cached[0] == mtime:
            return cached[1]
        book = _read_book(path)
        _CACHE[key] = (mtime, book)
        return book


def _read_book(path: Path) -> PriceBook:
    raw = _default_config()
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 坏文件不能让预估整个挂掉
            logger.warning("单价表不可读，用默认值: %s (%s)", path, exc)
            payload = None
        if isinstance(payload, dict):
            raw = _merge(raw, payload)
    return PriceBook(raw=raw, path=path, mtime=_safe_mtime(path))


def _merge(default: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """浅合并：顶层标量覆盖，``assumptions``/``synced``/``client_markups`` 深一层。"""

    merged = dict(default)
    for key, value in override.items():
        if key in {"assumptions", "synced"} and isinstance(value, dict) and isinstance(
            merged.get(key), dict
        ):
            nested = dict(merged[key])
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = value
    return merged


def save(book: PriceBook) -> PriceBook:
    """原子写回单价表并刷新缓存。"""

    path = book.path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(book.raw, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    book.path = path
    book.mtime = _safe_mtime(path)
    with _LOCK:
        _CACHE[str(path)] = (book.mtime, book)
    return book


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def normalize_phone(phone: Any) -> str:
    """规范化手机号：只留数字。"""

    return re.sub(r"\D", "", str(phone or ""))


def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _as_positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _as_number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


# ---------------------------------------------------------------------------
# 上游价目同步（显式运维动作，不参与请求路径）
# ---------------------------------------------------------------------------


def relay_label(base_url: str) -> str:
    """``https://aicf.nanie168.com`` → ``aicf``。"""

    from urllib.parse import urlparse

    host = urlparse(str(base_url or "")).hostname or str(base_url or "")
    return host.split(".")[0] or host or "relay"


def _relay_base_urls() -> list[str]:
    from novelvideo.relay_billing import DEFAULT_RELAY_BASE_URLS

    raw = str(os.environ.get("NOVELVIDEO_RELAY_BILLING_BASE_URLS", "") or "")
    urls = [part.strip() for part in raw.split(",") if part.strip()]
    return urls or list(DEFAULT_RELAY_BASE_URLS)


def sync_prices(
    book: PriceBook | None = None,
    *,
    base_urls: Sequence[str] | None = None,
    transport: Any | None = None,
) -> tuple[PriceBook, dict[str, Any]]:
    """从上游中转站拉公开价目，快照进单价表。

    只读上游、只写本地配置文件；失败**不抛异常**，错误在返回值里如实回报。
    ``transport`` 可注入以便单测不触网。
    """

    from novelvideo.relay_billing import RelayBillingClient, RelayBillingError

    book = book or load(force=True)
    urls = list(base_urls) if base_urls else _relay_base_urls()
    snapshot = book.synced if isinstance(book.synced, dict) else {}
    relays: list[dict[str, Any]] = []
    models: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    for base_url in urls:
        from novelvideo.relay_billing import RelayEndpoint

        endpoint = RelayEndpoint(base_url=base_url)
        label = relay_label(base_url)
        client = RelayBillingClient(endpoint, transport=transport)
        rate = _fallback_rate()
        quota_per_unit = 500000.0
        try:
            status = client.status()
            rate = status.usd_exchange_rate or rate
            quota_per_unit = status.quota_per_unit or quota_per_unit
        except RelayBillingError as exc:
            errors.append(str(exc))
        try:
            payload = client.pricing()
        except RelayBillingError as exc:
            errors.append(str(exc))
            continue
        relays.append(
            {
                "label": label,
                "base_url": str(base_url).rstrip("/"),
                "usd_cny_rate": rate,
                "quota_per_unit": quota_per_unit,
                "pricing_version": str(payload.get("pricing_version") or ""),
                "group_ratio": payload.get("group_ratio") or {},
                "models": len(payload.get("data") or []),
            }
        )
        for entry in payload.get("data") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("model_name") or "").strip()
            if not name:
                continue
            models[f"{label}:{name}"] = {
                "model_name": name,
                "quota_type": entry.get("quota_type"),
                "model_ratio": entry.get("model_ratio"),
                "model_price": entry.get("model_price"),
                "completion_ratio": entry.get("completion_ratio"),
                "billing_mode": entry.get("billing_mode") or "ratio",
                "billing_expr": entry.get("billing_expr") or "",
                "usage_schema": entry.get("billing_usage_schema") or {},
                "description": str(entry.get("description") or "")[:200],
            }

    book.raw["synced"] = {
        "fetched_at": datetime.now().isoformat(),
        "relays": relays,
        "models": models,
        "errors": errors,
        "previous_fetched_at": snapshot.get("fetched_at", ""),
    }
    save(book)
    result = {
        "fetched_at": book.raw["synced"]["fetched_at"],
        "relays": relays,
        "models": len(models),
        "errors": errors,
    }
    return book, result


# ---------------------------------------------------------------------------
# 用真实账单标定单价（¥/单位）
# ---------------------------------------------------------------------------


@dataclass
class _Calibration:
    kind: str
    model: str
    unit: str
    units: float = 0.0
    cny: float = 0.0
    samples: int = 0


_USAGE_SOURCES: tuple[tuple[str, str, str, str], ...] = (
    # (kind, db path helper, table, model column)
    ("image", "get_image_request_usage_db_path", "image_request_usage", "model_name"),
    ("video", "get_video_request_usage_db_path", "video_request_usage", "model_name"),
    ("audio", "get_audio_request_usage_db_path", "audio_request_usage", "model_name"),
    ("llm", "get_llm_request_usage_db_path", "llm_request_usage", "model"),
)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def calibrate_from_project(
    project_output_dir: str | Path, *, usd_cny_rate: float | None = None
) -> dict[str, Any]:
    """读项目四张用量表的已对账 ``cost_quota``，反推 ``¥/单位``。

    粒度只能到「种类 × 模型」：图片按「次」、视频按「秒」、音频按「次」、
    文本按「千 token」。``cost_quota`` 为空的行不计入（未对账 ≠ 0 元）。
    """

    from novelvideo.config import QUOTA_PER_USD

    rate = float(usd_cny_rate or _fallback_rate())
    per_usd = float(QUOTA_PER_USD or 500000.0)
    buckets: dict[tuple[str, str, str], _Calibration] = {}
    sources: list[dict[str, Any]] = []

    for kind, helper_name, table, model_column in _USAGE_SOURCES:
        import novelvideo.image_request_usage as image_usage
        import novelvideo.video_request_usage as video_usage
        import novelvideo.audio_request_usage as audio_usage
        import novelvideo.llm_request_usage as llm_usage

        module = {
            "get_image_request_usage_db_path": image_usage,
            "get_video_request_usage_db_path": video_usage,
            "get_audio_request_usage_db_path": audio_usage,
            "get_llm_request_usage_db_path": llm_usage,
        }[helper_name]
        db_path = module.__dict__[helper_name](project_output_dir)
        db_path = Path(db_path)
        info = {"kind": kind, "table": table, "rows": 0, "priced": 0}
        if not db_path.is_file():
            sources.append({**info, "error": "用量库不存在"})
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        except sqlite3.Error as exc:
            sources.append({**info, "error": str(exc)})
            continue
        conn.row_factory = sqlite3.Row
        try:
            columns = _table_columns(conn, table)
            if not columns:
                sources.append({**info, "error": "表不存在"})
                continue
            model_expr = model_column if model_column in columns else "''"
            quota_expr = "cost_quota" if "cost_quota" in columns else "NULL"
            duration_expr = "duration_seconds" if "duration_seconds" in columns else "0"
            input_expr = "input_tokens" if "input_tokens" in columns else "0"
            output_expr = "output_tokens" if "output_tokens" in columns else "0"
            query = (
                f"SELECT {model_expr} AS model_name, {quota_expr} AS cost_quota, "
                f"{duration_expr} AS duration_seconds, {input_expr} AS input_tokens, "
                f"{output_expr} AS output_tokens FROM {table}"
            )
            for row in conn.execute(query):
                info["rows"] += 1
                quota = row["cost_quota"]
                if quota is None:
                    continue
                info["priced"] += 1
                model = str(row["model_name"] or "unknown")
                if kind == "video":
                    unit, units = "second", _as_number(row["duration_seconds"])
                elif kind in {"image", "audio"}:
                    unit, units = "call", 1.0
                else:
                    unit = "1k_token"
                    units = (
                        _as_number(row["input_tokens"]) + _as_number(row["output_tokens"])
                    ) / 1000.0
                bucket = buckets.setdefault(
                    (kind, model, unit), _Calibration(kind=kind, model=model, unit=unit)
                )
                bucket.units += units
                bucket.cny += (float(quota) / per_usd) * rate
                bucket.samples += 1
        finally:
            conn.close()
        sources.append(info)

    rules: list[dict[str, Any]] = []
    for bucket in buckets.values():
        if bucket.units <= 0 or bucket.samples <= 0:
            continue
        price = bucket.cny / bucket.units
        if price <= 0:
            continue
        rules.append(
            {
                "id": f"actual:{bucket.kind}:{bucket.model}",
                "kind": bucket.kind,
                "model": bucket.model,
                "unit": bucket.unit,
                "price": round(price, 6),
                "samples": bucket.samples,
                "total_cny": round(bucket.cny, 2),
                "total_units": round(bucket.units, 2),
                "note": "由已对账的真实账单反推（非上游标价，是实收口径）",
            }
        )
    rules.sort(key=lambda item: (item["kind"], item["model"]))
    metadata = {
        "computed_at": datetime.now().isoformat(),
        "usd_cny_rate": rate,
        "quota_per_usd": per_usd,
        "sources": sources,
    }
    return {"rules": rules, "metadata": metadata}


def apply_actual_rules(book: PriceBook, calibration: dict[str, Any], *, replace: bool = True) -> PriceBook:
    """把标定结果写进 ``actual_rules``（默认整体替换）。"""

    new_rules = _as_dict_list(calibration.get("rules"))
    if replace:
        merged = new_rules
    else:
        existing = {str(rule.get("id")): rule for rule in book.actual_rules}
        for rule in new_rules:
            existing[str(rule.get("id"))] = rule
        merged = list(existing.values())
    book.raw["actual_rules"] = merged
    book.raw["calibrated_at"] = str(calibration.get("metadata", {}).get("computed_at") or "")
    return save(book)

