from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
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
                    await ws.send(
                        json.dumps(
                            {
                                "action": "auth",
                                "api_key": self.settings.prediction_hunt_api_key,
                            }
                        )
                    )
                    await ws.send(
                        json.dumps(
                            {"action": "subscribe", "channel": "fade_finder"}
                        )
                    )
                    logger.info("Connected to Prediction Hunt Fade Finder")
                    delay = 1
                    async for raw_message in ws:
                        message = json.loads(raw_message)
                        if message.get("type") == "error":
                            raise RuntimeError(
                                f"Prediction Hunt error "
                                f"{message.get('code')}: {message.get('message')}"
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
                    "reconnecting in %ss",
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
            except Exception:
                logger.exception(
                    "Fade Finder connection failed; reconnecting in %ss", delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

class PolymarketClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self.http = http

    async def resolve_market(self, slug: str, title: str) -> MarketInfo:
        market_response = await self.http.get(
            f"{self.settings.polymarket_gamma_url}/markets/slug/{slug}"
        )
        if market_response.status_code == 200:
            market = market_response.json()
            event = _first(market.get("events")) or {}
            return _to_market_info(market, event)
        if market_response.status_code not in {404, 422}:
            market_response.raise_for_status()

        event_response = await self.http.get(
            f"{self.settings.polymarket_gamma_url}/events/slug/{slug}"
        )
        event_response.raise_for_status()
        event = event_response.json()
        markets = event.get("markets") or []
        if not markets:
            raise ValueError("Polymarket event contains no markets")

        normalized_title = _normalize(title)
        exact = [m for m in markets if str(m.get("slug")) == slug]
        title_matches = [
            m
            for m in markets
            if normalized_title
            and normalized_title
            in {
                _normalize(str(m.get("question") or "")),
                _normalize(str(m.get("groupItemTitle") or "")),
            }
        ]
        open_markets = [
            m
            for m in markets
            if not m.get("closed") and m.get("enableOrderBook", True)
        ]
        candidates = exact or title_matches or open_markets
        if len(candidates) != 1:
            raise ValueError(
                f"market slug resolves to {len(candidates)} possible markets"
            )
        return _to_market_info(candidates[0], event)

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
        event = _first(market.get("events")) or {}
        return _to_market_info(market, event)


def _to_market_info(market: dict[str, Any], event: dict[str, Any]) -> MarketInfo:
    outcomes = [str(item) for item in _json_list(market.get("outcomes"))]
    token_ids = [str(item) for item in _json_list(market.get("clobTokenIds"))]
    outcome_prices = [
        float(item) for item in _json_list(market.get("outcomePrices"))
    ]
    if len(outcomes) != len(token_ids):
        raise ValueError("Polymarket outcome/token mapping is incomplete")
    if len(outcome_prices) != len(outcomes):
        outcome_prices = [0.0] * len(outcomes)

    game_time = (
        parse_datetime(market.get("gameStartTime"))
        or parse_datetime(event.get("gameStartTime"))
    )
    expiration_time = (
        parse_datetime(market.get("endDate"))
        or parse_datetime(event.get("endDate"))
    )
    category = str(event.get("category") or market.get("category") or "")
    if not category and (
        market.get("gameStartTime")
        or event.get("gameStartTime")
        or market.get("sportsMarketType")
    ):
        category = "sports"
    return MarketInfo(
        market_id=str(market.get("id") or ""),
        condition_id=str(market.get("conditionId") or ""),
        market_slug=str(market.get("slug") or ""),
        event_slug=str(event.get("slug") or market.get("slug") or ""),
        title=str(
            market.get("question")
            or market.get("groupItemTitle")
            or event.get("title")
            or market.get("slug")
            or ""
        ),
        event_time=game_time or expiration_time,
        category=category,
        outcomes=outcomes,
        token_ids=token_ids,
        outcome_prices=outcome_prices,
        closed=bool(market.get("closed")),
        fees_enabled=bool(market.get("feesEnabled")),
        fee_rate=_fee_rate(category),
        neg_risk=bool(market.get("negRisk") or event.get("negRisk")),
        expiration_time=expiration_time,
    )


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed
    return []


def _first(value: Any) -> Any | None:
    return value[0] if isinstance(value, list) and value else None


def _normalize(value: str) -> str:
    return " ".join(
        "".join(char.casefold() if char.isalnum() else " " for char in value).split()
    )


def _fee_rate(category: str) -> float:
    normalized = category.strip().casefold()
    rates = {
        "crypto": 0.07,
        "sports": 0.03,
        "finance": 0.04,
        "politics": 0.04,
        "economics": 0.05,
        "culture": 0.05,
        "weather": 0.05,
        "tech": 0.04,
        "mentions": 0.04,
        "geopolitics": 0.0,
    }
    return rates.get(normalized, 0.05)
