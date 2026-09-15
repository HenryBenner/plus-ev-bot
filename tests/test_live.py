import pytest

from fadebot.config import Settings
from fadebot.live import PolymarketLiveExecutor, _fill_from_executions, _split_market_side
from fadebot.models import PaperFill


def test_market_side_identifier():
    assert _split_market_side("some-market::NO") == ("some-market", "NO")
    with pytest.raises(ValueError):
        _split_market_side("some-market")


def test_us_execution_response_becomes_fill():
    expected = PaperFill(20, 9.8, 0.2, 10, 0.49, True)
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
        expected,
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
    expected = PaperFill(10, 4.5, 0, 4.5, 0.45, True)
    result = executor._buy_sync("us-market::NO", 0.50, expected, 10)
    payload = Client.orders.payload
    assert payload["quantity"] == pytest.approx(10)
    assert payload["intent"] == "ORDER_INTENT_BUY_SHORT"
    assert payload["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
    assert result.fill.shares == pytest.approx(10)
