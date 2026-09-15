from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from .clients import PolymarketUSClient
from .db import Database
from .models import MarketInfo


class MappingError(ValueError):
    pass


@dataclass(frozen=True)
class USMarketMapping:
    market: MarketInfo
    source_yes_target_outcome: str
    method: str

    def target_outcome(self, source_outcome: str) -> str:
        if source_outcome.upper() == "YES":
            return self.source_yes_target_outcome
        return "NO" if self.source_yes_target_outcome == "YES" else "YES"


class InternationalToUSMapper:
    """Strict, cached International-to-US mapping; never guesses on a tie."""

    def __init__(self, us_client: PolymarketUSClient, database: Database):
        self.us_client = us_client
        self.database = database

    async def map_market(self, source: MarketInfo) -> USMarketMapping:
        cached = await self.database.get_market_mapping(source.market_slug)
        if cached:
            try:
                market = await self.us_client.market_by_slug(
                    str(cached["target_market_slug"])
                )
                if not market.closed:
                    return USMarketMapping(
                        market=market,
                        source_yes_target_outcome=str(
                            cached["source_yes_target_outcome"]
                        ),
                        method="cached_" + str(cached["mapping_method"]),
                    )
            except Exception:
                pass

        if source.category.casefold() == "sports" and source.market_type == "team_winner":
            mapping = await self._map_team_winner(source)
        else:
            mapping = await self._map_exact_title(source)
        await self.database.save_market_mapping(
            source_slug=source.market_slug,
            target_slug=mapping.market.market_slug,
            source_yes_target_outcome=mapping.source_yes_target_outcome,
            method=mapping.method,
            source_title=source.title,
        )
        return mapping

    async def _map_team_winner(self, source: MarketInfo) -> USMarketMapping:
        team = _team_from_winner_question(source.title)
        if not team:
            raise MappingError("team_name_not_found")
        candidates = await self.us_client.search_markets(team)
        matches: list[tuple[MarketInfo, str]] = []
        for market in candidates:
            if market.category.casefold() != "sports" or market.market_type != "team_winner":
                continue
            if not _same_game_time(source, market):
                continue
            if _same_team(team, market.long_label):
                matches.append((market, "YES"))
            elif _same_team(team, market.short_label):
                matches.append((market, "NO"))
        if len(matches) != 1:
            raise MappingError(f"team_winner_candidates_{len(matches)}")
        market, source_yes_target_outcome = matches[0]
        return USMarketMapping(
            market=market,
            source_yes_target_outcome=source_yes_target_outcome,
            method="team_date_moneyline",
        )

    async def _map_exact_title(self, source: MarketInfo) -> USMarketMapping:
        wanted = _normalize(source.title)
        candidates = await self.us_client.search_markets(source.title)
        matches = [
            market
            for market in candidates
            if _normalize(market.title) == wanted and _same_expiration(source, market)
        ]
        if len(matches) != 1:
            raise MappingError(f"exact_title_candidates_{len(matches)}")
        return USMarketMapping(matches[0], "YES", "exact_title_date")


def _team_from_winner_question(title: str) -> str | None:
    match = re.search(r"^\s*will\s+(.+?)\s+win(?:\s|\?|$)", title, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _same_team(left: str, right: str) -> bool:
    a = _normalize_team(left)
    b = _normalize_team(right)
    return bool(a and b and (a == b or (len(a) >= 4 and a in b) or (len(b) >= 4 and b in a)))


def _normalize_team(value: str) -> str:
    ignored = {"fc", "cf", "sc", "the"}
    return " ".join(word for word in _normalize(value).split() if word not in ignored)


def _same_game_time(source: MarketInfo, target: MarketInfo) -> bool:
    if source.event_time is None or target.event_time is None:
        return False
    return abs(source.event_time - target.event_time) <= timedelta(hours=24)


def _same_expiration(source: MarketInfo, target: MarketInfo) -> bool:
    left = source.expiration_time or source.event_time
    right = target.expiration_time or target.event_time
    if left is None or right is None:
        return False
    return abs(left - right) <= timedelta(hours=24)


def _normalize(value: str) -> str:
    return " ".join(
        "".join(char.casefold() if char.isalnum() else " " for char in value).split()
    )
