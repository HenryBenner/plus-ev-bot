from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
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
    """Lazily loaded, explicitly gated Polymarket FAK order executor."""

    def __init__(self, settings: Settings):
        settings.validate_live_mode()
        self.settings = settings
        self._client: Any | None = None

    async def buy(
        self,
        *,
        token_id: str,
        max_price: float,
        expected_fill: PaperFill,
        tick_size: str,
        neg_risk: bool,
    ) -> LiveExecution:
        return await asyncio.to_thread(
            self._buy_sync,
            token_id,
            max_price,
            expected_fill,
            tick_size,
            neg_risk,
        )

    def _buy_sync(
        self,
        token_id: str,
        max_price: float,
        expected_fill: PaperFill,
        tick_size: str,
        neg_risk: bool,
    ) -> LiveExecution:
        types = _sdk_types()
        client = self._client or self._build_client(types)
        self._client = client
        size = Decimal(str(expected_fill.shares)).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN
        )
        if size <= 0:
            raise RuntimeError("Live order size rounded to zero")
        order_args = types["OrderArgs"](
            token_id=token_id,
            price=float(max_price),
            size=float(size),
            side=types["Side"].BUY,
        )
        options = types["PartialCreateOrderOptions"](
            tick_size=str(tick_size),
            neg_risk=bool(neg_risk),
        )
        try:
            try:
                response = client.create_and_post_order(
                    order_args=order_args,
                    options=options,
                    order_type=types["OrderType"].FAK,
                )
            except TypeError:
                response = client.create_and_post_order(
                    order_args, options, types["OrderType"].FAK
                )
        except Exception as exc:
            raise RuntimeError(
                "Live order submission failed. The signal will not be retried "
                f"automatically to prevent a duplicate order: {exc}"
            ) from exc

        raw = _as_dict(response)
        if raw.get("success") is False:
            raise RuntimeError(
                f"Polymarket rejected live FAK order: "
                f"{raw.get('errorMsg') or raw}"
            )
        status = str(raw.get("status") or "").casefold()
        if status not in {"matched", "filled", "delayed"}:
            raise RuntimeError(
                f"Live order state is not confirmed: status={status or 'missing'}"
            )
        order_id = str(
            raw.get("orderID")
            or raw.get("orderId")
            or raw.get("order_id")
            or ""
        )
        actual_fill = _fill_from_response(raw, expected_fill)
        return LiveExecution(
            order_id=order_id,
            status=status,
            fill=actual_fill,
            raw=raw,
        )

    def _build_client(self, types: dict[str, Any]) -> Any:
        creds = types["ApiCreds"](
            api_key=self.settings.polymarket_api_key,
            api_secret=self.settings.polymarket_api_secret,
            api_passphrase=self.settings.polymarket_api_passphrase,
        )
        signature_type: Any = self.settings.polymarket_signature_type
        if signature_type == 3:
            signature_type = types["SignatureTypeV2"].POLY_1271
        return types["ClobClient"](
            host=self.settings.polymarket_clob_url,
            chain_id=137,
            key=self.settings.polymarket_private_key,
            creds=creds,
            signature_type=signature_type,
            funder=self.settings.polymarket_funder_address,
        )


def _sdk_types() -> dict[str, Any]:
    try:
        from py_clob_client_v2 import (
            ApiCreds,
            ClobClient,
            OrderArgs,
            OrderType,
            PartialCreateOrderOptions,
            Side,
            SignatureTypeV2,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Live mode requires the official SDK. Install with: "
            'python -m pip install -e ".[live]"'
        ) from exc
    return {
        "ApiCreds": ApiCreds,
        "ClobClient": ClobClient,
        "OrderArgs": OrderArgs,
        "OrderType": OrderType,
        "PartialCreateOrderOptions": PartialCreateOrderOptions,
        "Side": Side,
        "SignatureTypeV2": SignatureTypeV2,
    }


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True)
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    raise RuntimeError(f"Polymarket returned an unsupported order response: {value}")


def _fill_from_response(
    raw: dict[str, Any], expected_fill: PaperFill
) -> PaperFill:
    making = _atomic_amount(
        raw.get("makingAmount")
        or raw.get("making_amount")
        or raw.get("sizeMatched")
        or raw.get("size_matched")
    )
    taking = _atomic_amount(
        raw.get("takingAmount") or raw.get("taking_amount")
    )
    if making is None or taking is None or taking <= 0:
        return expected_fill
    notional = making
    shares = taking
    average_price = notional / shares
    estimated_fee = max(expected_fill.total_cost - expected_fill.notional, 0)
    return PaperFill(
        shares=shares,
        notional=notional,
        fee=estimated_fee,
        total_cost=notional + estimated_fee,
        average_price=average_price,
        fully_filled=expected_fill.fully_filled,
    )


def _atomic_amount(value: Any) -> float | None:
    if value in (None, ""):
        return None
    amount = float(value)
    return amount / 1_000_000 if amount > 10_000 else amount
