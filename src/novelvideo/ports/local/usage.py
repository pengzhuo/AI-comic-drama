"""Local CE usage port implementations."""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any, Optional
from uuid import uuid4

from novelvideo.llm_instrumentation import (
    clear_llm_usage_context,
    set_llm_usage_context,
)
from novelvideo.ports.usage import (
    FeatureSettlementResolution,
    VerifiedTaskSettlementIdentity,
)


logger = logging.getLogger("novelvideo.ports.local.usage")

# CE has no credit ledger, but it still needs to know which project a model
# call served so the cost report can attribute gateway quota. ``_PROJECT_DIR_CTX``
# is set by callers that already resolved the project; ``_PROJECT_DIR_BY_ID``
# caches the registry lookup for callers that only carry a project id.
_PROJECT_DIR_CTX: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ce_llm_project_dir", default=""
)
_PENDING_TOKENS_CTX: contextvars.ContextVar[tuple[int, int]] = contextvars.ContextVar(
    "ce_pending_llm_tokens", default=(0, 0)
)
_PROJECT_DIR_BY_ID: dict[str, str] = {}



class NoOpUsageMeter:
    async def resolve_feature_credit_reservation(
        self,
        identity: VerifiedTaskSettlementIdentity,
    ) -> FeatureSettlementResolution:
        return FeatureSettlementResolution(outcome="not_applicable")

    async def reserve_current_model_call_credit(
        self,
        *,
        model: str,
        project_id: Optional[str] = None,
        resource_kind: str = "",
        billing_kind: str = "model",
        billing_params: Optional[dict[str, Any]] = None,
        billing_quantity: int | float | str | None = 1,
        metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        return ""

    async def refund_model_call_credit_reservation(
        self,
        reservation_id: str,
        *,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        return None

    async def update_current_model_call_log(
        self,
        *,
        request_payload: Optional[dict[str, Any]] = None,
        response_payload: Optional[dict[str, Any]] = None,
        error_message: str = "",
    ) -> None:
        return None

    async def reserve_feature_start_credits(
        self,
        *,
        user_id: str,
        feature_key: str,
        product_surface: str,
        project_id: str = "",
        resource_kind: str = "",
        task_id: str = "",
        task_type: str = "",
        metadata: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
        quantity: int | float | str | None = 1,
        idempotency_key: str = "",
        require_price_rule: bool = False,
        require_positive_cost: bool = False,
    ) -> dict[str, Any]:
        return {
            "id": "",
            "user_id": user_id,
            "feature_key": feature_key,
            "product_surface": product_surface,
            "cost": 0,
            "reserved": False,
            "balance_after": None,
            "reason": "feature_reserved",
        }

    async def require_feature_credit_balance(
        self,
        *,
        user_id: str,
        feature_key: str,
        project_id: str = "",
        resource_kind: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return {
            "user_id": user_id,
            "feature_key": feature_key,
            "required_balance": 0,
            "balance": None,
            "allowed": True,
        }

    async def confirm_feature_credit_reservation(
        self,
        reservation_id: str,
        *,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        return None

    async def refund_feature_credit_reservation(
        self,
        reservation_id: str,
        *,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        return None

    async def settle_feature_credit_reservation(
        self,
        reservation_id: str,
        *,
        action: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return {
            "reservation_id": reservation_id,
            "action": action,
            "status": "completed",
        }

    async def settle_cancelled_feature_credit_reservation(
        self,
        reservation_id: str,
        *,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return {
            "reservation_id": reservation_id,
            "decision": "refund",
            "status": "completed",
        }

    async def mark_feature_credit_settlement_for_review(
        self,
        reservation_id: str,
        *,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return {
            "reservation_id": reservation_id,
            "action": "review",
            "status": "awaiting",
        }

    async def mark_model_call_credit_settlement_for_review(
        self,
        reservation_id: str,
        *,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return {
            "reservation_id": reservation_id,
            "action": "review",
            "status": "awaiting",
        }
    async def mark_current_paid_execution_attempt(
        self,
        *,
        status: str,
        provider_request_id: str = "",
        provider_task_id: str = "",
        provider_response_id: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        return None

    async def bump_model_call(
        self,
        *,
        user_id: Optional[str],
        model: str = "",
        project_id: Optional[str] = None,
        resource_kind: str = "",
        provider_request_id: str = "",
        provider_task_id: str = "",
        credit_reservation_id: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        return None

    def set_llm_usage_context(
        self,
        user_id: Optional[str],
        project_id: Optional[str] = None,
        resource_kind: str = "",
        billing_metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        set_llm_usage_context(
            user_id,
            project_id=project_id,
            resource_kind=resource_kind,
            billing_metadata=billing_metadata,
        )

    def clear_llm_usage_context(self) -> None:
        clear_llm_usage_context()

    async def set_project_llm_usage_context(
        self,
        *,
        username: Optional[str],
        project_name: Optional[str],
        resource_kind: str = "",
        billing_metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        return None

    async def get_user_credit_balance(self, user_id: str) -> int | None:
        return 0

    async def bump_content_counter(
        self,
        *,
        user_id: Optional[str],
        metric: str,
        value: int,
        model: str = "",
        project_id: Optional[str] = None,
        resource_kind: str = "",
    ) -> None:
        return None

    async def log_resource_attempts(
        self,
        *,
        user_id: Optional[str],
        project_id: Optional[str],
        kind: str,
        refs: list[str],
        outcome: str = "success",
        model: str = "",
    ) -> None:
        return None

    async def record_llm_tokens(
        self,
        *,
        user_id: Optional[str],
        input_tokens: int,
        output_tokens: int,
        model: Optional[str] = None,
        project_id: Optional[str] = None,
        resource_kind: str = "",
    ) -> None:
        return None


class NoOpProviderInstrumentation:
    def install(self) -> None:
        return None


def _episode_from_metadata(metadata: Optional[dict[str, Any]]) -> int | None:
    if not isinstance(metadata, dict):
        return None
    for key in ("episode", "episode_num", "episode_number"):
        try:
            value = int(metadata.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


async def _resolve_project_output_dir(project_id: str) -> str:
    """Map a project id to its output dir using the project registry (cached)."""
    if not project_id:
        return ""
    if project_id in _PROJECT_DIR_BY_ID:
        return _PROJECT_DIR_BY_ID[project_id]
    output_dir = ""
    try:
        from novelvideo.ports import get_project_registry

        record = await get_project_registry().get_project(project_id)
        if record is not None:
            output_dir = str(getattr(record, "output_dir", "") or "")
    except Exception as exc:  # noqa: BLE001 - attribution must never break a call
        logger.debug("llm attribution project lookup failed: %s", exc)
    _PROJECT_DIR_BY_ID[project_id] = output_dir
    return output_dir


class LocalUsageMeter(NoOpUsageMeter):
    """CE usage meter: no credits, but records model-call attribution.

    Every override is best-effort and swallows errors so a diagnostics write
    can never fail or delay a model call.
    """

    def set_llm_usage_context(
        self,
        user_id: Optional[str],
        project_id: Optional[str] = None,
        resource_kind: str = "",
        billing_metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        super().set_llm_usage_context(
            user_id,
            project_id=project_id,
            resource_kind=resource_kind,
            billing_metadata=billing_metadata,
        )

    def clear_llm_usage_context(self) -> None:
        super().clear_llm_usage_context()
        _PROJECT_DIR_CTX.set("")
        _PENDING_TOKENS_CTX.set((0, 0))

    async def set_project_llm_usage_context(
        self,
        *,
        username: Optional[str],
        project_name: Optional[str],
        resource_kind: str = "",
        billing_metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        super().set_llm_usage_context(
            username or "local",
            resource_kind=resource_kind,
            billing_metadata=billing_metadata,
        )
        if not username or not project_name:
            return
        try:
            from novelvideo.utils.project_paths import ProjectPaths

            project_dir = ProjectPaths(str(username), str(project_name)).output_dir
        except Exception as exc:  # noqa: BLE001
            logger.debug("llm attribution project dir failed: %s", exc)
            return
        _PROJECT_DIR_CTX.set(str(project_dir))

    async def record_llm_tokens(
        self,
        *,
        user_id: Optional[str],
        input_tokens: int,
        output_tokens: int,
        model: Optional[str] = None,
        project_id: Optional[str] = None,
        resource_kind: str = "",
    ) -> None:
        in_tok, out_tok = _PENDING_TOKENS_CTX.get()
        _PENDING_TOKENS_CTX.set(
            (in_tok + int(input_tokens or 0), out_tok + int(output_tokens or 0))
        )

    async def bump_model_call(
        self,
        *,
        user_id: Optional[str],
        model: str = "",
        project_id: Optional[str] = None,
        resource_kind: str = "",
        provider_request_id: str = "",
        provider_task_id: str = "",
        credit_reservation_id: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        try:
            project_dir = _PROJECT_DIR_CTX.get() or await _resolve_project_output_dir(
                str(project_id or "")
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("llm attribution resolve failed: %s", exc)
            return
        in_tok, out_tok = _PENDING_TOKENS_CTX.get()
        _PENDING_TOKENS_CTX.set((0, 0))
        if not project_dir:
            return
        payload = metadata if isinstance(metadata, dict) else {}
        try:
            from novelvideo.llm_request_usage import record_llm_request

            await asyncio.to_thread(
                record_llm_request,
                project_output_dir=project_dir,
                request_id=uuid4().hex,
                model=str(model or ""),
                provider_request_id=str(provider_request_id or ""),
                provider_task_id=str(provider_task_id or ""),
                provider_response_id=str(payload.get("response_id") or ""),
                resource_kind=str(resource_kind or ""),
                episode=_episode_from_metadata(payload),
                user_id=user_id,
                input_tokens=in_tok or None,
                output_tokens=out_tok or None,
            )
        except Exception as exc:  # noqa: BLE001 - attribution is best-effort
            logger.debug("llm attribution write failed: %s", exc)
