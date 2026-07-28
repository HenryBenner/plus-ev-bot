from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(f"{normalized}T00:00:00+00:00")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class FadeSignal:
    signal_id: str
    received_at: datetime
    source_ts: float | None
    created_at: datetime
    event_id: int | None
    group_id: int | None
    market_slug: str
    title: str
    resolution_date: str | None
    snapshot: bool
    profitable_wallet_count: int | None
    time_delta_min: float | None
    profitable_wallet_address: str | None
    profitable_wallet_pnl: float | None
    profitable_outcome: str
    profitable_side: str
    signal_price: float
    losing_wallet_address: str | None
    losing_wallet_pnl: float | None
    raw: dict[str, Any]

    @classmethod
    def from_message(
        cls, message: dict[str, Any], received_at: datetime | None = None
    ) -> "FadeSignal":
        if message.get("channel") != "fade_finder":
            raise ValueError("not a fade_finder message")
        if message.get("type") != "fade_finder_update":
            raise ValueError("not a fade_finder_update")

        outer = message.get("data") or {}
        payload = outer.get("data") or {}
        profitable = payload.get("profitable_wallet") or {}
        losing = payload.get("losing_wallet") or {}
        created_at = parse_datetime(outer.get("created_at"))
        market_slug = str(
            outer.get("market_slug") or payload.get("marketSlug") or ""
        ).strip()
        outcome = str(profitable.get("outcome") or "").strip().upper()
        side = str(profitable.get("side") or "BUY").strip().upper()
        price = float(profitable.get("price"))

        if not created_at:
            raise ValueError("signal is missing a valid created_at")
        if not market_slug:
            raise ValueError("signal is missing market_slug")
        if not 0 < price < 1:
            raise ValueError("signal price must be between zero and one")
        if outcome not in {"YES", "NO"}:
            raise ValueError("profitable wallet outcome must be YES or NO")
        if side not in {"BUY", "SELL"}:
            raise ValueError("profitable wallet side must be BUY or SELL")

        identity = {
            "created_at": created_at.isoformat(),
            "event_id": outer.get("event_id"),
            "group_id": outer.get("group_id"),
            "market_slug": market_slug,
            "wallet": profitable.get("userAddr"),
            "outcome": outcome,
            "side": side,
            "price": price,
        }
        signal_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return cls(
            signal_id=signal_id,
            received_at=(received_at or datetime.now(timezone.utc)).astimezone(
                timezone.utc
            ),
            source_ts=float(message["ts"]) if message.get("ts") is not None else None,
            created_at=created_at,
            event_id=_optional_int(outer.get("event_id")),
            group_id=_optional_int(outer.get("group_id")),
            market_slug=market_slug,
            title=str(outer.get("title") or market_slug),
            resolution_date=payload.get("resolution_date"),
            snapshot=bool(outer.get("snapshot")),
            profitable_wallet_count=_optional_int(
                payload.get("profitable_wallet_count")
            ),
            time_delta_min=_optional_float(payload.get("time_delta_min")),
            profitable_wallet_address=profitable.get("userAddr"),
            profitable_wallet_pnl=_optional_float(profitable.get("pnl_to_date")),
            profitable_outcome=outcome,
            profitable_side=side,
            signal_price=price,
            losing_wallet_address=losing.get("userAddr"),
            losing_wallet_pnl=_optional_float(losing.get("pnl_to_date")),
            raw=message,
        )

    @property
    def paper_outcome(self) -> str:
        if self.profitable_side == "BUY":
            return self.profitable_outcome
        return "NO" if self.profitable_outcome == "YES" else "YES"

    @property
    def paper_reference_price(self) -> float:
        if self.profitable_side == "BUY":
            return self.signal_price
        return 1.0 - self.signal_price


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def _optional_float(value: Any) -> float | None:
    return float(value) if value is not None else None


@dataclass(frozen=True)
class MarketInfo:
    market_id: str
    condition_id: str
    market_slug: str
    event_slug: str
    title: str
    event_time: datetime | None
    category: str
    outcomes: list[str]
    token_ids: list[str]
    outcome_prices: list[float]
    closed: bool
    fees_enabled: bool
    fee_rate: float = 0.05
    neg_risk: bool = False
    expiration_time: datetime | None = None

    def token_for(self, outcome: str) -> str:
        wanted = outcome.casefold()
        for index, candidate in enumerate(self.outcomes):
            if candidate.casefold() == wanted:
                return self.token_ids[index]
        raise ValueError(f"market does not contain outcome {outcome}")

    def final_price_for(self, outcome: str) -> float:
        wanted = outcome.casefold()
        for index, candidate in enumerate(self.outcomes):
            if candidate.casefold() == wanted:
                return self.outcome_prices[index]
        raise ValueError(f"market does not contain outcome {outcome}")


@dataclass(frozen=True)
class PaperFill:
    shares: float
    notional: float
    fee: float
    total_cost: float
    average_price: float
    fully_filled: bool
