from datetime import datetime, timezone
from pathlib import Path

import pytest

from fadebot.db import Database
from fadebot.mapping import InternationalToUSMapper, MappingError, _same_team
from fadebot.models import MarketInfo


def market(
    slug: str,
    *,
    platform: str,
    title: str,
    long_label: str = "YES",
    short_label: str = "NO",
) -> MarketInfo:
    game_time = datetime(2026, 9, 20, 17, tzinfo=timezone.utc)
    return MarketInfo(
        market_id=slug,
        condition_id=slug,
        market_slug=slug,
        event_slug=slug,
        title=title,
        event_time=game_time,
        category="sports",
        outcomes=["YES", "NO"],
        token_ids=[f"{slug}::YES", f"{slug}::NO"],
        outcome_prices=[0.5, 0.5],
        closed=False,
        fees_enabled=True,
        platform=platform,
        market_type="team_winner",
        long_label=long_label,
        short_label=short_label,
    )


class FakeUSClient:
    def __init__(self, results: list[MarketInfo]):
        self.results = results

    async def search_markets(self, query: str) -> list[MarketInfo]:
        assert "Mets" in query
        return self.results

    async def sports_markets_near(self, event_time: datetime) -> list[MarketInfo]:
        assert event_time == datetime(2026, 9, 20, 17, tzinfo=timezone.utc)
        return self.results

    async def market_by_slug(self, slug: str) -> MarketInfo:
        return next(item for item in self.results if item.market_slug == slug)


@pytest.mark.asyncio
async def test_maps_team_date_and_caches_direction(tmp_path: Path):
    source = market(
        "international-mets",
        platform="international",
        title="Will New York Mets win on 2026-09-20?",
    )
    target = market(
        "us-mets-braves",
        platform="us",
        title="New York vs Atlanta",
        long_label="New York Mets",
        short_label="Atlanta Braves",
    )
    database = Database(tmp_path / "mapping.db")
    await database.initialize()
    mapping = await InternationalToUSMapper(
        FakeUSClient([target]), database  # type: ignore[arg-type]
    ).map_market(source)
    assert mapping.market.market_slug == target.market_slug
    assert mapping.target_outcome("YES") == "YES"
    assert mapping.target_outcome("NO") == "NO"
    cached = await database.get_market_mapping(source.market_slug)
    assert cached is not None
    assert cached["target_market_slug"] == target.market_slug


@pytest.mark.asyncio
async def test_ambiguous_team_mapping_is_rejected(tmp_path: Path):
    source = market(
        "international-mets",
        platform="international",
        title="Will New York Mets win?",
    )
    targets = [
        market(
            f"us-mets-{number}",
            platform="us",
            title="New York game",
            long_label="New York Mets",
            short_label="Opponent",
        )
        for number in (1, 2)
    ]
    database = Database(tmp_path / "mapping.db")
    await database.initialize()
    with pytest.raises(MappingError, match="candidates_2"):
        await InternationalToUSMapper(
            FakeUSClient(targets), database  # type: ignore[arg-type]
        ).map_market(source)


def test_team_alias_normalization():
    assert _same_team("Manchester City FC", "Manchester City")
    assert _same_team("FC Internazionale Milano", "Inter Milan")
    assert _same_team("BV Borussia 09 Dortmund", "Borussia 09 Dortmund")
    assert not _same_team("Los Angeles Galaxy", "Los Angeles FC")
