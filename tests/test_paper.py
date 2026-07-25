import pytest

from fadebot.paper import simulate_market_buy, taker_fee


def test_walks_book_and_includes_fee_in_budget():
    fill = simulate_market_buy(
        [(0.40, 10), (0.50, 100)],
        10,
        fees_enabled=True,
        fee_rate=0.03,
    )
    assert fill is not None
    assert fill.shares > 20
    assert fill.notional + fill.fee == pytest.approx(fill.total_cost)
    assert fill.total_cost == pytest.approx(10, abs=0.005)
    assert fill.average_price > 0.40
    assert fill.fee > 0
    assert fill.fully_filled


def test_partial_fill_is_reported():
    fill = simulate_market_buy(
        [(0.40, 2)],
        10,
        fees_enabled=False,
    )
    assert fill is not None
    assert fill.shares == pytest.approx(2)
    assert fill.total_cost == pytest.approx(0.8)
    assert not fill.fully_filled


def test_below_minimum_is_unfilled():
    fill = simulate_market_buy(
        [(0.40, 2)],
        10,
        fees_enabled=False,
        min_order_size=5,
    )
    assert fill is None


def test_price_ceiling_blocks_expensive_levels():
    fill = simulate_market_buy(
        [(0.40, 10), (0.51, 100)],
        10,
        fees_enabled=False,
        max_price=0.50,
    )
    assert fill is not None
    assert fill.shares == pytest.approx(10)
    assert fill.total_cost == pytest.approx(4)
    assert not fill.fully_filled


def test_price_ceiling_can_reject_entire_book():
    assert (
        simulate_market_buy(
            [(0.51, 100)],
            10,
            fees_enabled=False,
            max_price=0.50,
        )
        is None
    )


def test_fee_formula():
    assert taker_fee(100, 0.5, 0.03) == pytest.approx(0.75)
