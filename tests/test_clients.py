from datetime import datetime, timezone

import httpx
import pytest

from fadebot.clients import PolymarketUSClient, _us_market
from fadebot.config import Settings


MARKET = {
    "id": "42",
    "slug": "us-test-market",
    "question": "Will the test happen?",
    "category": "politics",
    "active": True,
    "closed": False,
    "endDate": "2026-09-06T00:00:00Z",
    "gameStartTime": "2026-09-04T18:00:00Z",
    "outcomePrices": '["0.40", "0.60"]',
    "orderPriceMinTickSize": 0.001,
    "minimumTradeQty": 1,
    "feeCoefficient": 0.05,
}


def settings() -> Settings:
    return Settings(
        prediction_hunt_api_key="test",
        polymarket_us_gateway_url="https://gateway.test",
    )


@pytest.mark.asyncio
async def test_resolves_us_market_and_converts_long_book():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/market/slug/us-test-market":
            return httpx.Response(200, json={"market": MARKET})
        if request.url.path == "/v1/markets/us-test-market/book":
            return httpx.Response(
                200,
                json={
                    "marketData": {
                        "offers": [{"px": {"value": "0.41"}, "qty": "5"}],
                        "bids": [{"px": {"value": "0.39"}, "qty": "7"}],
                    }
                },
            )
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PolymarketUSClient(settings(), http)
        market = await client.market_by_slug("us-test-market")
        assert market.token_for("YES") == "us-test-market::YES"
        assert market.expiration_time == datetime(
            2026, 9, 6, tzinfo=timezone.utc
        )
        book = await client.orderbook(market.token_for("YES"))
        assert book["asks"] == [{"price": 0.41, "size": "5"}]
        assert book["tick_size"] == pytest.approx(0.001)


@pytest.mark.asyncio
async def test_short_book_is_complement_of_long_bids():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/book"):
            return httpx.Response(
                200,
                json={
                    "marketData": {
                        "offers": [],
                        "bids": [{"px": {"value": "0.72"}, "qty": "8"}],
                    }
                },
            )
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PolymarketUSClient(settings(), http)
        book = await client.orderbook("us-test-market::NO")
        assert book["asks"][0]["price"] == pytest.approx(0.28)
        assert book["asks"][0]["size"] == "8"


@pytest.mark.asyncio
async def test_closed_market_uses_us_settlement_endpoint():
    closed = {**MARKET, "closed": True, "active": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/market/slug/us-test-market":
            return httpx.Response(200, json={"market": closed})
        if request.url.path.endswith("/settlement"):
            return httpx.Response(200, json={"settlement": 1})
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        market = await PolymarketUSClient(settings(), http).market_by_slug(
            "us-test-market"
        )
        assert market.closed
        assert market.outcome_prices == [1.0, 0.0]


def test_us_sports_market_uses_nested_team_identity():
    info = _us_market(
        {
            "id": "1",
            "slug": "mets-win",
            "question": "Moneyline",
            "category": "sports",
            "sportsMarketType": "moneyline",
            "gameStartTime": "2026-09-20T17:00:00Z",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.55", "0.45"]',
            "marketSides": [
                {
                    "long": True,
                    "description": "Yes",
                    "team": {"name": "New York Mets"},
                },
                {
                    "long": False,
                    "description": "No",
                    "team": {"name": "New York Mets"},
                },
            ],
        },
        {},
    )
    assert info.market_type == "team_winner"
    assert info.long_label == "New York Mets"
    assert info.short_label == "not New York Mets"
