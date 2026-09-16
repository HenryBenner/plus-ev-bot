from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .models import PaperFill


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
            raise RuntimeError(
                "Polymarket US order submission failed. It will not be retried "
                f"automatically to prevent a duplicate order: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise RuntimeError("Polymarket US returned an invalid order response")

        fill = _fill_from_executions(raw.get("executions") or [], requested_shares)
        if fill is None:
            raise RuntimeError("Polymarket US IOC order received no confirmed fill")
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
        if not execution.get("tradeId"):
            continue
        quantity = float(execution.get("lastShares") or 0)
        price = _amount_value(execution.get("lastPx"))
        if quantity <= 0 or price is None:
            continue
        shares += quantity
        notional += quantity * price
        fee += _amount_value(execution.get("commissionNotionalCollected")) or 0
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
    return float(value)
