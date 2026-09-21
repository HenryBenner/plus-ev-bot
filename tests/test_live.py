import pytest

from fadebot.config import Settings
from fadebot.live import PolymarketLiveExecutor, _fill_from_executions, _split_market_side


def settings():
    return Settings(
        prediction_hunt_api_key="test", trading_mode="live",
        live_trading_enabled=True,
        live_trading_ack="I_UNDERSTAND_REAL_MONEY_IS_AT_RISK",
        polymarket_us_key_id="key", polymarket_us_secret_key="secret",
    )


class Orders:
    def __init__(self, created=None, retrieved=None, error=None):
        self.created = created
        self.retrieved = list(retrieved or [])
        self.error = error
        self.create_calls = 0
        self.retrieve_calls = 0
        self.payload = None

    def create(self, payload):
        self.create_calls += 1
        self.payload = payload
        if self.error:
            raise self.error
        return self.created

    def retrieve(self, order_id):
        self.retrieve_calls += 1
        return self.retrieved.pop(0)


class Client:
    def __init__(self, orders):
        self.orders = orders


def response(shares, state, *, price="0.48", execution_type=None):
    execution_type = execution_type or (
        "EXECUTION_TYPE_FILL" if shares else "EXECUTION_TYPE_CANCELED"
    )
    return {
        "id": "order-123",
        "executions": [{
            "type": execution_type,
            "lastShares": str(shares),
            "lastPx": {"value": price},
            "commissionNotionalCollected": {"value": "0.05"},
            "order": {
                "id": "order-123", "state": state,
                "cumQuantity": shares, "leavesQuantity": 10 - shares,
                "avgPx": {"value": price},
                "commissionNotionalTotalCollected": {"value": "0.05"},
            },
        }],
    }


def executor(orders):
    value = PolymarketLiveExecutor(settings())
    value._client = Client(orders)
    value._sleep = lambda _: None
    return value


def test_market_side_identifier():
    assert _split_market_side("some-market::NO") == ("some-market", "NO")
    with pytest.raises(ValueError):
        _split_market_side("some-market")


def test_execution_aggregation_tracks_partial_fill():
    fill = _fill_from_executions([
        {"type": "EXECUTION_TYPE_FILL", "lastShares": "10", "lastPx": {"value": "0.48"},
         "commissionNotionalCollected": {"value": "0.05"}},
        {"type": "EXECUTION_TYPE_PARTIAL_FILL", "lastShares": "5", "lastPx": {"value": "0.50"},
         "commissionNotionalCollected": {"value": "0.03"}},
    ], 20)
    assert fill.shares == pytest.approx(15)
    assert fill.notional == pytest.approx(7.3)
    assert fill.fee == pytest.approx(0.08)
    assert not fill.fully_filled


def test_yes_fill_returns_structured_attempt():
    orders = Orders(response(10, "ORDER_STATE_FILLED"))
    result = executor(orders)._buy_sync("us-market::YES", 0.50, 10)
    assert result.classification == "filled"
    assert result.filled_shares == pytest.approx(10)
    assert result.average_price == pytest.approx(0.48)
    assert result.notional == pytest.approx(4.8)
    assert result.fee == pytest.approx(0.05)
    assert orders.payload["intent"] == "ORDER_INTENT_BUY_LONG"
    assert orders.payload["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"


def test_no_order_and_fill_use_selected_outcome_economic_price():
    orders = Orders(response(10, "ORDER_STATE_FILLED", price="0.60"))
    result = executor(orders)._buy_sync("us-market::NO", 0.40, 10)
    assert orders.payload["intent"] == "ORDER_INTENT_BUY_SHORT"
    assert float(orders.payload["price"]["value"]) == pytest.approx(0.60)
    assert result.average_price == pytest.approx(0.40)
    assert result.notional == pytest.approx(4.0)


@pytest.mark.parametrize("state", ["ORDER_STATE_CANCELED", "ORDER_STATE_EXPIRED"])
def test_terminal_zero_fill_is_confirmed_safe_to_retry(state):
    result = executor(Orders(response(0, state)))._buy_sync(
        "us-market::YES", 0.50, 10
    )
    assert result.classification == "confirmed_zero_fill"
    assert result.filled_shares == 0


def test_rejected_order_is_terminal():
    result = executor(Orders(response(
        0, "ORDER_STATE_REJECTED", execution_type="EXECUTION_TYPE_REJECTED"
    )))._buy_sync("us-market::YES", 0.50, 10)
    assert result.classification == "rejected"


def test_create_timeout_is_submission_unknown_and_never_retrieved():
    orders = Orders(error=TimeoutError("request timed out"))
    result = executor(orders)._buy_sync("us-market::YES", 0.50, 10)
    assert result.classification == "submission_unknown"
    assert orders.create_calls == 1
    assert orders.retrieve_calls == 0


def test_unclear_ioc_is_retrieved_until_final():
    pending = response(0, "ORDER_STATE_NEW", execution_type="EXECUTION_TYPE_NEW")
    order = response(10, "ORDER_STATE_FILLED")["executions"][0]["order"]
    orders = Orders(pending, [{"order": order}])
    result = executor(orders)._buy_sync("us-market::YES", 0.50, 10)
    assert result.classification == "filled"
    assert result.filled_shares == pytest.approx(10)
    assert orders.create_calls == 1
    assert orders.retrieve_calls == 1


def test_retrieved_partial_uses_aggregate_order_fields():
    pending = {"id": "order-123", "state": "ORDER_STATE_NEW"}
    order = response(4, "ORDER_STATE_PARTIALLY_FILLED")["executions"][0]["order"]
    result = executor(Orders(pending, [{"order": order}]))._buy_sync(
        "us-market::YES", 0.50, 10
    )
    assert result.classification == "partial_fill"
    assert result.filled_shares == pytest.approx(4)
    assert result.average_price == pytest.approx(0.48)


def test_unclear_ioc_retrieved_as_canceled_zero_is_safe_to_retry():
    pending = {"id": "order-123", "state": "ORDER_STATE_NEW"}
    order = response(0, "ORDER_STATE_CANCELED")["executions"][0]["order"]
    result = executor(Orders(pending, [{"order": order}]))._buy_sync(
        "us-market::YES", 0.50, 10
    )
    assert result.classification == "confirmed_zero_fill"
    assert result.order_id == "order-123"
