from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any

import httpx

from .clients import (
    PolymarketInternationalClient,
    PolymarketUSClient,
    PredictionHuntClient,
    normalize_filter,
)
from .config import Settings
from .db import Database
from .live import LiveOrderError, PolymarketLiveExecutor
from .mapping import InternationalToUSMapper, MappingError
from .models import FadeSignal, MarketInfo
from .paper import simulate_market_buy, simulate_share_buy

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
        self.polymarket = PolymarketInternationalClient(settings, self.http)
        self.polymarket_us = PolymarketUSClient(settings, self.http)
        self.market_mapper = InternationalToUSMapper(
            self.polymarket_us, database
        )
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
            if str(exc) == "signal is missing a valid created_at":
                logger.warning(
                    "Ignored malformed Fade Finder message: %s; summary=%s",
                    exc,
                    _malformed_message_summary(message),
                )
            else:
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

        await self._paper_trade(signal, market, received_at)
        if self.settings.trading_mode == "live":
            await self._live_trade(signal, market, received_at)

    async def _paper_trade(
        self, signal: FadeSignal, market: MarketInfo, received_at: datetime
    ) -> None:
        try:
            token_id = market.token_for(signal.paper_outcome)
            book = await self.polymarket.orderbook(token_id)
            asks = [
                (float(level["price"]), float(level["size"]))
                for level in book.get("asks") or []
            ]
            tick_size = str(book.get("tick_size") or "0.01")
            max_price = _floor_to_tick(
                min(signal.paper_reference_price + self.settings.max_price_drift, 0.99),
                tick_size,
            )
            if asks and min(price for price, _ in asks) > max_price:
                await self.database.reject_signal(signal.signal_id, "price_guard_exceeded")
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
            logger.warning("Could not simulate paper fill for %s: %s", market.market_slug, exc)
            return
        if fill is None:
            await self.database.reject_signal(signal.signal_id, "unfilled_no_liquidity")
            return
        await self.database.create_trade(
            signal,
            market,
            token_id,
            fill,
            received_at,
            execution_mode="paper",
            max_price=max_price,
            platform=market.platform,
            outcome=signal.paper_outcome,
        )
        logger.info(
            "Paper trade %s %s: %.4f shares @ %.4f, cost $%.2f (ceiling %.3f)",
            signal.paper_outcome,
            market.market_slug,
            fill.shares,
            fill.average_price,
            fill.total_cost,
            max_price,
        )

    async def _live_trade(
        self, signal: FadeSignal, market: MarketInfo, received_at: datetime
    ) -> None:
        async def reject(reason: str) -> None:
            await self.database.record_live_attempt(signal.signal_id, "rejected", reason)
            logger.info("Live signal %s rejected: %s", signal.signal_id, reason)

        filter_reason = live_filter_reason(market, self.settings)
        if filter_reason:
            await reject(filter_reason)
            return
        try:
            mapping = await self.market_mapper.map_market(market)
        except MappingError as exc:
            await reject(f"us_mapping_failed:{exc}")
            return
        except Exception as exc:
            await reject(f"us_mapping_error:{type(exc).__name__}")
            logger.warning("US mapping failed for %s: %s", market.market_slug, exc)
            return
        target_market = mapping.market
        target_outcome = mapping.target_outcome(signal.paper_outcome)
        try:
            token_id = target_market.token_for(target_outcome)
            book = await self.polymarket_us.orderbook(token_id)
            asks = [
                (float(level["price"]), float(level["size"]))
                for level in book.get("asks") or []
            ]
            tick_size = str(book.get("tick_size") or "0.01")
            max_price = live_price_ceiling(signal.paper_reference_price, self.settings, tick_size)
            if asks:
                price_reason = live_entry_price_reason(min(price for price, _ in asks), self.settings)
                if price_reason:
                    await reject(price_reason)
                    return
            if asks and min(price for price, _ in asks) > max_price:
                await reject("price_guard_exceeded")
                return
            fill = simulate_share_buy(
                asks,
                self.settings.live_shares_per_trade,
                fees_enabled=target_market.fees_enabled,
                fee_rate=target_market.fee_rate,
                min_order_size=float(book.get("min_order_size") or 0),
                max_price=max_price,
            )
        except Exception as exc:
            await reject(f"orderbook_failed:{type(exc).__name__}")
            logger.warning("Could not simulate live fill for %s: %s", target_market.market_slug, exc)
            return
        if fill is None:
            await reject("unfilled_no_liquidity")
            return
        if not await self.database.claim_live_market_side(
            target_market.market_slug, target_outcome, signal.signal_id
        ):
            await reject("live_opposite_side_already_attempted")
            return
        best_ask = min((price for price, _ in asks), default=None)
        available_shares = sum(
            size for price, size in asks if price <= max_price + 1e-9
        )
        logger.info(
            "LIVE ORDER ATTEMPT signal_id=%s source_market=%s us_market=%s "
            "outcome=%s quantity=%s best_ask=%s limit_price=%.6f "
            "available_shares_at_or_below_limit=%.4f",
            signal.signal_id,
            market.market_slug,
            target_market.market_slug,
            target_outcome,
            self.settings.live_shares_per_trade,
            best_ask,
            max_price,
            available_shares,
        )
        try:
            execution = await self.live_executor.buy(
                token_id=token_id,
                max_price=max_price,
                requested_shares=self.settings.live_shares_per_trade,
                tick_size=tick_size,
                neg_risk=target_market.neg_risk,
            )
        except LiveOrderError as exc:
            await reject(f"live_order_{exc.classification}")
            logger.error(
                "Live order ended without a recorded trade for %s: "
                "classification=%s detail=%s; it was not retried",
                target_market.market_slug,
                exc.classification,
                exc,
            )
            return
        except Exception as exc:
            await reject(f"live_order_failed:{type(exc).__name__}")
            logger.exception("Live order failed for %s; it was not retried", target_market.market_slug)
            return
        log_message = (
            "LIVE ORDER FILLED"
            if execution.status == "filled"
            else "LIVE ORDER PARTIALLY FILLED"
        )
        logger.info(
            "%s order_id=%s market=%s outcome=%s requested_shares=%s "
            "filled_shares=%.4f avg_price=%.6f fees=%.6f total_cost=%.6f status=%s",
            log_message,
            execution.order_id,
            target_market.market_slug,
            target_outcome,
            self.settings.live_shares_per_trade,
            execution.fill.shares,
            execution.fill.average_price,
            execution.fill.fee,
            execution.fill.total_cost,
            execution.status,
        )
        await self.database.create_trade(
            signal, target_market, token_id, execution.fill, received_at,
            execution_mode="live", max_price=max_price,
            external_order_id=execution.order_id, external_status=execution.status,
            platform=target_market.platform, outcome=target_outcome,
        )
        await self.database.record_live_attempt(
            signal.signal_id, "traded", execution.status
        )
        logger.info(
            "Live trade %s %s: %.4f shares @ %.4f, cost $%.2f (ceiling %.3f)",
            target_outcome, target_market.market_slug, execution.fill.shares,
            execution.fill.average_price, execution.fill.total_cost, max_price,
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
                market_client = (
                    self.polymarket_us
                    if trade.get("platform") == "us"
                    else self.polymarket
                )
                market = await market_client.market_by_slug(trade["market_slug"])
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


def _malformed_message_summary(message: Any) -> dict[str, Any]:
    """Extract message-shape metadata without wallet or credential values."""
    if not isinstance(message, dict):
        return {"message_type": type(message).__name__}
    outer = message.get("data")
    outer = outer if isinstance(outer, dict) else {}
    nested = outer.get("data")
    nested = nested if isinstance(nested, dict) else {}
    return {
        "channel": message.get("channel"),
        "type": message.get("type"),
        "websocket_ts": message.get("ts"),
        "created_at": outer.get("created_at"),
        "market_slug": outer.get("market_slug") or nested.get("marketSlug"),
        "title": outer.get("title"),
        "event_id": outer.get("event_id"),
        "group_id": outer.get("group_id"),
        "outer_data_keys": sorted(str(key) for key in outer),
        "nested_data_keys": sorted(str(key) for key in nested),
    }


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


def live_price_ceiling(
    reference_price: float, settings: Settings, tick_size: str
) -> float:
    ceiling = min(reference_price + settings.max_price_drift, 0.99)
    if settings.trading_mode == "live":
        ceiling = min(ceiling, settings.live_max_entry_price)
    return _floor_to_tick(ceiling, tick_size)


def live_entry_price_reason(price: float, settings: Settings) -> str | None:
    if price < settings.live_min_entry_price - 1e-9:
        return "live_entry_price_below_minimum"
    if price > settings.live_max_entry_price + 1e-9:
        return "live_entry_price_above_maximum"
    return None


def live_filter_reason(market: MarketInfo, settings: Settings) -> str | None:
    """Apply optional filters only to live execution, never paper collection."""
    category = normalize_filter(market.category) or "unknown"
    market_type = normalize_filter(market.market_type) or "unknown"
    if (
        settings.live_category_filters
        and category not in settings.live_category_filters
    ):
        return f"live_filter_category:{category}"
    if (
        settings.live_market_type_filters
        and market_type not in settings.live_market_type_filters
    ):
        return f"live_filter_market_type:{market_type}"
    return None
