from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .models import PaperFill


logger = logging.getLogger(__name__)

FILL_EXECUTION_TYPES = {
    "EXECUTION_TYPE_PARTIAL_FILL",
    "EXECUTION_TYPE_FILL",
}
FINAL_ZERO_STATES = {"ORDER_STATE_CANCELED", "ORDER_STATE_EXPIRED"}
PENDING_STATES = {
    "ORDER_STATE_NEW",
    "ORDER_STATE_PENDING_NEW",
    "ORDER_STATE_PENDING_REPLACE",
    "ORDER_STATE_PENDING_CANCEL",
    "ORDER_STATE_PENDING_RISK",
}
RECONCILE_POLL_SECONDS = 0.1
RECONCILE_MAX_SECONDS = 5.0


@dataclass(frozen=True)
class LiveAttemptResult:
    order_id: str
    state: str
    filled_shares: float
    average_price: float | None
    fee: float
    classification: str
    raw: dict[str, Any]

    @property
    def notional(self) -> float:
        if self.average_price is None:
            return 0.0
        return self.filled_shares * self.average_price


class PolymarketLiveExecutor:
    """Submit and reconcile exactly one Polymarket US IOC order."""

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
    ) -> LiveAttemptResult:
        del tick_size, neg_risk
        return await asyncio.to_thread(
            self._buy_sync, token_id, max_price, requested_shares
        )

    def _buy_sync(
        self,
        token_id: str,
        max_price: float,
        requested_shares: int,
    ) -> LiveAttemptResult:
        market_slug, outcome = _split_market_side(token_id)
        client = self._client or self._build_client()
        self._client = client
        quantity = int(requested_shares)
        if quantity <= 0:
            raise ValueError("Live order quantity must be a positive integer")

        raw_limit_price = _raw_yes_price(outcome, max_price)
        payload = {
            "marketSlug": market_slug,
            "intent": (
                "ORDER_INTENT_BUY_LONG"
                if outcome == "YES"
                else "ORDER_INTENT_BUY_SHORT"
            ),
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{raw_limit_price:.6f}", "currency": "USD"},
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
                "Polymarket US submission unknown: market=%s intent=%s "
                "quantity=%s raw_yes_limit=%.6f economic_limit=%.6f "
                "error_type=%s error=%s",
                market_slug,
                payload["intent"],
                quantity,
                raw_limit_price,
                max_price,
                type(exc).__name__,
                safe_error,
            )
            return LiveAttemptResult(
                order_id="",
                state="UNKNOWN",
                filled_shares=0,
                average_price=None,
                fee=0,
                classification="submission_unknown",
                raw={"error_type": type(exc).__name__, "error": safe_error},
            )

        diagnostic = _response_diagnostic(raw, payload)
        logger.info("Polymarket US order response: %s", _format_diagnostic(diagnostic))
        immediate = _classify_response(raw, outcome, quantity)
        if immediate is not None:
            return immediate

        order_id = str(raw.get("id") or "") if isinstance(raw, dict) else ""
        known_fill = _known_fill(raw, outcome, quantity)
        if not order_id:
            return _submission_unknown(raw, known_fill, "UNKNOWN")

        deadline = time.monotonic() + RECONCILE_MAX_SECONDS
        while True:
            try:
                retrieved = client.orders.retrieve(order_id)
            except Exception as exc:
                safe_error = _safe_error_text(
                    exc,
                    self.settings.polymarket_us_key_id,
                    self.settings.polymarket_us_secret_key,
                )
                logger.error(
                    "Polymarket US order reconciliation failed: order_id=%s "
                    "error_type=%s error=%s",
                    order_id,
                    type(exc).__name__,
                    safe_error,
                )
                return _submission_unknown(
                    raw, known_fill, "RETRIEVE_FAILED", order_id
                )

            reconciled = _classify_response(retrieved, outcome, quantity, order_id)
            if reconciled is not None:
                logger.info(
                    "Polymarket US order reconciled: order_id=%s state=%s "
                    "classification=%s filled=%.4f",
                    order_id,
                    reconciled.state,
                    reconciled.classification,
                    reconciled.filled_shares,
                )
                return reconciled
            known_fill = _known_fill(retrieved, outcome, quantity) or known_fill
            if time.monotonic() >= deadline:
                return _submission_unknown(
                    retrieved, known_fill, "RECONCILE_TIMEOUT", order_id
                )
            time.sleep(RECONCILE_POLL_SECONDS)

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


def _raw_yes_price(outcome: str, economic_price: float) -> float:
    return economic_price if outcome == "YES" else 1.0 - economic_price


def _economic_price(outcome: str, raw_yes_price: float) -> float:
    return raw_yes_price if outcome == "YES" else 1.0 - raw_yes_price


def _classify_response(
    raw: Any,
    outcome: str,
    requested_shares: int,
    fallback_order_id: str = "",
) -> LiveAttemptResult | None:
    if not isinstance(raw, dict):
        return None
    order_id = str(raw.get("id") or fallback_order_id)
    executions = raw.get("executions")
    executions = executions if isinstance(executions, list) else []
    order = raw.get("order")
    if not isinstance(order, dict):
        order = _last_execution_order(executions)
    state = str(order.get("state") or "") if isinstance(order, dict) else ""
    fill = _aggregate_fill(order, executions, requested_shares, outcome)
    filled = fill.shares if fill else 0.0
    average = fill.average_price if fill else None
    fee = fill.fee if fill else 0.0
    execution_types = {
        str(item.get("type") or "")
        for item in executions
        if isinstance(item, dict)
    }

    if state == "ORDER_STATE_REJECTED" or "EXECUTION_TYPE_REJECTED" in execution_types:
        return LiveAttemptResult(
            order_id, state or "ORDER_STATE_REJECTED", filled, average, fee,
            "rejected", raw,
        )
    if state == "ORDER_STATE_FILLED":
        if filled <= 0 or average is None:
            return None
        return LiveAttemptResult(
            order_id, state, filled, average, fee, "filled", raw
        )
    if state == "ORDER_STATE_PARTIALLY_FILLED":
        if filled <= 0 or average is None:
            return None
        return LiveAttemptResult(
            order_id, state, filled, average, fee, "partial_fill", raw
        )
    if state in FINAL_ZERO_STATES:
        classification = "partial_fill" if filled > 0 else "confirmed_zero_fill"
        return LiveAttemptResult(
            order_id, state, filled, average, fee, classification, raw
        )
    if state in PENDING_STATES:
        return None

    if "EXECUTION_TYPE_CANCELED" in execution_types or "EXECUTION_TYPE_EXPIRED" in execution_types:
        classification = "partial_fill" if filled > 0 else "confirmed_zero_fill"
        return LiveAttemptResult(
            order_id, state or "TERMINAL", filled, average, fee, classification, raw
        )
    if (
        filled >= requested_shares - 0.0001
        and "EXECUTION_TYPE_FILL" in execution_types
    ):
        return LiveAttemptResult(
            order_id, state or "ORDER_STATE_FILLED", filled, average, fee,
            "filled", raw,
        )
    return None


def _known_fill(
    raw: Any, outcome: str, requested_shares: int
) -> PaperFill | None:
    if not isinstance(raw, dict):
        return None
    executions = raw.get("executions")
    executions = executions if isinstance(executions, list) else []
    order = raw.get("order")
    if not isinstance(order, dict):
        order = _last_execution_order(executions)
    return _aggregate_fill(order, executions, requested_shares, outcome)


def _aggregate_fill(
    order: Any,
    executions: list[Any],
    requested_shares: int,
    outcome: str,
) -> PaperFill | None:
    execution_fill = _fill_from_executions(executions, requested_shares, outcome)
    if isinstance(order, dict):
        shares = _optional_float(order.get("cumQuantity")) or 0
        raw_average = _amount_value(order.get("avgPx"))
        if shares > 0 and raw_average is not None:
            economic_average = _economic_price(outcome, raw_average)
            fee = _amount_value(order.get("commissionNotionalTotalCollected"))
            if fee is None:
                fee = execution_fill.fee if execution_fill else 0.0
            notional = shares * economic_average
            return PaperFill(
                shares=shares,
                notional=notional,
                fee=fee,
                total_cost=notional + fee,
                average_price=economic_average,
                fully_filled=shares >= requested_shares - 0.0001,
            )
    return execution_fill


def _fill_from_executions(
    executions: list[Any],
    requested_shares: int,
    outcome: str = "YES",
) -> PaperFill | None:
    shares = 0.0
    notional = 0.0
    fee = 0.0
    for execution in executions:
        if not isinstance(execution, dict):
            continue
        execution_type = str(execution.get("type") or "")
        if execution_type not in FILL_EXECUTION_TYPES and not execution.get("tradeId"):
            continue
        quantity = _optional_float(execution.get("lastShares")) or 0
        raw_price = _amount_value(execution.get("lastPx"))
        if quantity <= 0 or raw_price is None:
            continue
        price = _economic_price(outcome, raw_price)
        shares += quantity
        notional += quantity * price
        fee += _amount_value(execution.get("commissionNotionalCollected")) or 0
    if shares <= 0:
        return None
    return PaperFill(
        shares=shares,
        notional=notional,
        fee=fee,
        total_cost=notional + fee,
        average_price=notional / shares,
        fully_filled=shares >= requested_shares - 0.0001,
    )


def _last_execution_order(executions: list[Any]) -> dict[str, Any]:
    for execution in reversed(executions):
        if isinstance(execution, dict) and isinstance(execution.get("order"), dict):
            return execution["order"]
    return {}


def _submission_unknown(
    raw: Any,
    fill: PaperFill | None,
    state: str,
    fallback_order_id: str = "",
) -> LiveAttemptResult:
    safe_raw = raw if isinstance(raw, dict) else {"response_type": type(raw).__name__}
    return LiveAttemptResult(
        order_id=str(safe_raw.get("id") or fallback_order_id),
        state=state,
        filled_shares=fill.shares if fill else 0,
        average_price=fill.average_price if fill else None,
        fee=fill.fee if fill else 0,
        classification="submission_unknown",
        raw=safe_raw,
    )


def _amount_value(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("value")
    return _optional_float(value)


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _response_diagnostic(raw: Any, payload: dict[str, Any]) -> dict[str, Any]:
    diagnostic: dict[str, Any] = {
        "order_id": "",
        "market": str(payload.get("marketSlug") or ""),
        "intent": str(payload.get("intent") or ""),
        "quantity": payload.get("quantity"),
        "raw_yes_limit": _amount_value(payload.get("price")),
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


def _format_diagnostic(diagnostic: dict[str, Any]) -> str:
    summary_keys = (
        "order_id", "market", "intent", "quantity", "raw_yes_limit",
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
