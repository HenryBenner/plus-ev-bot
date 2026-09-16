from datetime import datetime, timedelta, timezone

import pytest

from fadebot.config import Settings
from fadebot.db import Database
from fadebot.live import LiveExecution
from fadebot.mapping import USMarketMapping
from fadebot.models import MarketInfo, PaperFill
from fadebot.service import TradingService

from .test_models import sample_message


def make_message(outcome: str, created_at: str) -> dict:
    message = sample_message()
    message["data"]["created_at"] = created_at
    message["data"]["data"]["profitable_wallet"]["outcome"] = outcome
    message["data"]["data"]["profitable_wallet"]["price"] = (
        0.50 if outcome == "YES" else 0.50
    )
    return message


def make_market(slug: str, now: datetime, platform: str) -> MarketInfo:
    return MarketInfo(
        market_id=slug,
        condition_id=slug,
        market_slug=slug,
        event_slug=slug,
        title="Will Lakers win?",
        event_time=now + timedelta(hours=2),
        expiration_time=now + timedelta(hours=5),
        category="sports",
        outcomes=["YES", "NO"],
        token_ids=[f"{slug}::YES", f"{slug}::NO"],
        outcome_prices=[0.5, 0.5],
        closed=False,
        fees_enabled=False,
        platform=platform,
        market_type="team_winner",
    )


class SourceClient:
    def __init__(self, market: MarketInfo):
        self.market = market

    async def resolve_market(self, slug: str, title: str) -> MarketInfo:
        return self.market


class USClient:
    def __init__(self, price: float):
        self.price = price

    async def orderbook(self, market_side: str) -> dict:
        return {
            "asks": [{"price": self.price, "size": 20}],
            "tick_size": 0.01,
            "min_order_size": 1,
        }


class Mapper:
    def __init__(self, market: MarketInfo):
        self.market = market

    async def map_market(self, source: MarketInfo) -> USMarketMapping:
        return USMarketMapping(self.market, "YES", "test")


class Executor:
    def __init__(self, price: float):
        self.price = price
        self.orders: list[str] = []

    async def buy(self, *, token_id: str, **kwargs) -> LiveExecution:
        self.orders.append(token_id)
        shares = kwargs["requested_shares"]
        return LiveExecution(
            order_id=f"order-{len(self.orders)}",
            status="filled",
            fill=PaperFill(shares, shares * self.price, 0, shares * self.price, self.price, True),
            raw={},
        )


@pytest.mark.asyncio
async def test_live_service_rejects_reversal_after_first_us_order(tmp_path):
    now = datetime(2026, 7, 23, 18, tzinfo=timezone.utc)
    database = Database(tmp_path / "live.db")
    await database.initialize()
    settings = Settings(
        prediction_hunt_api_key="test",
        trading_mode="live",
        live_trading_enabled=True,
        live_trading_ack="I_UNDERSTAND_REAL_MONEY_IS_AT_RISK",
        polymarket_us_key_id="test",
        polymarket_us_secret_key="test",
        database_path=database.path,
    )
    service = TradingService(settings, database)
    service.polymarket = SourceClient(make_market("intl", now, "international"))
    service.polymarket_us = USClient(0.50)
    service.market_mapper = Mapper(make_market("us-market", now, "us"))
    executor = Executor(0.50)
    service.live_executor = executor
    try:
        await service.process_message(make_message("YES", "2026-07-23T18:00:00Z"), now)
        await service.process_message(make_message("NO", "2026-07-23T18:01:00Z"), now)
        assert executor.orders == ["us-market::YES"]
        summary = await database.summary()
        assert summary["total_trades"] == 1
        assert any(
            row["reason"] == "live_opposite_side_already_attempted"
            for row in summary["rejections"]
        )
    finally:
        await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("price", [0.29, 0.91])
async def test_live_service_rejects_price_outside_band(tmp_path, price):
    now = datetime(2026, 7, 23, 18, tzinfo=timezone.utc)
    database = Database(tmp_path / "live.db")
    await database.initialize()
    settings = Settings(
        prediction_hunt_api_key="test",
        trading_mode="live",
        live_trading_enabled=True,
        live_trading_ack="I_UNDERSTAND_REAL_MONEY_IS_AT_RISK",
        polymarket_us_key_id="test",
        polymarket_us_secret_key="test",
        database_path=database.path,
    )
    service = TradingService(settings, database)
    service.polymarket = SourceClient(make_market("intl", now, "international"))
    service.polymarket_us = USClient(price)
    service.market_mapper = Mapper(make_market("us-market", now, "us"))
    executor = Executor(price)
    service.live_executor = executor
    try:
        await service.process_message(make_message("YES", "2026-07-23T18:00:00Z"), now)
        assert executor.orders == []
        assert (await database.summary())["total_trades"] == 0
    finally:
        await service.stop()
