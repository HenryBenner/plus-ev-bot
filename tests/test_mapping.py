from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fadebot.db import Database
from fadebot.mapping import InternationalToUSMapper, MappingError, _same_team
from fadebot.models import MarketInfo


GAME_TIME = datetime(2026, 9, 20, 17, tzinfo=timezone.utc)


def market(
    slug: str,
    *,
    platform: str,
    title: str,
    long_label: str = "YES",
    short_label: str = "NO",
    long_aliases: tuple[str, ...] = (),
    short_aliases: tuple[str, ...] = (),
    event_time: datetime = GAME_TIME,
    event_slug: str | None = None,
    league: str = "",
) -> MarketInfo:
    return MarketInfo(
        market_id=slug,
        condition_id=slug,
        market_slug=slug,
        event_slug=event_slug or slug,
        title=title,
        event_time=event_time,
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
        long_aliases=long_aliases,
        short_aliases=short_aliases,
        league=league,
    )


class FakeUSClient:
    def __init__(
        self,
        *,
        nearby: list[MarketInfo] | None = None,
        searches: dict[str, list[MarketInfo]] | None = None,
        wide: list[MarketInfo] | None = None,
        all_markets: list[MarketInfo] | None = None,
    ):
        self.nearby = nearby or []
        self.searches = searches or {}
        self.wide = wide or []
        self.all_markets = all_markets or [*self.nearby, *self.wide]
        self.search_queries: list[str] = []
        self.near_calls = 0
        self.wide_calls = 0
        self.market_by_slug_calls = 0

    async def search_markets(self, query: str) -> list[MarketInfo]:
        self.search_queries.append(query)
        wanted = query.casefold()
        return self.searches.get(wanted, [])

    async def sports_markets_near(self, event_time: datetime) -> list[MarketInfo]:
        assert event_time == GAME_TIME
        self.near_calls += 1
        return self.nearby

    async def sports_markets_wide(self, event_time: datetime) -> list[MarketInfo]:
        assert event_time == GAME_TIME
        self.wide_calls += 1
        return self.wide

    async def market_by_slug(self, slug: str) -> MarketInfo:
        self.market_by_slug_calls += 1
        return next(item for item in self.all_markets if item.market_slug == slug)


async def mapper(tmp_path: Path, client: FakeUSClient) -> InternationalToUSMapper:
    database = Database(tmp_path / "mapping.db")
    await database.initialize()
    return InternationalToUSMapper(client, database)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_maps_exact_team_on_narrow_fast_path_and_caches_direction(tmp_path: Path):
    source = market(
        "international-mets",
        platform="international",
        title="Will New York Mets win on 2026-09-20?",
    )
    target = market(
        "us-mets-braves",
        platform="us",
        title="New York Mets vs Atlanta Braves",
        long_label="New York Mets",
        short_label="Atlanta Braves",
    )
    client = FakeUSClient(nearby=[target], all_markets=[target])
    instance = await mapper(tmp_path, client)

    mapping = await instance.map_market(source)

    assert mapping.market.market_slug == target.market_slug
    assert mapping.target_outcome("YES") == "YES"
    assert mapping.target_outcome("NO") == "NO"
    assert client.search_queries == []
    assert client.wide_calls == 0
    cached = await instance.database.get_market_mapping(source.market_slug)
    assert cached is not None
    assert cached["target_market_slug"] == target.market_slug

    cached_mapping = await instance.map_market(source)
    assert cached_mapping.method.startswith("cached_")
    assert client.market_by_slug_calls == 1


@pytest.mark.asyncio
async def test_ambiguous_genuine_candidates_are_rejected(tmp_path: Path):
    source = market(
        "international-mets",
        platform="international",
        title="Will New York Mets win?",
    )
    targets = [
        market(
            f"us-mets-{number}",
            platform="us",
            title="New York Mets game",
            long_label="New York Mets",
            short_label="Opponent",
        )
        for number in (1, 2)
    ]
    client = FakeUSClient(nearby=targets, wide=targets)

    with pytest.raises(MappingError, match="ambiguous_candidates"):
        await (await mapper(tmp_path, client)).map_market(source)

    assert client.wide_calls == 1


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Manchester City FC", "Manchester City"),
        ("FC Internazionale Milano", "Inter Milan"),
        ("BV Borussia 09 Dortmund", "Borussia 09 Dortmund"),
        ("TSG 1899 Hoffenheim", "Hoffenheim"),
        ("SV Darmstadt 98", "Darmstadt 98"),
        ("Olympique de Marseille", "Marseille"),
        ("Psim Yogyakarta", "PSIM Yogyakarta"),
        ("Atlético Madrid", "Atletico Madrid"),
    ],
)
def test_team_alias_normalization(left, right):
    assert _same_team(left, right)


def test_team_normalization_does_not_merge_different_clubs():
    assert not _same_team("Los Angeles Galaxy", "Los Angeles FC")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_team", "target_name", "target_aliases", "outcome"),
    [
        ("SV Darmstadt 98", "Darmstadt 98", (), "YES"),
        ("Psim Yogyakarta", "Perserikatan Sepakbola Indonesia Mataram", ("PSIM Yogyakarta", "PSIM"), "YES"),
        ("Olympique de Marseille", "Marseille", ("OM",), "YES"),
        ("TSG 1899 Hoffenheim", "Hoffenheim", ("TSG Hoffenheim",), "NO"),
    ],
)
async def test_historical_misses_map_through_wide_fallback(
    tmp_path, source_team, target_name, target_aliases, outcome
):
    source = market(
        f"intl-{source_team.casefold().replace(' ', '-')}",
        platform="international",
        title=f"Will {source_team} win?",
        event_slug=f"{source_team}-vs-known-opponent",
    )
    if outcome == "YES":
        long_label, short_label = target_name, "Known Opponent"
        long_aliases, short_aliases = target_aliases, ()
    else:
        long_label, short_label = "Known Opponent", target_name
        long_aliases, short_aliases = (), target_aliases
    target = market(
        f"us-{source_team.casefold().replace(' ', '-')}",
        platform="us",
        title=f"{target_name} vs Known Opponent",
        long_label=long_label,
        short_label=short_label,
        long_aliases=long_aliases,
        short_aliases=short_aliases,
        event_time=GAME_TIME + timedelta(hours=5),
    )
    client = FakeUSClient(wide=[target])

    result = await (await mapper(tmp_path, client)).map_market(source)

    assert result.market.market_slug == target.market_slug
    assert result.source_yes_target_outcome == outcome
    assert client.wide_calls == 1


@pytest.mark.asyncio
async def test_alias_and_abbreviation_metadata_are_used(tmp_path: Path):
    source = market(
        "intl-psg", platform="international", title="Will PSG win?"
    )
    target = market(
        "us-paris", platform="us", title="Paris vs Lyon",
        long_label="Paris Saint-Germain",
        long_aliases=("Paris SG", "PSG"),
        short_label="Olympique Lyonnais",
    )
    client = FakeUSClient(nearby=[target])
    result = await (await mapper(tmp_path, client)).map_market(source)
    assert result.market.market_slug == "us-paris"
    assert result.source_yes_target_outcome == "YES"


@pytest.mark.asyncio
async def test_normalized_team_search_runs_before_wide_window(tmp_path: Path):
    source = market(
        "intl-hoffenheim", platform="international",
        title="Will TSG 1899 Hoffenheim win?",
    )
    target = market(
        "us-hoffenheim", platform="us", title="Hoffenheim vs Mainz",
        long_label="Hoffenheim", short_label="Mainz",
    )
    client = FakeUSClient(searches={"hoffenheim": [target]})

    result = await (await mapper(tmp_path, client)).map_market(source)

    assert result.market.market_slug == target.market_slug
    assert client.search_queries[:2] == ["TSG 1899 Hoffenheim", "hoffenheim"]
    assert client.wide_calls == 0


@pytest.mark.asyncio
async def test_wrong_date_is_rejected_after_exhaustive_search(tmp_path: Path):
    source = market("intl-mets", platform="international", title="Will Mets win?")
    wrong_date = market(
        "us-mets-old", platform="us", title="Mets game",
        long_label="Mets", event_time=GAME_TIME + timedelta(hours=25),
    )
    client = FakeUSClient(
        searches={"mets": [wrong_date]}, wide=[wrong_date]
    )
    with pytest.raises(MappingError, match="no_us_equivalent"):
        await (await mapper(tmp_path, client)).map_market(source)


@pytest.mark.asyncio
async def test_closest_of_same_team_multiple_games_is_selected(tmp_path: Path):
    source = market("intl-mets", platform="international", title="Will Mets win?")
    close = market(
        "us-mets-close", platform="us", title="Mets close game",
        long_label="Mets", event_time=GAME_TIME + timedelta(minutes=15),
    )
    farther = market(
        "us-mets-farther", platform="us", title="Mets later game",
        long_label="Mets", event_time=GAME_TIME + timedelta(hours=3),
    )
    result = await (await mapper(
        tmp_path, FakeUSClient(nearby=[farther, close])
    )).map_market(source)
    assert result.market.market_slug == close.market_slug


@pytest.mark.asyncio
async def test_opponent_disambiguates_same_team_same_time(tmp_path: Path):
    source = market(
        "intl-united-fulham", platform="international",
        title="Will Manchester United win?",
        event_slug="manchester-united-vs-fulham",
    )
    correct = market(
        "us-united-fulham", platform="us",
        title="Fulham vs Manchester United",
        event_slug="fulham-vs-manchester-united",
        long_label="Fulham", short_label="Manchester United",
    )
    wrong = market(
        "us-united-chelsea", platform="us",
        title="Chelsea vs Manchester United",
        event_slug="chelsea-vs-manchester-united",
        long_label="Chelsea", short_label="Manchester United",
    )
    result = await (await mapper(
        tmp_path, FakeUSClient(nearby=[wrong, correct])
    )).map_market(source)
    assert result.market.market_slug == correct.market_slug
    assert result.source_yes_target_outcome == "NO"


@pytest.mark.asyncio
async def test_explicit_different_opponent_is_rejected(tmp_path: Path):
    source = market(
        "intl-united-fulham", platform="international",
        title="Will Manchester United win?",
        event_slug="manchester-united-vs-fulham",
    )
    wrong = market(
        "us-united-chelsea", platform="us",
        title="Manchester United vs Chelsea",
        event_slug="manchester-united-vs-chelsea",
        long_label="Manchester United", short_label="Chelsea",
    )
    client = FakeUSClient(nearby=[wrong], wide=[wrong])
    with pytest.raises(MappingError, match="no_us_equivalent"):
        await (await mapper(tmp_path, client)).map_market(source)


@pytest.mark.asyncio
async def test_absent_market_logs_diagnostics_and_returns_clear_reason(
    tmp_path: Path, caplog
):
    source = market("intl-none", platform="international", title="Will Nobody FC win?")
    client = FakeUSClient()
    with caplog.at_level("WARNING"), pytest.raises(
        MappingError, match="no_us_equivalent"
    ):
        await (await mapper(tmp_path, client)).map_market(source)
    assert "MAPPING FAILED" in caplog.text
    assert "nearby_candidates=0" in caplog.text
    assert "wide_window_candidates=0" in caplog.text
    assert "rejection_reason=no_us_equivalent" in caplog.text


@pytest.mark.asyncio
async def test_missing_team_metadata_has_distinct_reason(tmp_path: Path):
    source = market("intl-bad", platform="international", title="Match winner")
    with pytest.raises(MappingError, match="missing_team_metadata"):
        await (await mapper(tmp_path, FakeUSClient())).map_market(source)
