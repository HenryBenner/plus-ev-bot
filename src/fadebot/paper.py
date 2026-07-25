from __future__ import annotations

from collections.abc import Iterable

from .models import PaperFill


def taker_fee(shares: float, price: float, fee_rate: float) -> float:
    return round(shares * fee_rate * price * (1.0 - price), 5)


def simulate_market_buy(
    asks: Iterable[tuple[float, float]],
    budget: float,
    *,
    fees_enabled: bool,
    fee_rate: float = 0.03,
    min_order_size: float = 0,
    max_price: float | None = None,
) -> PaperFill | None:
    """Walk asks using a total cash budget, including taker fees."""
    if budget <= 0:
        raise ValueError("budget must be positive")
    remaining = budget
    shares = 0.0
    notional = 0.0
    fee = 0.0

    levels = sorted(
        ((float(price), float(size)) for price, size in asks),
        key=lambda item: item[0],
    )
    for price, available in levels:
        if max_price is not None and price > max_price + 1e-9:
            break
        if not (0 < price < 1) or available <= 0 or remaining <= 1e-9:
            continue
        per_share_fee = fee_rate * price * (1.0 - price) if fees_enabled else 0.0
        cash_per_share = price + per_share_fee
        quantity = min(available, remaining / cash_per_share)
        if quantity <= 0:
            continue
        level_notional = quantity * price
        level_fee = (
            taker_fee(quantity, price, fee_rate) if fees_enabled else 0.0
        )
        level_cost = level_notional + level_fee
        if level_cost > remaining:
            quantity *= remaining / level_cost
            level_notional = quantity * price
            level_fee = (
                taker_fee(quantity, price, fee_rate) if fees_enabled else 0.0
            )
            level_cost = level_notional + level_fee
        shares += quantity
        notional += level_notional
        fee += level_fee
        remaining -= level_cost

    if shares <= 0 or shares + 1e-9 < min_order_size:
        return None
    total_cost = notional + fee
    return PaperFill(
        shares=shares,
        notional=notional,
        fee=fee,
        total_cost=total_cost,
        average_price=notional / shares,
        fully_filled=total_cost >= budget - 0.005,
    )
