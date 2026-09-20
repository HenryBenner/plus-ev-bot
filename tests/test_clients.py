from datetime import datetime, timezone

import httpx
import pytest

from fadebot.clients import PolymarketUSClient, _market_type, _us_market
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
async def test_date_bounded_sports_lookup_uses_event_time():
    seen_query = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_query
        assert request.url.path == "/v1/events"
        seen_query = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "events": [
                    {
                        "slug": "us-test-event",
                        "startTime": "2026-09-04T18:00:00Z",
                        "category": "sports",
                        "markets": [MARKET],
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PolymarketUSClient(settings(), http)
        markets = await client.sports_markets_near(
            datetime(2026, 9, 4, 18, tzinfo=timezone.utc)
        )
    assert [market.market_slug for market in markets] == ["us-test-market"]
    assert seen_query is not None
    assert seen_query["active"] == "true"
    assert seen_query["startTimeMin"] == "2026-09-04T16:00:00Z"
    assert seen_query["startTimeMax"] == "2026-09-04T20:00:00Z"


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
                    "team": {
                        "name": "New York Mets",
                        "alias": "NY Mets",
                        "safeName": "new-york-mets",
                        "abbreviation": "NYM",
                        "league": "MLB",
                    },
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
    assert info.long_aliases == ("New York Mets", "NY Mets", "new-york-mets", "NYM")
    assert info.short_aliases == ()
    assert info.league == "MLB"


@pytest.mark.asyncio
async def test_wide_sports_lookup_fully_paginates_24_hour_window():
    seen_offsets = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/events"
        offset = int(request.url.params["offset"])
        seen_offsets.append(offset)
        count = 100 if offset == 0 else 1
        events = []
        for index in range(count):
            item = {
                **MARKET,
                "id": f"{offset}-{index}",
                "slug": f"market-{offset}-{index}",
                "category": "sports",
            }
            events.append({
                "slug": f"event-{offset}-{index}",
                "category": "sports",
                "markets": [item],
            })
        return httpx.Response(200, json={"events": events})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PolymarketUSClient(settings(), http)
        markets = await client.sports_markets_wide(
            datetime(2026, 9, 4, 18, tzinfo=timezone.utc)
        )

    assert seen_offsets == [0, 100]
    assert len(markets) == 101


def test_team_winner_excludes_partial_game_markets():
    assert (
        _market_type(
            "soccer_team_full_time_winner",
            "sports",
            "Will Arsenal FC win against Chelsea FC?",
        )
        == "team_winner"
    )
    assert (
        _market_type(
            "soccer_team_second_half_winner",
            "sports",
            "Will Arsenal FC win the second half against Chelsea FC?",
        )
        == "soccer_team_second_half_winner"
    )
    assert (
        _market_type(
            "soccer_game_exact_score",
            "sports",
            "Will ARS vs CHE finish ARS wins 2-1?",
        )
        == "soccer_game_exact_score"
    )
