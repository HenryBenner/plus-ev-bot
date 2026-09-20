from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable

from .clients import PolymarketUSClient
from .db import Database
from .models import MarketInfo


logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class _ScoredCandidate:
    market: MarketInfo
    outcome: str
    score: float
    team_score: int
    time_delta: timedelta
    opponent_match: bool


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
            _log_mapping_failure(source, "", [], [], [], "missing_team_metadata")
            raise MappingError("missing_team_metadata")

        nearby: list[MarketInfo] = []
        if source.event_time is not None:
            nearby = await self.us_client.sports_markets_near(source.event_time)
            selected = _select_team_winner(source, team, nearby)
            if selected is not None:
                return _mapping_from_score(selected)

        searched: list[MarketInfo] = []
        searched.extend(await self.us_client.search_markets(team))
        combined = _unique_markets([*nearby, *searched])
        selected = _select_team_winner(source, team, combined)
        if selected is not None:
            return _mapping_from_score(selected)

        used_queries = {_normalize(team)}
        for query in _team_search_variants(team):
            normalized_query = _normalize(query)
            if not normalized_query or normalized_query in used_queries:
                continue
            used_queries.add(normalized_query)
            searched.extend(await self.us_client.search_markets(query))

        combined = _unique_markets([*nearby, *searched])
        selected = _select_team_winner(source, team, combined)
        if selected is not None:
            return _mapping_from_score(selected)

        wide: list[MarketInfo] = []
        if source.event_time is not None:
            wide = await self.us_client.sports_markets_wide(source.event_time)

        exhaustive = _unique_markets([*nearby, *searched, *wide])
        scored = _score_team_winner_candidates(source, team, exhaustive)
        selected = _dominant_candidate(scored)
        if selected is not None:
            return _mapping_from_score(selected)

        reason = "ambiguous_candidates" if scored else "no_us_equivalent"
        _log_mapping_failure(source, team, nearby, searched, wide, reason, scored)
        raise MappingError(reason)

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


def _mapping_from_score(candidate: _ScoredCandidate) -> USMarketMapping:
    return USMarketMapping(
        market=candidate.market,
        source_yes_target_outcome=candidate.outcome,
        method="team_date_moneyline",
    )


def _team_from_winner_question(title: str) -> str | None:
    match = re.search(r"^\s*will\s+(.+?)\s+win(?:\s|\?|$)", title, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _same_team(left: str, right: str) -> bool:
    return _team_match_quality(left, [right]) > 0


def _team_match_quality(team: str, names: Iterable[str]) -> int:
    wanted = _normalize_team(team)
    if not wanted:
        return 0
    wanted_compact = wanted.replace(" ", "")
    wanted_initials = _initialism(wanted)
    best = 0
    for name in names:
        candidate = _normalize_team(name)
        if not candidate:
            continue
        if wanted == candidate:
            best = max(best, 100)
            continue
        if wanted_compact == candidate.replace(" ", ""):
            best = max(best, 98)
            continue
        if wanted_initials and wanted_initials == candidate.replace(" ", ""):
            best = max(best, 96)
            continue
        candidate_initials = _initialism(candidate)
        if candidate_initials and candidate_initials == wanted_compact:
            best = max(best, 96)
            continue
        if _safe_team_partial_match(wanted, candidate):
            best = max(best, 90)
    return best


def _safe_team_partial_match(left: str, right: str) -> bool:
    left_tokens = left.split()
    right_tokens = right.split()
    if left in right or right in left:
        shorter = left_tokens if len(left_tokens) < len(right_tokens) else right_tokens
        generic = {
            "angeles", "city", "madrid", "new", "real", "sport", "united", "york"
        }
        return any(len(token) >= 5 and token not in generic for token in shorter)
    return len(left_tokens) == len(right_tokens) and all(
        x == y or (min(len(x), len(y)) >= 4 and (x.startswith(y) or y.startswith(x)))
        for x, y in zip(left_tokens, right_tokens)
    )


def _team_winner_matches(
    source: MarketInfo,
    team: str,
    candidates: list[MarketInfo],
) -> list[tuple[MarketInfo, str]]:
    """Compatibility helper returning every valid scored team/date match."""
    return [
        (candidate.market, candidate.outcome)
        for candidate in _score_team_winner_candidates(source, team, candidates)
    ]


def _select_team_winner(
    source: MarketInfo, team: str, candidates: list[MarketInfo]
) -> _ScoredCandidate | None:
    return _dominant_candidate(_score_team_winner_candidates(source, team, candidates))


def _score_team_winner_candidates(
    source: MarketInfo, team: str, candidates: list[MarketInfo]
) -> list[_ScoredCandidate]:
    scored: list[_ScoredCandidate] = []
    for market in _unique_markets(candidates):
        if market.category.casefold() != "sports" or market.market_type != "team_winner":
            continue
        if not _same_game_time(source, market):
            continue

        long_names = _side_names(market, "YES")
        short_names = _side_names(market, "NO")
        long_score = _team_match_quality(team, long_names)
        short_score = _team_match_quality(team, short_names)
        if max(long_score, short_score) <= 0 or long_score == short_score:
            continue
        outcome = "YES" if long_score > short_score else "NO"
        team_score = max(long_score, short_score)
        other_names = short_names if outcome == "YES" else long_names
        opponent_relation = _opponent_relation(source, market, team, other_names)
        if opponent_relation < 0:
            continue
        opponent_match = opponent_relation > 0
        delta = _game_time_delta(source, market)
        hours = delta.total_seconds() / 3600
        time_score = max(0.0, 48.0 - (2.0 * hours))
        league_score = 0.0
        if source.league and market.league:
            league_score = 12.0 if _normalize(source.league) == _normalize(market.league) else -8.0
        context_score = 20.0 * _event_context_similarity(source, market, team)
        scored.append(_ScoredCandidate(
            market=market,
            outcome=outcome,
            score=(team_score + time_score + league_score + context_score
                   + (40.0 if opponent_match else 0.0)),
            team_score=team_score,
            time_delta=delta,
            opponent_match=opponent_match,
        ))
    return sorted(
        scored,
        key=lambda item: (-item.score, item.time_delta, item.market.market_slug),
    )


def _dominant_candidate(
    candidates: list[_ScoredCandidate],
) -> _ScoredCandidate | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    first, second = candidates[:2]
    if first.opponent_match and not second.opponent_match:
        return first
    if first.team_score >= second.team_score + 6:
        return first
    if first.score >= second.score + 8:
        return first
    if (
        first.score > second.score
        and first.time_delta + timedelta(minutes=30) <= second.time_delta
    ):
        return first
    return None


def _side_names(market: MarketInfo, outcome: str) -> tuple[str, ...]:
    if outcome == "YES":
        label, aliases = market.long_label, market.long_aliases
    else:
        label, aliases = market.short_label, market.short_aliases
    values = list(aliases)
    if label and not label.casefold().startswith("not "):
        values.append(label)
    return tuple(dict.fromkeys(value for value in values if value))


def _source_mentions_any(
    source: MarketInfo, selected_team: str, names: Iterable[str]
) -> bool:
    context = _normalize(f"{source.title} {source.event_slug}")
    selected_tokens = set(_normalize_team(selected_team).split())
    context_tokens = set(context.split()) - selected_tokens
    for name in names:
        normalized = _normalize_team(name)
        tokens = set(normalized.split())
        if normalized and tokens and tokens.issubset(context_tokens):
            return True
    return False


def _opponent_relation(
    source: MarketInfo,
    target: MarketInfo,
    selected_team: str,
    target_opponent_names: Iterable[str],
) -> int:
    """Return 1 for a matching opponent, -1 for a conflict, or 0 if unknown."""
    source_opponent = _structured_opponent_tokens(
        f"{source.title} {source.event_slug}", selected_team
    )
    if not source_opponent:
        return 1 if _source_mentions_any(source, selected_team, target_opponent_names) else 0

    target_sets = [
        set(_normalize_team(name).split())
        for name in target_opponent_names
        if _normalize_team(name)
    ]
    target_opponent = _structured_opponent_tokens(
        f"{target.title} {target.event_slug}", selected_team
    )
    if target_opponent:
        target_sets.append(target_opponent)
    if not target_sets:
        return 0
    if any(source_opponent & tokens for tokens in target_sets):
        return 1
    return -1


def _structured_opponent_tokens(value: str, selected_team: str) -> set[str]:
    normalized = _normalize(value)
    tokens = normalized.split()
    if not any(marker in tokens for marker in ("vs", "v", "at", "against")):
        return set()
    ignored = {
        "will", "win", "vs", "v", "at", "against", "on", "match", "game",
        *(_normalize_team(selected_team).split()),
    }
    return {
        token for token in tokens
        if token not in ignored and not token.isdigit() and len(token) > 1
    }


def _event_context_similarity(
    source: MarketInfo, target: MarketInfo, selected_team: str
) -> float:
    ignored = {
        "will", "win", "vs", "v", "at", "on", "yes", "no", "moneyline",
        *(_normalize_team(selected_team).split()),
    }
    left = set(_normalize(f"{source.title} {source.event_slug}").split()) - ignored
    right = set(_normalize(f"{target.title} {target.event_slug}").split()) - ignored
    left = {token for token in left if not token.isdigit() and len(token) > 1}
    right = {token for token in right if not token.isdigit() and len(token) > 1}
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _team_search_variants(team: str) -> list[str]:
    normalized = _normalize_team(team)
    variants = [team, normalized]
    words = normalized.split()
    initials = _initialism(normalized)
    if initials and len(initials) >= 2:
        variants.append(initials.upper())
    if len(words) > 1:
        variants.append(" ".join(words[-2:]))
        variants.append(words[-1])
    return list(dict.fromkeys(value for value in variants if value))


def _normalize_team(value: str) -> str:
    ignored = {
        "ac", "afc", "bv", "ca", "cf", "club", "de", "del", "fc", "fbc",
        "ff", "fk", "olympique", "sc", "se", "sk", "sv", "the", "tsg",
    }
    words = [word for word in _normalize(value).split() if word not in ignored]
    if (
        len(words) > 1
        and len(words[0]) == 4
        and words[0].isdigit()
        and 1800 <= int(words[0]) <= 2029
    ):
        words.pop(0)
    return " ".join(words)


def _initialism(value: str) -> str:
    words = value.split()
    return "".join(word[0] for word in words) if len(words) >= 2 else ""


def _same_game_time(source: MarketInfo, target: MarketInfo) -> bool:
    if source.event_time is None or target.event_time is None:
        return False
    return abs(source.event_time - target.event_time) <= timedelta(hours=24)


def _game_time_delta(source: MarketInfo, target: MarketInfo) -> timedelta:
    if source.event_time is None or target.event_time is None:
        return timedelta.max
    return abs(source.event_time - target.event_time)


def _same_expiration(source: MarketInfo, target: MarketInfo) -> bool:
    left = source.expiration_time or source.event_time
    right = target.expiration_time or target.event_time
    if left is None or right is None:
        return False
    return abs(left - right) <= timedelta(hours=24)


def _unique_markets(markets: Iterable[MarketInfo]) -> list[MarketInfo]:
    unique: dict[str, MarketInfo] = {}
    for market in markets:
        unique[market.market_slug] = market
    return list(unique.values())


def _log_mapping_failure(
    source: MarketInfo,
    team: str,
    nearby: list[MarketInfo],
    searched: list[MarketInfo],
    wide: list[MarketInfo],
    reason: str,
    scored: list[_ScoredCandidate] | None = None,
) -> None:
    ranked = scored or []
    if not ranked:
        raw = _unique_markets([*nearby, *searched, *wide])
        raw.sort(key=lambda market: _game_time_delta(source, market))
        top_names = [market.title for market in raw[:5]]
        top_times = [
            market.event_time.isoformat() if market.event_time else None
            for market in raw[:5]
        ]
    else:
        top_names = [item.market.title for item in ranked[:5]]
        top_times = [
            item.market.event_time.isoformat() if item.market.event_time else None
            for item in ranked[:5]
        ]
    logger.warning(
        "MAPPING FAILED source_title=%s source_slug=%s team=%s event_time=%s "
        "nearby_candidates=%d team_search_candidates=%d wide_window_candidates=%d "
        "top_candidate_names=%s top_candidate_times=%s rejection_reason=%s",
        source.title,
        source.market_slug,
        team,
        source.event_time.isoformat() if source.event_time else None,
        len(nearby),
        len(_unique_markets(searched)),
        len(wide),
        top_names,
        top_times,
        reason,
    )


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(
        "".join(char.casefold() if char.isalnum() else " " for char in unaccented).split()
    )
