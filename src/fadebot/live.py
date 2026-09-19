from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .models import PaperFill


logger = logging.getLogger(__name__)

FILL_EXECUTION_TYPES = {
    "EXECUTION_TYPE_PARTIAL_FILL",
    "EXECUTION_TYPE_FILL",
}


class LiveOrderError(RuntimeError):
    """A classified live-order outcome that must never be retried automatically."""

    def __init__(self, classification: str, message: str):
        super().__init__(message)
        self.classification = classification


@dataclass(frozen=True)
class LiveExecution:
    order_id: str
    status: str
    fill: PaperFill
    raw: dict[str, Any]


class PolymarketLiveExecutor:
    """Explicitly gated Polymarket US immediate-or-cancel executor."""

    def __init__(self, settings: Settings):
        settings.validate_live_mode()
        self.settings = settings
        self._client: Any | None = None

    async def buy(
        self,
        *,
        token_id: str,
        max_price: float,
        requested_shares: int,
        tick_size: str,
        neg_risk: bool,
    ) -> LiveExecution:
        del tick_size, neg_risk
        return await asyncio.to_thread(
            self._buy_sync, token_id, max_price, requested_shares
        )

    def _buy_sync(
        self,
        token_id: str,
        max_price: float,
        requested_shares: int,
    ) -> LiveExecution:
        market_slug, outcome = _split_market_side(token_id)
        client = self._client or self._build_client()
        self._client = client
        quantity = int(requested_shares)
        if quantity <= 0:
            raise RuntimeError("Live order quantity must be a positive integer")

        payload = {
            "marketSlug": market_slug,
            "intent": (
                "ORDER_INTENT_BUY_LONG"
                if outcome == "YES"
                else "ORDER_INTENT_BUY_SHORT"
            ),
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{max_price:.6f}", "currency": "USD"},
            "quantity": quantity,
            "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
            "participateDontInitiate": False,
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            "synchronousExecution": True,
            "maxBlockTime": "10",
        }
        try:
            raw = client.orders.create(payload)
        except Exception as exc:
            safe_error = _safe_error_text(
                exc,
                self.settings.polymarket_us_key_id,
                self.settings.polymarket_us_secret_key,
            )
            logger.error(
                "Polymarket US order submission error: market=%s intent=%s "
                "quantity=%s limit_price=%.6f error_type=%s error=%s",
                market_slug, payload["intent"], quantity, max_price,
                type(exc).__name__, safe_error,
            )
            raise LiveOrderError(
                "submission_error",
                "Polymarket US order submission failed. It will not be retried "
                f"automatically to prevent a duplicate order: {safe_error}"
            ) from exc

        diagnostic = _response_diagnostic(raw, payload)
        logger.info("Polymarket US order response: %s", _format_diagnostic(diagnostic))
        if not isinstance(raw, dict):
            raise LiveOrderError(
                "invalid_response",
                "Polymarket US returned an invalid order response: "
                + _format_diagnostic(diagnostic),
            )
        if not isinstance(raw.get("executions"), list):
            raise LiveOrderError(
                "invalid_response",
                "Polymarket US returned an invalid executions response: "
                + _format_diagnostic(diagnostic),
            )

        fill = _fill_from_executions(raw["executions"], requested_shares)
        if fill is None:
            classification = _zero_fill_classification(diagnostic)
            label = {
                "rejected": "Polymarket US IOC rejected",
                "canceled_no_fill": "Polymarket US IOC canceled with no fill",
                "expired_no_fill": "Polymarket US IOC expired with no fill",
                "ioc_no_fill": "Polymarket US IOC received no fill",
            }[classification]
            raise LiveOrderError(
                classification,
                f"{label}: {_format_diagnostic(diagnostic)}",
            )
        return LiveExecution(
            order_id=str(raw.get("id") or ""),
            status="filled" if fill.fully_filled else "partially_filled",
            fill=fill,
            raw=raw,
        )

    def _build_client(self) -> Any:
        try:
            from polymarket_us import PolymarketUS
        except ImportError as exc:
            raise RuntimeError(
                "Install the Polymarket US SDK with: python -m pip install ."
            ) from exc
        return PolymarketUS(
            key_id=self.settings.polymarket_us_key_id,
            secret_key=self.settings.polymarket_us_secret_key,
            gateway_base_url=self.settings.polymarket_us_gateway_url,
            api_base_url=self.settings.polymarket_us_api_url,
            timeout=float(self.settings.api_read_timeout_seconds),
        )


def _split_market_side(value: str) -> tuple[str, str]:
    slug, separator, outcome = value.rpartition("::")
    if not separator or outcome not in {"YES", "NO"}:
        raise ValueError("Invalid Polymarket US market-side identifier")
    return slug, outcome


def _fill_from_executions(
    executions: list[dict[str, Any]], requested_shares: int
) -> PaperFill | None:
    shares = 0.0
    notional = 0.0
    fee = 0.0
    for execution in executions:
        if not isinstance(execution, dict):
            continue
        execution_type = str(execution.get("type") or "")
        # The REST API documents lastShares and lastPx on fill executions.
        # tradeId is useful evidence but is not required: some SDK/API response
        # variants identify fills by their execution type alone.
        if execution_type not in FILL_EXECUTION_TYPES and not execution.get("tradeId"):
            continue
        quantity = _optional_float(execution.get("lastShares")) or 0
        price = _amount_value(execution.get("lastPx"))
        if quantity <= 0 or price is None:
            continue
        shares += quantity
        notional += quantity * price
        fee += _amount_value(execution.get("commissionNotionalCollected")) or 0

    # A synchronous response can expose the aggregate fill on the nested order
    # even if it omits per-fill values. Use only one final order snapshot here
    # so cumulative quantity is not double counted across executions.
    if shares <= 0:
        for execution in reversed(executions):
            if not isinstance(execution, dict):
                continue
            order = execution.get("order")
            if not isinstance(order, dict):
                continue
            cumulative = _optional_float(order.get("cumQuantity")) or 0
            average_price = _amount_value(order.get("avgPx"))
            if cumulative <= 0 or average_price is None:
                continue
            shares = cumulative
            notional = cumulative * average_price
            fee = _amount_value(order.get("commissionNotionalTotalCollected")) or 0
            break
    if shares <= 0:
        return None
    total_cost = notional + fee
    return PaperFill(
        shares=shares,
        notional=notional,
        fee=fee,
        total_cost=total_cost,
        average_price=notional / shares,
        fully_filled=shares >= requested_shares - 0.0001,
    )


def _amount_value(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("value")
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _response_diagnostic(raw: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Return only order/execution fields that are safe to write to logs."""
    diagnostic: dict[str, Any] = {
        "order_id": "",
        "market": str(payload.get("marketSlug") or ""),
        "intent": str(payload.get("intent") or ""),
        "quantity": payload.get("quantity"),
        "limit_price": _amount_value(payload.get("price")),
        "executions": 0,
        "states": [],
        "execution_types": [],
        "trade_ids": 0,
        "execution_details": [],
        "response_type": type(raw).__name__,
    }
    if not isinstance(raw, dict):
        return diagnostic

    diagnostic["order_id"] = str(raw.get("id") or "")
    executions = raw.get("executions")
    if not isinstance(executions, list):
        diagnostic["executions_value_type"] = type(executions).__name__
        return diagnostic
    diagnostic["executions"] = len(executions)
    states: list[str] = []
    execution_types: list[str] = []
    details: list[dict[str, Any]] = []
    trade_ids = 0
    for execution in executions:
        if not isinstance(execution, dict):
            details.append({"value_type": type(execution).__name__})
            continue
        order = execution.get("order")
        order = order if isinstance(order, dict) else {}
        state = str(order.get("state") or "")
        execution_type = str(execution.get("type") or "")
        trade_id = str(execution.get("tradeId") or "")
        if state and state not in states:
            states.append(state)
        if execution_type:
            execution_types.append(execution_type)
        if trade_id:
            trade_ids += 1
        details.append({
            "type": execution_type,
            "trade_id": trade_id,
            "last_shares": execution.get("lastShares"),
            "last_px": _amount_value(execution.get("lastPx")),
            "order_state": state,
            "cumulative_quantity": order.get("cumQuantity"),
            "leaves_quantity": order.get("leavesQuantity"),
            "average_price": _amount_value(order.get("avgPx")),
            "text": str(execution.get("text") or ""),
            "reject_reason": str(execution.get("orderRejectReason") or ""),
        })
    diagnostic["states"] = states
    diagnostic["execution_types"] = execution_types
    diagnostic["trade_ids"] = trade_ids
    diagnostic["execution_details"] = details
    return diagnostic


def _zero_fill_classification(diagnostic: dict[str, Any]) -> str:
    types = set(diagnostic.get("execution_types") or [])
    states = set(diagnostic.get("states") or [])
    if "EXECUTION_TYPE_REJECTED" in types or "ORDER_STATE_REJECTED" in states:
        return "rejected"
    if "EXECUTION_TYPE_CANCELED" in types or "ORDER_STATE_CANCELED" in states:
        return "canceled_no_fill"
    if "EXECUTION_TYPE_EXPIRED" in types or "ORDER_STATE_EXPIRED" in states:
        return "expired_no_fill"
    return "ioc_no_fill"


def _format_diagnostic(diagnostic: dict[str, Any]) -> str:
    summary_keys = (
        "order_id", "market", "intent", "quantity", "limit_price",
        "executions", "states", "execution_types", "trade_ids",
        "response_type", "executions_value_type",
    )
    parts = [
        f"{key}={diagnostic[key]}"
        for key in summary_keys
        if key in diagnostic
    ]
    parts.append(f"execution_details={diagnostic.get('execution_details', [])}")
    return " ".join(parts)


def _safe_error_text(exc: Exception, *sensitive_values: str) -> str:
    # SDK exceptions contain HTTP status/body details. Limit length and flatten
    # whitespace, then redact common authentication fields and configured keys.
    text = " ".join(str(exc).split())
    text = re.sub(
        r"(?i)(authorization|x-pm-access-key|x-pm-signature|secret[_ -]?key|"
        r"private[_ -]?key)(\s*[:=]\s*)([^\s,;}]+)",
        r"\1\2[REDACTED]",
        text,
    )
    for value in sensitive_values:
        if value:
            text = text.replace(value, "[REDACTED]")
    return text[:1000]
