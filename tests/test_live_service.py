from datetime import datetime, timedelta, timezone

import pytest

from fadebot.config import Settings
from fadebot.db import Database
from fadebot.live import LiveExecution, LiveOrderError
from fadebot.mapping import InternationalToUSMapper, USMarketMapping
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

    async def orderbook(self, market_side: str) -> dict:
        return {
            "asks": [{"price": 0.50, "size": 100}],
            "tick_size": 0.01,
            "min_order_size": 1,
        }


class USClient:
    def __init__(self, price: float):
        self.price = price

    async def orderbook(self, market_side: str) -> dict:
        return {
            "asks": [{"price": self.price, "size": 20}],
            "tick_size": 0.01,
            "min_order_size": 1,
        }


class MappingUSClient(USClient):
    def __init__(self, price: float, target: MarketInfo):
        super().__init__(price)
        self.target = target

    async def sports_markets_near(self, event_time: datetime) -> list[MarketInfo]:
        return []

    async def search_markets(self, query: str) -> list[MarketInfo]:
        return []

    async def sports_markets_wide(self, event_time: datetime) -> list[MarketInfo]:
        return [self.target]

    async def market_by_slug(self, slug: str) -> MarketInfo:
        assert slug == self.target.market_slug
        return self.target


class Mapper:
    def __init__(self, market: MarketInfo):
        self.market = market

    async def map_market(self, source: MarketInfo) -> USMarketMapping:
        return USMarketMapping(self.market, "YES", "test")


class Executor:
    def __init__(self, price: float, filled_shares: int | None = None):
        self.price = price
        self.filled_shares = filled_shares
        self.orders: list[str] = []

    async def buy(self, *, token_id: str, **kwargs) -> LiveExecution:
        self.orders.append(token_id)
        requested = kwargs["requested_shares"]
        shares = self.filled_shares if self.filled_shares is not None else requested
        return LiveExecution(
            order_id=f"order-{len(self.orders)}",
            status="filled" if shares == requested else "partially_filled",
            fill=PaperFill(
                shares, shares * self.price, 0, shares * self.price,
                self.price, shares == requested,
            ),
            raw={},
        )


class FailingExecutor:
    def __init__(self, classification: str):
        self.classification = classification
        self.calls = 0

    async def buy(self, **kwargs):
        self.calls += 1
        raise LiveOrderError(self.classification, f"test {self.classification}")


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
        assert summary["total_trades"] == 3
        assert (await database.summary("paper"))["total_trades"] == 2
        assert (await database.summary("live"))["total_trades"] == 1
        assert any(
            row["reason"] == "live_opposite_side_already_attempted"
            for row in (await database.summary("live"))["rejections"]
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
        assert (await database.summary("paper"))["total_trades"] == 1
        assert (await database.summary("live"))["total_trades"] == 0
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_live_mode_paper_tracks_non_sports_without_live_order(tmp_path):
    now = datetime(2026, 7, 23, 18, tzinfo=timezone.utc)
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
    market = make_market("intl-politics", now, "international")
    service.polymarket = SourceClient(MarketInfo(**{**market.__dict__, "category": "politics"}))
    executor = Executor(0.50)
    service.live_executor = executor
    try:
        await service.process_message(make_message("YES", "2026-07-23T18:00:00Z"), now)
        assert executor.orders == []
        assert (await database.summary("paper"))["total_trades"] == 1
        assert (await database.summary("live"))["total_trades"] == 0
        assert (await database.summary("live"))["rejections"][0]["reason"] == "live_filter_category:politics"
    finally:
        await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filled_shares", "expected_status"),
    [(10, "filled"), (4, "partially_filled")],
)
async def test_live_fill_creates_trade_for_exact_executed_quantity(
    tmp_path, filled_shares, expected_status, caplog
):
    now = datetime(2026, 7, 23, 18, tzinfo=timezone.utc)
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
    service.polymarket = SourceClient(make_market("intl", now, "international"))
    service.polymarket_us = USClient(0.50)
    service.market_mapper = Mapper(make_market("us-market", now, "us"))
    service.live_executor = Executor(0.48, filled_shares)
    try:
        with caplog.at_level("INFO"):
            await service.process_message(
                make_message("YES", "2026-07-23T18:00:00Z"), now
            )
        live_trades = await database.recent_trades(mode="live")
        assert len(live_trades) == 1
        assert live_trades[0]["shares"] == pytest.approx(filled_shares)
        assert live_trades[0]["external_status"] == expected_status
        assert "LIVE ORDER ATTEMPT" in caplog.text
        assert (
            "LIVE ORDER FILLED" if expected_status == "filled"
            else "LIVE ORDER PARTIALLY FILLED"
        ) in caplog.text
    finally:
        await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "classification",
    ["canceled_no_fill", "ioc_no_fill", "rejected", "submission_error"],
)
async def test_classified_live_failure_creates_no_trade_and_is_not_retried(
    tmp_path, classification
):
    now = datetime(2026, 7, 23, 18, tzinfo=timezone.utc)
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
    service.polymarket = SourceClient(make_market("intl", now, "international"))
    service.polymarket_us = USClient(0.50)
    service.market_mapper = Mapper(make_market("us-market", now, "us"))
    executor = FailingExecutor(classification)
    service.live_executor = executor
    try:
        await service.process_message(
            make_message("YES", "2026-07-23T18:00:00Z"), now
        )
        assert await database.recent_trades(mode="live") == []
        assert executor.calls == 1
        reasons = (await database.summary("live"))["rejections"]
        assert reasons == [{"reason": f"live_order_{classification}", "count": 1}]
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_missing_created_at_logs_sanitized_shape_and_does_not_crash(
    tmp_path, caplog
):
    database = Database(tmp_path / "malformed.db")
    await database.initialize()
    settings = Settings(prediction_hunt_api_key="test", database_path=database.path)
    service = TradingService(settings, database)
    message = make_message("YES", "2026-07-23T18:00:00Z")
    message["data"]["created_at"] = None
    try:
        with caplog.at_level("WARNING"):
            await service.process_message(message)
        assert (await database.summary())["total_signals"] == 0
        assert "signal is missing a valid created_at" in caplog.text
        assert "outer_data_keys" in caplog.text
        assert "nested_data_keys" in caplog.text
        assert "0xwinner" not in caplog.text
    finally:
        await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_team", "target_team", "aliases"),
    [
        ("SV Darmstadt 98", "Darmstadt 98", ()),
        ("Psim Yogyakarta", "Perserikatan Sepakbola Indonesia Mataram", ("PSIM Yogyakarta",)),
        ("Olympique de Marseille", "Marseille", ("OM",)),
        ("TSG 1899 Hoffenheim", "Hoffenheim", ("TSG Hoffenheim",)),
    ],
)
async def test_historical_mapping_misses_reach_live_execution(
    tmp_path, source_team, target_team, aliases
):
    now = datetime(2026, 9, 20, 17, tzinfo=timezone.utc)
    database = Database(tmp_path / "live.db")
    await database.initialize()
    settings = Settings(
        prediction_hunt_api_key="test", trading_mode="live",
        live_trading_enabled=True,
        live_trading_ack="I_UNDERSTAND_REAL_MONEY_IS_AT_RISK",
        polymarket_us_key_id="test", polymarket_us_secret_key="test",
        database_path=database.path,
    )
    source = make_market("intl-team", now, "international")
    source = MarketInfo(**{
        **source.__dict__,
        "title": f"Will {source_team} win?",
        "event_slug": f"{source_team}-vs-opponent",
    })
    target = make_market("us-team", now, "us")
    target = MarketInfo(**{
        **target.__dict__,
        "title": f"{target_team} vs Opponent",
        "event_slug": f"{target_team}-vs-opponent",
        "event_time": now + timedelta(hours=4),
        "long_label": target_team,
        "short_label": "Opponent",
        "long_aliases": aliases,
    })
    service = TradingService(settings, database)
    service.polymarket = SourceClient(source)
    us_client = MappingUSClient(0.50, target)
    service.polymarket_us = us_client
    service.market_mapper = InternationalToUSMapper(
        us_client, database  # type: ignore[arg-type]
    )
    executor = Executor(0.50)
    service.live_executor = executor
    try:
        await service.process_message(
            make_message("YES", "2026-09-20T17:00:00Z"), now
        )
        assert executor.orders == ["us-team::YES"]
        trades = await database.recent_trades(mode="live")
        assert len(trades) == 1
        assert trades[0]["market_slug"] == "us-team"
    finally:
        await service.stop()
