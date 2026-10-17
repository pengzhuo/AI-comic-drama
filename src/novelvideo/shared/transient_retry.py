"""Bounded retry for transient upstream failures at leaf call sites.

The one-click pipeline pauses a whole episode when a beat task fails, so a single
5xx or dropped connection used to cost the user the entire run — and, for the
video stage, a whole 40-beat batch of retries. Leaf call sites use this helper to
ride out those cases in place instead of failing the task.

Only failures that are safe to repeat are retried:

* HTTP 408/425/429 and 5xx: the provider either asked for a retry or answered on
  the server side. A 5xx may already have been accepted and paid upstream, so a
  repeat risks one extra upstream generation on that (rare) path — the same
  asymmetry ``nanobanana_grid`` documents for the image leaf. Operators who need
  strict old behaviour set the per-call-site attempts variable to 1.
* transport errors raised while connecting (``ClientConnectorError`` and
  friends), i.e. the request never reached the provider.

Read timeouts and protocol errors are deliberately *not* retried: the work may
already be in flight and repeating it would pay twice. 4xx client errors are
never retried, and billing failures are re-raised untouched so a retry can never
spend credits the account does not have.

The image leaf keeps its own inline equivalent because its retry has to reuse a
credit reservation created once per logical request; this module is for the
call sites whose reservation (or absence of one) is unaffected by a repeat.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from novelvideo.shared.billing_errors import find_billing_error

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: HTTP statuses worth repeating. 408 (timeout) and 425 (too early) are
#: retryable by definition; 429 is rate limiting; 5xx is a transient upstream.
RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
#: Exception class names that mean the request never produced a response.
RETRYABLE_TRANSPORT_ERROR_NAMES = frozenset(
    {
        "ClientConnectorError",
        "ClientConnectionError",
        "ClientOSError",
        "ConnectError",
        "ConnectTimeout",
        "PoolTimeout",
        "ServerDisconnectedError",
        "ServerTimeoutError",
    }
)
DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = (5.0, 20.0)


def transient_retry_delay(
    attempt: int,
    backoff: tuple[float, ...] = DEFAULT_BACKOFF_SECONDS,
) -> float:
    """Seconds to wait before retry ``attempt`` (1-based), saturating at the end."""

    if not backoff:
        return 0.0
    index = min(max(int(attempt), 1), len(backoff)) - 1
    return float(backoff[index])


def exception_http_status(exc: BaseException) -> int | None:
    """Best-effort HTTP status of a provider exception, if it carries one.

    Covers the three shapes the gateways hand back: a raw ``status_code``
    (``NewApiVideoError``, billing errors), aiohttp's ``status``, and httpx's
    ``HTTPStatusError`` (which keeps the status on the response object).
    """

    for attribute in ("status_code", "status", "http_status"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def is_retryable_transient(exc: BaseException) -> bool:
    """Default predicate: retry only unambiguous transient provider failures."""

    if find_billing_error(exc) is not None:
        # A retry cannot create credits, and billing failures surface to the user.
        return False
    status = exception_http_status(exc)
    if status is not None:
        return status in RETRYABLE_HTTP_STATUS
    return type(exc).__name__ in RETRYABLE_TRANSPORT_ERROR_NAMES


async def call_with_transient_retry(
    func: Callable[[], Awaitable[T]],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    backoff: tuple[float, ...] = DEFAULT_BACKOFF_SECONDS,
    is_retryable: Callable[[BaseException], bool] = is_retryable_transient,
    label: str = "",
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Await ``func`` up to ``attempts`` times while the predicate allows it.

    ``attempts`` counts the first try, so the default 3 means "one try and two
    retries". Anything the predicate rejects is re-raised unchanged.
    """

    total = max(int(attempts), 1)
    for attempt in range(1, total + 1):
        try:
            return await func()
        except Exception as exc:  # noqa: BLE001 - the predicate is the policy
            if attempt >= total or not is_retryable(exc):
                raise
            delay = transient_retry_delay(attempt, backoff)
            logger.warning(
                "%s transient failure (%s: %s), retrying in %.0fs (%d/%d)",
                label or "call",
                type(exc).__name__,
                exc,
                delay,
                attempt,
                total,
            )
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            await asyncio.sleep(delay)
    raise AssertionError("unreachable: the retry loop always returns or raises")
