from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any

import httpx

from .clients import PolymarketClient, PredictionHuntClient
from .config import Settings
from .db import Database
from .live import PolymarketLiveExecutor
from .models import FadeSignal, MarketInfo
from .paper import simulate_market_buy

logger = logging.getLogger(__name__)


class TradingService:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(
                self.settings.api_read_timeout_seconds,
                connect=10,
            ),
            follow_redirects=True,
            headers={"User-Agent": "prediction-hunt-fade-paper-bot/0.1"},
        )
        self.prediction_hunt = PredictionHuntClient(settings)
        self.polymarket = PolymarketClient(settings, self.http)
        self.live_executor = (
            PolymarketLiveExecutor(settings)
            if settings.trading_mode == "live"
            else None
        )
        self._tasks: list[asyncio.Task[Any]] = []

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self.signal_loop(), name="fade-signals"),
            asyncio.create_task(self.settlement_loop(), name="settlements"),
        ]
        if self.settings.trading_mode == "paper":
            self._tasks.append(
                asyncio.create_task(
                    self.reprocess_started_rejections(),
                    name="started-signal-backfill",
                )
            )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.http.aclose()

    async def signal_loop(self) -> None:
        async for message in self.prediction_hunt.fade_messages():
            try:
                await self.process_message(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected error processing Fade Finder message")

    async def reprocess_started_rejections(self) -> None:
        messages = await self.database.rejected_signal_messages(
            "event_already_started"
        )
        if not messages:
            return
        logger.info(
            "Reconsidering %d signals previously rejected because the event started",
            len(messages),
        )
        for message in messages:
            try:
                await self.process_message(message, allow_existing=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Could not reconsider a previously rejected signal")
            await asyncio.sleep(0.1)
        logger.info("Finished reconsidering previously started-event signals")

    async def process_message(
        self,
        message: dict[str, Any],
        now: datetime | None = None,
        *,
        allow_existing: bool = False,
    ) -> None:
        received_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        try:
            signal = FadeSignal.from_message(message, received_at=received_at)
        except (TypeError, ValueError) as exc:
            logger.warning("Ignored malformed Fade Finder message: %s", exc)
            return

        added = await self.database.add_signal(signal)
        if not added and not allow_existing:
            logger.debug("Duplicate Fade Finder signal %s", signal.signal_id)
            return

        try:
            market = await self.polymarket.resolve_market(
                signal.market_slug, signal.title
            )
        except Exception as exc:
            await self.database.reject_signal(
                signal.signal_id, f"market_resolution_failed:{type(exc).__name__}"
            )
            logger.warning("Could not resolve %s: %s", signal.market_slug, exc)
            return

        eligibility_reason = self.eligibility_reason(signal, market, received_at)
        if eligibility_reason:
            await self.database.reject_signal(signal.signal_id, eligibility_reason)
            return

        try:
            token_id = market.token_for(signal.paper_outcome)
            book = await self.polymarket.orderbook(token_id)
            asks = [
                (float(level["price"]), float(level["size"]))
                for level in book.get("asks") or []
            ]
            tick_size = str(book.get("tick_size") or "0.01")
            max_price = _floor_to_tick(
                min(
                    signal.paper_reference_price
                    + self.settings.max_price_drift,
                    0.99,
                ),
                tick_size,
            )
            if asks and min(price for price, _ in asks) > max_price:
                await self.database.reject_signal(
                    signal.signal_id, "price_guard_exceeded"
                )
                return
            fill = simulate_market_buy(
                asks,
                self.settings.paper_stake_usd,
                fees_enabled=market.fees_enabled,
                fee_rate=market.fee_rate,
                min_order_size=float(book.get("min_order_size") or 0),
                max_price=max_price,
            )
        except Exception as exc:
            await self.database.reject_signal(
                signal.signal_id, f"orderbook_failed:{type(exc).__name__}"
            )
            logger.warning("Could not simulate fill for %s: %s", market.market_slug, exc)
            return

        if fill is None:
            await self.database.reject_signal(signal.signal_id, "unfilled_no_liquidity")
            return

        execution_mode = self.settings.trading_mode
        external_order_id = None
        external_status = None
        if self.live_executor is not None:
            try:
                execution = await self.live_executor.buy(
                    token_id=token_id,
                    max_price=max_price,
                    expected_fill=fill,
                    tick_size=tick_size,
                    neg_risk=market.neg_risk,
                )
                fill = execution.fill
                external_order_id = execution.order_id
                external_status = execution.status
            except Exception as exc:
                await self.database.reject_signal(
                    signal.signal_id,
                    f"live_order_failed:{type(exc).__name__}",
                )
                logger.exception(
                    "Live order failed for %s; it was not retried",
                    market.market_slug,
                )
                return

        await self.database.create_trade(
            signal,
            market,
            token_id,
            fill,
            received_at,
            execution_mode=execution_mode,
            max_price=max_price,
            external_order_id=external_order_id,
            external_status=external_status,
        )
        logger.info(
            "%s trade %s %s: %.4f shares @ %.4f, cost $%.2f (ceiling %.3f)",
            execution_mode.capitalize(),
            signal.paper_outcome,
            market.market_slug,
            fill.shares,
            fill.average_price,
            fill.total_cost,
            max_price,
        )

    def eligibility_reason(
        self, signal: FadeSignal, market: MarketInfo, now: datetime
    ) -> str | None:
        return eligibility_reason(
            signal,
            market,
            now,
            max_event_hours=self.settings.max_event_hours,
        )

    async def settlement_loop(self) -> None:
        while True:
            try:
                await self.settle_open_trades()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Settlement poll failed")
            await asyncio.sleep(self.settings.settlement_poll_seconds)

    async def settle_open_trades(self) -> None:
        trades = await self.database.open_trades()
        for trade in trades:
            try:
                market = await self.polymarket.market_by_slug(trade["market_slug"])
                if not market.closed:
                    continue
                final_price = market.final_price_for(trade["outcome"])
                if not 0 <= final_price <= 1:
                    continue
                resolved_outcome = _resolved_label(market)
                await self.database.settle_trade(
                    int(trade["id"]),
                    final_price=final_price,
                    resolved_outcome=resolved_outcome,
                    settled_at=datetime.now(timezone.utc),
                )
                logger.info(
                    "Settled trade %s at %.2f (%s)",
                    trade["id"],
                    final_price,
                    resolved_outcome,
                )
            except Exception:
                logger.exception("Could not settle trade %s", trade["id"])


def _resolved_label(market: MarketInfo) -> str:
    if not market.outcome_prices:
        return "unknown"
    winner_index = max(
        range(len(market.outcome_prices)), key=market.outcome_prices.__getitem__
    )
    if market.outcome_prices[winner_index] >= 0.99:
        return market.outcomes[winner_index]
    if all(abs(price - 0.5) <= 0.01 for price in market.outcome_prices):
        return "void"
    return "split"


def eligibility_reason(
    signal: FadeSignal,
    market: MarketInfo,
    now: datetime,
    *,
    max_event_hours: int = 72,
) -> str | None:
    expiration_time = market.expiration_time
    if expiration_time is None and signal.resolution_date:
        from .models import parse_datetime

        expiration_time = parse_datetime(signal.resolution_date)
        if expiration_time and len(signal.resolution_date.strip()) == 10:
            expiration_time += timedelta(days=1)
    if expiration_time is None:
        expiration_time = market.event_time
    if expiration_time is None:
        return "missing_expiration_time"

    delta = expiration_time - now
    if delta.total_seconds() < 0:
        return "market_already_expired"
    if delta > timedelta(hours=max_event_hours):
        return "event_outside_window"
    if market.closed:
        return "market_closed"
    return None


def _floor_to_tick(price: float, tick_size: str) -> float:
    tick = Decimal(tick_size)
    if tick <= 0:
        raise ValueError("tick size must be positive")
    value = Decimal(str(price))
    return float((value / tick).to_integral_value(rounding=ROUND_DOWN) * tick)
