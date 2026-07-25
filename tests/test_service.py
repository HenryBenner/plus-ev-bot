from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fadebot.db import Database
from fadebot.models import FadeSignal, MarketInfo
from fadebot.service import eligibility_reason

from .test_models import sample_message


def make_market(event_time, category="sports", closed=False):
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
    )


@pytest.mark.asyncio
async def test_eligibility_window(tmp_path: Path):
    now = datetime(2026, 7, 23, 18, tzinfo=timezone.utc)
    signal = FadeSignal.from_message(sample_message(), now)
    assert eligibility_reason(
        signal,
        make_market(now + timedelta(hours=71)),
        now,
    ) is None
    assert (
        eligibility_reason(
            signal,
            make_market(now + timedelta(hours=73)),
            now,
        )
        == "event_outside_window"
    )
    assert (
        eligibility_reason(
            signal,
            make_market(now - timedelta(minutes=1)),
            now,
        )
        == "event_already_started"
    )


def test_non_sports_market_is_eligible():
    now = datetime(2026, 7, 23, 18, tzinfo=timezone.utc)
    signal = FadeSignal.from_message(sample_message(), now)
    market = make_market(now + timedelta(hours=24), category="politics")
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
