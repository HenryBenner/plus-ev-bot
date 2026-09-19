import pytest

from fadebot.config import Settings
from fadebot.live import (
    LiveOrderError,
    PolymarketLiveExecutor,
    _fill_from_executions,
    _split_market_side,
)
from fadebot.models import PaperFill


def test_market_side_identifier():
    assert _split_market_side("some-market::NO") == ("some-market", "NO")
    with pytest.raises(ValueError):
        _split_market_side("some-market")


def test_us_execution_response_becomes_fill():
    fill = _fill_from_executions(
        [
            {
                "tradeId": "trade-1",
                "lastShares": "10",
                "lastPx": {"value": "0.48"},
                "commissionNotionalCollected": {"value": "0.05"},
            },
            {
                "tradeId": "trade-2",
                "lastShares": "5",
                "lastPx": {"value": "0.50"},
                "commissionNotionalCollected": {"value": "0.03"},
            },
        ],
        20,
    )
    assert fill is not None
    assert fill.shares == pytest.approx(15)
    assert fill.notional == pytest.approx(7.3)
    assert fill.fee == pytest.approx(0.08)
    assert not fill.fully_filled


def test_live_order_uses_configured_fixed_shares_and_ioc():
    class Orders:
        payload = None

        def create(self, payload):
            self.payload = payload
            return {
                "id": "order-1",
                "executions": [
                    {
                        "tradeId": "trade-1",
                        "lastShares": "10",
                        "lastPx": {"value": "0.45"},
                    }
                ],
            }

    class Client:
        orders = Orders()

    settings = Settings(
        prediction_hunt_api_key="test",
        trading_mode="live",
        live_trading_enabled=True,
        live_trading_ack="I_UNDERSTAND_REAL_MONEY_IS_AT_RISK",
        polymarket_us_key_id="key",
        polymarket_us_secret_key="secret",
    )
    executor = PolymarketLiveExecutor(settings)
    executor._client = Client()
    result = executor._buy_sync("us-market::NO", 0.50, 10)
    payload = Client.orders.payload
    assert payload["quantity"] == pytest.approx(10)
    assert payload["intent"] == "ORDER_INTENT_BUY_SHORT"
    assert payload["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
    assert result.fill.shares == pytest.approx(10)


def test_partial_book_prediction_does_not_mislabel_execution_as_full():
    fill = _fill_from_executions(
        [{"tradeId": "one", "lastShares": "5", "lastPx": {"value": "0.5"}}],
        10,
    )
    assert fill is not None
    assert not fill.fully_filled


def live_settings():
    return Settings(
        prediction_hunt_api_key="test",
        trading_mode="live",
        live_trading_enabled=True,
        live_trading_ack="I_UNDERSTAND_REAL_MONEY_IS_AT_RISK",
        polymarket_us_key_id="key",
        polymarket_us_secret_key="secret",
    )


class ResponseOrders:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    def create(self, payload):
        self.calls += 1
        if self.error:
            raise self.error
        return self.response


class ResponseClient:
    def __init__(self, orders):
        self.orders = orders


def execution_response(shares, *, state, execution_type, trade_id="trade-1"):
    return {
        "id": "order-123",
        "executions": [{
            "id": "execution-123",
            "type": execution_type,
            "tradeId": trade_id,
            "lastShares": str(shares) if shares else "0",
            "lastPx": {"value": "0.48", "currency": "USD"},
            "commissionNotionalCollected": {"value": "0.05", "currency": "USD"},
            "text": "",
            "orderRejectReason": "",
            "order": {
                "id": "order-123",
                "state": state,
                "cumQuantity": shares,
                "leavesQuantity": 10 - shares,
                "avgPx": {"value": "0.48", "currency": "USD"},
            },
        }],
    }


@pytest.mark.parametrize(
    ("shares", "execution_type", "state", "expected_status"),
    [
        (10, "EXECUTION_TYPE_FILL", "ORDER_STATE_FILLED", "filled"),
        (4, "EXECUTION_TYPE_PARTIAL_FILL", "ORDER_STATE_PARTIALLY_FILLED", "partially_filled"),
    ],
)
def test_realistic_success_responses_create_exact_fill_without_requiring_trade_id(
    shares, execution_type, state, expected_status, caplog
):
    response = execution_response(
        shares, state=state, execution_type=execution_type, trade_id=""
    )
    orders = ResponseOrders(response)
    executor = PolymarketLiveExecutor(live_settings())
    executor._client = ResponseClient(orders)

    with caplog.at_level("INFO"):
        result = executor._buy_sync("us-market::YES", 0.50, 10)

    assert result.status == expected_status
    assert result.fill.shares == pytest.approx(shares)
    assert orders.calls == 1
    assert "order_id=order-123" in caplog.text
    assert execution_type in caplog.text


@pytest.mark.parametrize(
    ("execution_type", "state", "classification"),
    [
        ("EXECUTION_TYPE_CANCELED", "ORDER_STATE_CANCELED", "canceled_no_fill"),
        ("EXECUTION_TYPE_NEW", "ORDER_STATE_NEW", "ioc_no_fill"),
        ("EXECUTION_TYPE_EXPIRED", "ORDER_STATE_EXPIRED", "expired_no_fill"),
    ],
)
def test_zero_fill_is_classified_and_never_retried(
    execution_type, state, classification, caplog
):
    orders = ResponseOrders(execution_response(
        0, state=state, execution_type=execution_type, trade_id=""
    ))
    executor = PolymarketLiveExecutor(live_settings())
    executor._client = ResponseClient(orders)

    with caplog.at_level("INFO"), pytest.raises(LiveOrderError) as caught:
        executor._buy_sync("us-market::NO", 0.56, 10)

    assert caught.value.classification == classification
    assert orders.calls == 1
    assert "Polymarket US order response" in caplog.text
    assert execution_type in str(caught.value)


def test_rejected_order_captures_reason_and_is_not_retried():
    response = execution_response(
        0,
        state="ORDER_STATE_REJECTED",
        execution_type="EXECUTION_TYPE_REJECTED",
        trade_id="",
    )
    response["executions"][0]["text"] = "insufficient buying power"
    response["executions"][0]["orderRejectReason"] = "INSUFFICIENT_BUYING_POWER"
    orders = ResponseOrders(response)
    executor = PolymarketLiveExecutor(live_settings())
    executor._client = ResponseClient(orders)

    with pytest.raises(LiveOrderError) as caught:
        executor._buy_sync("us-market::YES", 0.50, 10)

    assert caught.value.classification == "rejected"
    assert "INSUFFICIENT_BUYING_POWER" in str(caught.value)
    assert orders.calls == 1


def test_submission_error_is_classified_and_not_retried():
    orders = ResponseOrders(error=TimeoutError("request timed out"))
    executor = PolymarketLiveExecutor(live_settings())
    executor._client = ResponseClient(orders)

    with pytest.raises(LiveOrderError) as caught:
        executor._buy_sync("us-market::YES", 0.50, 10)

    assert caught.value.classification == "submission_error"
    assert orders.calls == 1


@pytest.mark.parametrize("response", ["unexpected", {"id": "order-123"}])
def test_invalid_response_is_classified_and_not_retried(response):
    orders = ResponseOrders(response=response)
    executor = PolymarketLiveExecutor(live_settings())
    executor._client = ResponseClient(orders)

    with pytest.raises(LiveOrderError) as caught:
        executor._buy_sync("us-market::YES", 0.50, 10)

    assert caught.value.classification == "invalid_response"
    assert orders.calls == 1


def test_aggregate_order_fill_is_used_when_fill_execution_omits_last_fields():
    response = execution_response(
        4,
        state="ORDER_STATE_PARTIALLY_FILLED",
        execution_type="EXECUTION_TYPE_PARTIAL_FILL",
        trade_id="",
    )
    del response["executions"][0]["lastShares"]
    del response["executions"][0]["lastPx"]
    response["executions"][0]["order"]["commissionNotionalTotalCollected"] = {
        "value": "0.02", "currency": "USD"
    }
    orders = ResponseOrders(response)
    executor = PolymarketLiveExecutor(live_settings())
    executor._client = ResponseClient(orders)

    result = executor._buy_sync("us-market::YES", 0.50, 10)

    assert result.status == "partially_filled"
    assert result.fill.shares == pytest.approx(4)
    assert result.fill.average_price == pytest.approx(0.48)
    assert result.fill.fee == pytest.approx(0.02)
