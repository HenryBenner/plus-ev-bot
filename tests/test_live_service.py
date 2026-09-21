from datetime import datetime, timedelta, timezone

import pytest

from fadebot.config import Settings
from fadebot.db import Database
from fadebot.live import LiveAttemptResult
from fadebot.mapping import USMarketMapping
from fadebot.models import MarketInfo
from fadebot.service import TradingService

from .test_models import sample_message


NOW = datetime(2026, 7, 23, 18, tzinfo=timezone.utc)


def make_message(outcome="YES", created_at="2026-07-23T18:00:00Z"):
    message = sample_message()
    message["data"]["created_at"] = created_at
    message["data"]["data"]["profitable_wallet"]["outcome"] = outcome
    message["data"]["data"]["profitable_wallet"]["price"] = 0.50
    return message


def make_market(slug, platform):
    return MarketInfo(
        market_id=slug, condition_id=slug, market_slug=slug, event_slug=slug,
        title="Will Lakers win?", event_time=NOW + timedelta(hours=2),
        expiration_time=NOW + timedelta(hours=5), category="sports",
        outcomes=["YES", "NO"], token_ids=[f"{slug}::YES", f"{slug}::NO"],
        outcome_prices=[0.5, 0.5], closed=False, fees_enabled=False,
        platform=platform, market_type="team_winner",
    )


class SourceClient:
    async def resolve_market(self, slug, title):
        return make_market("intl", "international")

    async def orderbook(self, market_side):
        return {"asks": [{"price": 0.5, "size": 100}], "tick_size": 0.01,
                "min_order_size": 1}


class Mapper:
    async def map_market(self, source):
        return USMarketMapping(make_market("us-market", "us"), "YES", "test")


class USClient:
    def __init__(self, prices=(0.50,), settlements=None):
        self.prices = list(prices)
        self.last_price = self.prices[-1]
        self.settlements = settlements or {}
        self.book_calls = 0

    async def orderbook(self, market_side):
        self.book_calls += 1
        if self.prices:
            self.last_price = self.prices.pop(0)
        return {"asks": [{"price": self.last_price, "size": 20}],
                "tick_size": 0.01, "min_order_size": 1}

    async def official_settlement(self, slug):
        value = self.settlements.get(slug)
        if isinstance(value, Exception):
            raise value
        return value


def attempt(classification, shares=0, price=0.50, order_id="order"):
    return LiveAttemptResult(
        order_id, "ORDER_STATE_TEST", shares, price if shares else None, 0,
        classification, {},
    )


class Executor:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def buy(self, **kwargs):
        self.calls.append(kwargs)
        return self.results.pop(0)


async def configured_service(tmp_path, results, *, prices=(0.50,)):
    database = Database(tmp_path / "live.db")
    await database.initialize()
    settings = Settings(
        prediction_hunt_api_key="test", trading_mode="live",
        live_trading_enabled=True,
        live_trading_ack="I_UNDERSTAND_REAL_MONEY_IS_AT_RISK",
        polymarket_us_key_id="test", polymarket_us_secret_key="test",
        database_path=database.path,
    )
    service = TradingService(settings, database)
    service.polymarket = SourceClient()
    service.polymarket_us = USClient(prices)
    service.market_mapper = Mapper()
    service.live_executor = Executor(results)

    async def no_sleep(_):
        return None

    service._sleep = no_sleep
    return service, database


@pytest.mark.asyncio
@pytest.mark.parametrize("zero_count", [1, 2])
async def test_confirmed_zero_fill_retries_then_fills(tmp_path, zero_count):
    results = [attempt("confirmed_zero_fill", order_id=f"zero-{i}")
               for i in range(zero_count)]
    results.append(attempt("filled", 10, 0.49, "filled"))
    service, database = await configured_service(tmp_path, results)
    try:
        await service.process_message(make_message(), NOW)
        executor = service.live_executor
        assert len(executor.calls) == zero_count + 1
        assert [call["requested_shares"] for call in executor.calls] == [10] * (zero_count + 1)
        trades = await database.recent_trades(mode="live")
        assert len(trades) == 1
        assert trades[0]["shares"] == pytest.approx(10)
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_refreshed_price_above_original_ceiling_stops_chase(tmp_path):
    service, database = await configured_service(
        tmp_path, [attempt("confirmed_zero_fill")], prices=(0.50, 0.61)
    )
    try:
        await service.process_message(make_message(), NOW)
        assert len(service.live_executor.calls) == 1
        assert await database.recent_trades(mode="live") == []
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_partial_fills_are_combined_into_one_trade(tmp_path):
    service, database = await configured_service(tmp_path, [
        attempt("partial_fill", 4, 0.48, "one"),
        attempt("filled", 6, 0.50, "two"),
    ])
    try:
        await service.process_message(make_message(), NOW)
        calls = service.live_executor.calls
        assert [call["requested_shares"] for call in calls] == [10, 6]
        trades = await database.recent_trades(mode="live")
        assert len(trades) == 1
        assert trades[0]["shares"] == pytest.approx(10)
        assert trades[0]["fill_avg_price"] == pytest.approx(0.492)
        assert trades[0]["external_order_id"] == "one,two"
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_partial_fill_is_saved_when_chase_times_out(tmp_path):
    service, database = await configured_service(
        tmp_path, [attempt("partial_fill", 4, 0.48, "one")]
    )
    clock = iter([0.0, 0.0, 16.0])
    service._monotonic = lambda: next(clock)
    try:
        await service.process_message(make_message(), NOW)
        trades = await database.recent_trades(mode="live")
        assert len(trades) == 1
        assert trades[0]["shares"] == pytest.approx(4)
        assert trades[0]["external_status"] == "partially_filled"
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_ten_confirmed_zero_fills_create_no_trade(tmp_path):
    service, database = await configured_service(
        tmp_path, [attempt("confirmed_zero_fill", order_id=str(i)) for i in range(10)]
    )
    try:
        await service.process_message(make_message(), NOW)
        assert len(service.live_executor.calls) == 10
        assert await database.recent_trades(mode="live") == []
        assert (await database.summary("live"))["rejections"] == [
            {"reason": "live_order_attempt_limit", "count": 1}
        ]
    finally:
        await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("classification", ["rejected", "submission_unknown"])
async def test_unsafe_result_never_retries(tmp_path, classification):
    service, database = await configured_service(
        tmp_path, [attempt(classification)]
    )
    try:
        await service.process_message(make_message(), NOW)
        assert len(service.live_executor.calls) == 1
        assert await database.recent_trades(mode="live") == []
        assert (await database.summary("live"))["rejections"][0]["reason"] == f"live_order_{classification}"
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_repeated_missing_created_at_message_is_deduplicated(tmp_path):
    service, database = await configured_service(tmp_path, [attempt("filled", 10)])
    message = make_message(created_at=None)
    try:
        await service.process_message(message, NOW)
        await service.process_message(message, NOW + timedelta(seconds=5))
        assert (await database.summary("paper"))["total_trades"] == 1
        assert (await database.summary("live"))["total_trades"] == 1
        assert len(service.live_executor.calls) == 1
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_reversal_is_rejected_after_first_live_order(tmp_path):
    service, database = await configured_service(tmp_path, [attempt("filled", 10)])
    try:
        await service.process_message(make_message("YES"), NOW)
        await service.process_message(make_message("NO", "2026-07-23T18:01:00Z"), NOW)
        assert len(service.live_executor.calls) == 1
        reasons = (await database.summary("live"))["rejections"]
        assert any(row["reason"] == "live_opposite_side_already_attempted" for row in reasons)
    finally:
        await service.stop()


async def create_live_trade(database, outcome="YES"):
    signal = __import__("fadebot.models", fromlist=["FadeSignal"]).FadeSignal.from_message(make_message(outcome))
    await database.add_signal(signal)
    from fadebot.models import PaperFill
    await database.create_trade(
        signal, make_market("us-market", "us"), f"us-market::{outcome}",
        PaperFill(10, 5, 0, 5, 0.5, True), NOW,
        execution_mode="live", max_price=0.6, platform="us", outcome=outcome,
    )


@pytest.mark.asyncio
async def test_us_settlement_uses_only_official_endpoint(tmp_path):
    database = Database(tmp_path / "settle.db")
    await database.initialize()
    await create_live_trade(database, "NO")
    service = TradingService(Settings(prediction_hunt_api_key="test", database_path=database.path), database)
    service.polymarket_us = USClient(settlements={"us-market": 1.0})
    try:
        await service.settle_open_trades()
        trade = (await database.recent_trades(mode="live"))[0]
        assert trade["status"] == "settled"
        assert trade["final_price"] == pytest.approx(0)
        assert trade["resolved_outcome"] == "YES"
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_unavailable_us_settlement_leaves_open(tmp_path):
    database = Database(tmp_path / "settle.db")
    await database.initialize()
    await create_live_trade(database)
    service = TradingService(Settings(prediction_hunt_api_key="test", database_path=database.path), database)
    service.polymarket_us = USClient(settlements={})
    try:
        await service.settle_open_trades()
        assert (await database.recent_trades(mode="live"))[0]["status"] == "open"
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_reconciliation_repairs_and_reopens_historical_settlements(tmp_path):
    database = Database(tmp_path / "repair.db")
    await database.initialize()
    await create_live_trade(database)
    trade = (await database.recent_trades(mode="live"))[0]
    await database.settle_trade(trade["id"], final_price=0, resolved_outcome="NO", settled_at=NOW)
    service = TradingService(Settings(prediction_hunt_api_key="test", database_path=database.path), database)
    service.polymarket_us = USClient(settlements={"us-market": 1.0})
    try:
        assert await service.reconcile_live_settlements() == {"settled": 1, "reopened": 0, "errors": 0}
        fixed = (await database.recent_trades(mode="live"))[0]
        assert fixed["final_price"] == 1
        assert fixed["payout"] == 10
        service.polymarket_us = USClient(settlements={})
        assert (await service.reconcile_live_settlements())["reopened"] == 1
        reopened = (await database.recent_trades(mode="live"))[0]
        assert reopened["status"] == "open"
        assert reopened["pnl"] is None
        assert reopened["settled_at"] is None
    finally:
        await service.stop()
