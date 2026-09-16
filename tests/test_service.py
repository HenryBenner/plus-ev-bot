import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fadebot.db import Database
from fadebot.models import FadeSignal, MarketInfo
from fadebot.config import Settings
from fadebot.service import (
    eligibility_reason,
    live_entry_price_reason,
    live_filter_reason,
    live_price_ceiling,
)

from .test_models import sample_message


def make_market(
    event_time,
    category="sports",
    closed=False,
    expiration_time=None,
):
    return MarketInfo(
        market_id="1",
        condition_id="condition",
        market_slug="lakers-celtics",
        event_slug="lakers-celtics",
        title="Lakers vs Celtics",
        event_time=event_time,
        category=category,
        outcomes=["Yes", "No"],
        token_ids=["yes-token", "no-token"],
        outcome_prices=[0.47, 0.53],
        closed=closed,
        fees_enabled=True,
        expiration_time=expiration_time,
    )


@pytest.mark.asyncio
async def test_eligibility_window(tmp_path: Path):
    now = datetime(2026, 7, 23, 18, tzinfo=timezone.utc)
    signal = FadeSignal.from_message(sample_message(), now)
    assert eligibility_reason(
        signal,
        make_market(
            now + timedelta(hours=1),
            expiration_time=now + timedelta(hours=71),
        ),
        now,
    ) is None
    assert (
        eligibility_reason(
            signal,
            make_market(
                now + timedelta(hours=1),
                expiration_time=now + timedelta(hours=73),
            ),
            now,
        )
        == "event_outside_window"
    )
    assert eligibility_reason(
        signal,
        make_market(
            now - timedelta(hours=1),
            expiration_time=now + timedelta(hours=4),
        ),
        now,
    ) is None
    assert eligibility_reason(
        signal,
        make_market(
            now - timedelta(days=1),
            expiration_time=now - timedelta(minutes=1),
        ),
        now,
    ) == "market_already_expired"


def test_non_sports_market_is_eligible():
    now = datetime(2026, 7, 23, 18, tzinfo=timezone.utc)
    signal = FadeSignal.from_message(sample_message(), now)
    market = make_market(now + timedelta(hours=24), category="politics")
    assert eligibility_reason(signal, market, now) is None


def test_live_filters_are_optional_and_mode_independent():
    market = make_market(None, category="sports")
    market = MarketInfo(**{**market.__dict__, "market_type": "team_winner"})
    allow = Settings(
        prediction_hunt_api_key="test",
        live_category_filters=("sports",),
        live_market_type_filters=("team_winner",),
    )
    assert live_filter_reason(market, allow) is None
    reject = Settings(
        prediction_hunt_api_key="test",
        live_category_filters=("crypto",),
    )
    assert live_filter_reason(market, reject) == "live_filter_category:sports"


def test_live_entry_price_band_and_existing_drift_ceiling():
    settings = Settings(
        prediction_hunt_api_key="test",
        trading_mode="live",
        live_min_entry_price=0.30,
        live_max_entry_price=0.90,
    )
    assert live_entry_price_reason(0.299, settings) == "live_entry_price_below_minimum"
    assert live_entry_price_reason(0.30, settings) is None
    assert live_entry_price_reason(0.90, settings) is None
    assert live_entry_price_reason(0.901, settings) == "live_entry_price_above_maximum"
    assert live_price_ceiling(0.85, settings, "0.01") == pytest.approx(0.90)
    assert live_price_ceiling(0.50, settings, "0.01") == pytest.approx(0.60)
    paper = Settings(prediction_hunt_api_key="test", trading_mode="paper")
    assert live_price_ceiling(0.85, paper, "0.01") == pytest.approx(0.95)


def test_date_only_resolution_is_valid_through_end_of_day():
    now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    message = sample_message()
    message["data"]["data"]["resolution_date"] = "2026-07-24"
    signal = FadeSignal.from_message(message, now)
    market = make_market(now - timedelta(hours=2), expiration_time=None)
    assert eligibility_reason(signal, market, now) is None


@pytest.mark.asyncio
async def test_database_settlement_metrics(tmp_path: Path):
    path = tmp_path / "test.db"
    database = Database(path)
    await database.initialize()
    now = datetime(2026, 7, 23, 18, tzinfo=timezone.utc)
    signal = FadeSignal.from_message(sample_message(), now)
    assert await database.add_signal(signal)

    from fadebot.models import PaperFill

    fill = PaperFill(
        shares=20,
        notional=9.8,
        fee=0.2,
        total_cost=10,
        average_price=0.49,
        fully_filled=True,
    )
    await database.create_trade(
        signal,
        make_market(now + timedelta(days=1)),
        "yes-token",
        fill,
        now,
    )
    trade = (await database.open_trades())[0]
    await database.settle_trade(
        trade["id"],
        final_price=1,
        resolved_outcome="Yes",
        settled_at=now + timedelta(days=1),
    )
    summary = await database.summary()
    assert summary["settled_trades"] == 1
    assert summary["wins"] == 1
    assert summary["pnl"] == pytest.approx(10)
    assert summary["portfolio_roi"] == pytest.approx(1)


@pytest.mark.asyncio
async def test_rejected_started_signals_can_be_loaded_for_backfill(tmp_path: Path):
    database = Database(tmp_path / "backfill.db")
    await database.initialize()
    now = datetime(2026, 7, 23, 18, tzinfo=timezone.utc)
    signal = FadeSignal.from_message(sample_message(), now)
    assert await database.add_signal(signal)
    await database.reject_signal(signal.signal_id, "event_already_started")

    messages = await database.rejected_signal_messages("event_already_started")
    assert messages == [sample_message()]


@pytest.mark.asyncio
async def test_live_side_lock_blocks_opposite_but_not_same_side(tmp_path: Path):
    database = Database(tmp_path / "side_lock.db")
    await database.initialize()
    assert await database.claim_live_market_side("us-market", "YES", "signal-1")
    assert await database.claim_live_market_side("us-market", "YES", "signal-2")
    assert not await database.claim_live_market_side("us-market", "NO", "signal-3")
    assert await database.claim_live_market_side("other-market", "NO", "signal-4")
    reopened = Database(tmp_path / "side_lock.db")
    await reopened.initialize()
    assert not await reopened.claim_live_market_side("us-market", "NO", "signal-5")


@pytest.mark.asyncio
async def test_opposite_live_side_claims_cannot_both_succeed(tmp_path: Path):
    database = Database(tmp_path / "race.db")
    await database.initialize()
    claims = await asyncio.gather(
        database.claim_live_market_side("us-market", "YES", "one"),
        database.claim_live_market_side("us-market", "NO", "two"),
    )
    assert sorted(claims) == [False, True]


@pytest.mark.asyncio
async def test_live_side_lock_respects_older_live_trades_but_not_paper(tmp_path: Path):
    database = Database(tmp_path / "historic_live.db")
    await database.initialize()
    now = datetime(2026, 7, 23, 18, tzinfo=timezone.utc)
    signal = FadeSignal.from_message(sample_message(), now)
    await database.add_signal(signal)
    from fadebot.models import PaperFill

    fill = PaperFill(10, 5, 0, 5, 0.5, True)
    market = MarketInfo(
        **{
            **make_market(now + timedelta(days=1)).__dict__,
            "market_slug": "us-historic",
            "platform": "us",
        }
    )
    await database.create_trade(
        signal,
        market,
        "us-historic::YES",
        fill,
        now,
        execution_mode="live",
        outcome="YES",
        platform="us",
    )
    assert not await database.claim_live_market_side("us-historic", "NO", "new")
    assert await database.claim_live_market_side("us-historic", "YES", "new")
    paper_message = sample_message()
    paper_message["data"]["created_at"] = "2026-07-23T19:00:00Z"
    paper_signal = FadeSignal.from_message(paper_message, now)
    await database.add_signal(paper_signal)
    paper_market = MarketInfo(
        **{**market.__dict__, "market_slug": "paper-only", "platform": "international"}
    )
    await database.create_trade(
        paper_signal,
        paper_market,
        "paper-token",
        fill,
        now,
        execution_mode="paper",
        outcome="YES",
    )
    assert await database.claim_live_market_side("paper-only", "NO", "new")
