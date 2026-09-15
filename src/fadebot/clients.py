from __future__ import annotations

import asyncio
import json
import logging
import time
import unicodedata
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import websockets

from .config import Settings
from .models import MarketInfo, parse_datetime

logger = logging.getLogger(__name__)


class PredictionHuntClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def fade_messages(self) -> AsyncIterator[dict[str, Any]]:
        delay = 1
        while True:
            try:
                async with websockets.connect(
                    self.settings.prediction_hunt_ws_url,
                    open_timeout=self.settings.websocket_open_timeout_seconds,
                    close_timeout=10,
                    ping_interval=30,
                    ping_timeout=30,
                    max_size=2**20,
                ) as ws:
                    await ws.send(json.dumps({
                        "action": "auth",
                        "api_key": self.settings.prediction_hunt_api_key,
                    }))
                    await ws.send(json.dumps({
                        "action": "subscribe", "channel": "fade_finder"
                    }))
                    logger.info("Connected to Prediction Hunt Fade Finder")
                    delay = 1
                    async for raw_message in ws:
                        message = json.loads(raw_message)
                        if message.get("type") == "error":
                            raise RuntimeError(
                                f"Prediction Hunt error {message.get('code')}: "
                                f"{message.get('message')}"
                            )
                        if (
                            message.get("channel") == "fade_finder"
                            and message.get("type") == "fade_finder_update"
                        ):
                            yield message
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                logger.warning(
                    "Prediction Hunt WebSocket handshake timed out; "
                    "reconnecting in %ss", delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
            except Exception:
                logger.exception(
                    "Fade Finder connection failed; reconnecting in %ss", delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)


class PolymarketInternationalClient:
    """International market data used for signals and paper accounting."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self.http = http

    async def resolve_market(self, slug: str, title: str) -> MarketInfo:
        response = await self.http.get(
            f"{self.settings.polymarket_gamma_url}/markets/slug/{slug}"
        )
        if response.status_code == 200:
            market = response.json()
            return _international_market(market, _first(market.get("events")) or {})
        if response.status_code not in {404, 422}:
            response.raise_for_status()

        response = await self.http.get(
            f"{self.settings.polymarket_gamma_url}/events/slug/{slug}"
        )
        response.raise_for_status()
        event = response.json()
        markets = event.get("markets") or []
        normalized_title = _normalize(title)
        exact = [m for m in markets if str(m.get("slug")) == slug]
        title_matches = [
            m for m in markets
            if normalized_title in {
                _normalize(str(m.get("question") or "")),
                _normalize(str(m.get("groupItemTitle") or "")),
            }
        ]
        open_markets = [
            m for m in markets
            if not m.get("closed") and m.get("enableOrderBook", True)
        ]
        candidates = exact or title_matches or open_markets
        if len(candidates) != 1:
            raise ValueError(f"market slug resolves to {len(candidates)} markets")
        return _international_market(candidates[0], event)

    async def orderbook(self, token_id: str) -> dict[str, Any]:
        response = await self.http.get(
            f"{self.settings.polymarket_clob_url}/book",
            params={"token_id": token_id},
        )
        response.raise_for_status()
        return response.json()

    async def market_by_slug(self, slug: str) -> MarketInfo:
        response = await self.http.get(
            f"{self.settings.polymarket_gamma_url}/markets/slug/{slug}"
        )
        response.raise_for_status()
        market = response.json()
        return _international_market(market, _first(market.get("events")) or {})


class PolymarketUSClient:
    """US market data plus strict search primitives used by the mapper."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self.http = http
        self._markets: dict[str, MarketInfo] = {}
        self._request_lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def search_markets(self, query: str) -> list[MarketInfo]:
        response = await self._get(
            f"{self.settings.polymarket_us_gateway_url}/v1/search",
            params={"query": query, "limit": 20, "status": "active"},
        )
        response.raise_for_status()
        return [
            self._remember(_us_market(market, event))
            for event in response.json().get("events") or []
            for market in event.get("markets") or []
            if not market.get("closed") and market.get("active", True)
        ]

    async def sports_markets_near(self, event_time: datetime) -> list[MarketInfo]:
        """Return active US markets starting near an International event time."""
        event_time = event_time.astimezone(timezone.utc)
        start = (event_time - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        end = (event_time + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        markets: dict[str, MarketInfo] = {}
        for offset in range(0, 300, 100):
            response = await self._get(
                f"{self.settings.polymarket_us_gateway_url}/v1/events",
                params={
                    "active": "true",
                    "startTimeMin": start,
                    "startTimeMax": end,
                    "limit": 100,
                    "offset": offset,
                },
            )
            response.raise_for_status()
            events = response.json().get("events") or []
            for event in events:
                for market in event.get("markets") or []:
                    if market.get("closed") or not market.get("active", True):
                        continue
                    info = self._remember(_us_market(market, event))
                    markets[info.market_slug] = info
            if len(events) < 100:
                break
        return list(markets.values())

    async def market_by_slug(self, slug: str) -> MarketInfo:
        response = await self._get(
            f"{self.settings.polymarket_us_gateway_url}/v1/market/slug/{slug}"
        )
        response.raise_for_status()
        market = response.json().get("market") or response.json()
        info = _us_market(market, {})
        if info.closed:
            settlement = await self._get(
                f"{self.settings.polymarket_us_gateway_url}"
                f"/v1/markets/{info.market_slug}/settlement"
            )
            if settlement.status_code == 200:
                yes_price = float(settlement.json()["settlement"])
                info = MarketInfo(**{
                    **info.__dict__,
                    "outcome_prices": [yes_price, 1.0 - yes_price],
                })
        return self._remember(info)

    async def orderbook(self, market_side: str) -> dict[str, Any]:
        slug, outcome = split_us_market_side(market_side)
        response = await self._get(
            f"{self.settings.polymarket_us_gateway_url}/v1/markets/{slug}/book"
        )
        response.raise_for_status()
        data = response.json().get("marketData") or {}
        if outcome == "YES":
            asks = [
                {"price": _amount(level.get("px")), "size": level.get("qty")}
                for level in data.get("offers") or []
            ]
        else:
            asks = [
                {"price": 1.0 - _amount(level.get("px")), "size": level.get("qty")}
                for level in data.get("bids") or []
            ]
        market = self._markets.get(slug)
        return {
            "asks": asks,
            "tick_size": market.tick_size if market else 0.001,
            "min_order_size": market.min_order_size if market else 1.0,
        }

    def _remember(self, market: MarketInfo) -> MarketInfo:
        self._markets[market.market_slug] = market
        return market

    async def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        async with self._request_lock:
            response: httpx.Response | None = None
            for attempt in range(5):
                spacing = 0.35 - (time.monotonic() - self._last_request_at)
                if spacing > 0:
                    await asyncio.sleep(spacing)
                response = await self.http.get(url, **kwargs)
                self._last_request_at = time.monotonic()
                if response.status_code not in {429, 502, 503, 504}:
                    return response
                if attempt < 4:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = (
                            min(max(float(retry_after), 1.0), 8.0)
                            if retry_after
                            else min(1.0 * (2**attempt), 8.0)
                        )
                    except ValueError:
                        delay = min(1.0 * (2**attempt), 8.0)
                    await asyncio.sleep(delay)
            assert response is not None
            return response


def _international_market(market: dict[str, Any], event: dict[str, Any]) -> MarketInfo:
    outcomes = [str(value) for value in _json_list(market.get("outcomes"))]
    token_ids = [str(value) for value in _json_list(market.get("clobTokenIds"))]
    prices = [float(value) for value in _json_list(market.get("outcomePrices"))]
    if len(outcomes) != len(token_ids):
        raise ValueError("International outcome/token mapping is incomplete")
    if len(prices) != len(outcomes):
        prices = [0.0] * len(outcomes)
    category = str(event.get("category") or market.get("category") or "")
    market_type = str(
        market.get("sportsMarketType")
        or market.get("marketType")
        or event.get("marketType")
        or ""
    )
    game_time = parse_datetime(market.get("gameStartTime")) or parse_datetime(
        event.get("gameStartTime")
    )
    if not category and (game_time is not None or market_type):
        category = "sports"
    expiration = parse_datetime(market.get("endDate")) or parse_datetime(
        event.get("endDate")
    )
    return MarketInfo(
        market_id=str(market.get("id") or ""),
        condition_id=str(market.get("conditionId") or ""),
        market_slug=str(market.get("slug") or ""),
        event_slug=str(event.get("slug") or market.get("slug") or ""),
        title=str(market.get("question") or market.get("groupItemTitle") or ""),
        event_time=game_time or expiration,
        category=category,
        outcomes=outcomes,
        token_ids=token_ids,
        outcome_prices=prices,
        closed=bool(market.get("closed")),
        fees_enabled=bool(market.get("feesEnabled")),
        fee_rate=_fee_rate(category),
        neg_risk=bool(market.get("negRisk") or event.get("negRisk")),
        expiration_time=expiration,
        tick_size=float(market.get("orderPriceMinTickSize") or 0.01),
        min_order_size=float(market.get("orderMinSize") or 0),
        platform="international",
        market_type=_market_type(market_type, category, str(market.get("question") or "")),
    )


def _us_market(market: dict[str, Any], event: dict[str, Any]) -> MarketInfo:
    slug = str(market.get("slug") or "")
    sides = market.get("marketSides") or []
    selected_team = next(
        (
            str((side.get("team") or {}).get("name") or "")
            for side in sides
            if side.get("long") is True and (side.get("team") or {}).get("name")
        ),
        "",
    )
    long_label = next(
        (str(side.get("description") or "") for side in sides if side.get("long") is True),
        "YES",
    )
    short_label = next(
        (str(side.get("description") or "") for side in sides if side.get("long") is False),
        "NO",
    )
    # US sports markets name the selected team in a nested object; the side
    # descriptions themselves are normally just Yes and No.
    if selected_team:
        long_label = selected_team
        short_label = f"not {selected_team}"
    raw_prices = [float(value) for value in _json_list(market.get("outcomePrices"))]
    if len(raw_prices) != 2:
        long_price = _optional_float(
            market.get("lastTradePrice") or market.get("bestAsk") or _side_price(sides, True)
        )
        raw_prices = [long_price, 1.0 - long_price] if long_price is not None else [0.0, 0.0]
    category = str(event.get("category") or market.get("category") or "")
    market_type = str(
        market.get("sportsMarketType")
        or market.get("sportsMarketTypeV2")
        or market.get("marketType")
        or ""
    )
    game_time = parse_datetime(market.get("gameStartTime")) or parse_datetime(
        event.get("gameStartTime")
    )
    expiration = parse_datetime(market.get("endDate")) or parse_datetime(
        event.get("endDate")
    )
    coefficient = float(market.get("feeCoefficient") or 0.05)
    return MarketInfo(
        market_id=str(market.get("id") or slug),
        condition_id=str(market.get("id") or slug),
        market_slug=slug,
        event_slug=str(event.get("slug") or slug),
        title=str(market.get("question") or event.get("title") or slug),
        event_time=game_time or expiration,
        category=category,
        outcomes=["YES", "NO"],
        token_ids=[f"{slug}::YES", f"{slug}::NO"],
        outcome_prices=raw_prices,
        closed=bool(market.get("closed")),
        fees_enabled=coefficient > 0,
        fee_rate=coefficient,
        expiration_time=expiration,
        tick_size=float(market.get("orderPriceMinTickSize") or 0.001),
        min_order_size=float(market.get("minimumTradeQty") or 1),
        platform="us",
        market_type=_market_type(market_type, category, str(market.get("question") or "")),
        long_label=long_label,
        short_label=short_label,
    )


def split_us_market_side(value: str) -> tuple[str, str]:
    slug, separator, outcome = value.rpartition("::")
    if not separator or outcome not in {"YES", "NO"}:
        raise ValueError("Invalid Polymarket US market-side identifier")
    return slug, outcome


def normalize_filter(value: str) -> str:
    return value.strip().casefold().replace("-", "_").replace(" ", "_")


def _market_type(value: str, category: str, title: str) -> str:
    normalized = normalize_filter(value)
    normalized_title = normalize_filter(title)
    if normalize_filter(category) == "sports":
        partial_period = any(
            marker in normalized
            for marker in ("first_half", "second_half", "first_period", "second_period")
        ) or any(
            marker in normalized_title
            for marker in ("at_halftime", "first_half", "second_half")
        )
        full_game_winner = (
            normalized in {
                "moneyline",
                "sports_market_type_moneyline",
                "winner",
                "match_winner",
                "drawable_outcome",
            }
            or "full_time_winner" in normalized
            or normalized.endswith("_moneyline")
        )
        title_winner = (
            normalized in {"", "unknown"}
            and title.casefold().startswith("will ")
            and " win" in title.casefold()
        )
        if not partial_period and (full_game_winner or title_winner):
            return "team_winner"
    return normalized or "unknown"


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    return []


def _first(value: Any) -> Any | None:
    return value[0] if isinstance(value, list) and value else None


def _amount(value: Any) -> float:
    return float(value.get("value") if isinstance(value, dict) else value)


def _optional_float(value: Any) -> float | None:
    return None if value in (None, "") else float(value)


def _side_price(sides: list[dict[str, Any]], long: bool) -> Any:
    return next((side.get("price") for side in sides if side.get("long") is long), None)


def _normalize(value: str) -> str:
    value = "".join(
        char
        for char in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(char)
    )
    return " ".join(
        "".join(char.casefold() if char.isalnum() else " " for char in value).split()
    )


def _fee_rate(category: str) -> float:
    return {
        "crypto": 0.07,
        "sports": 0.03,
        "finance": 0.04,
        "politics": 0.04,
        "geopolitics": 0.0,
    }.get(category.strip().casefold(), 0.05)
