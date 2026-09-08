"""Synthetic slates for exercising the pipeline without live data.

Generated rather than recorded so a test can ask for a slate of any
shape, and seeded so failures reproduce. Salaries and projections are
correlated the way a real pool is -- expensive players score more --
because an optimizer tested against uncorrelated noise passes tests
that tell you nothing about whether it works.
"""

from __future__ import annotations

import random
from typing import Any

from dfs.sports import SportConfig

# Positions to generate per team, chosen so every roster slot in the
# corresponding config has several eligible candidates.
_POSITIONS: dict[str, dict[str, int]] = {
    "NFL": {"QB": 2, "RB": 4, "WR": 6, "TE": 3, "DST": 1},
    "NBA": {"PG": 3, "SG": 3, "SF": 3, "PF": 3, "C": 3},
    "MLB": {"SP": 2, "C": 2, "1B": 2, "2B": 2, "3B": 2, "SS": 2, "OF": 5},
    "NHL": {"C": 3, "W": 5, "D": 4, "G": 1},
    "EPL": {"F": 3, "M": 5, "D": 5, "GK": 1},
}

_TEAMS = [
    ("AAA", "BBB"), ("CCC", "DDD"), ("EEE", "FFF"),
    ("GGG", "HHH"), ("III", "JJJ"), ("KKK", "LLL"),
]


def make_pool(config: SportConfig, games: int = 4, seed: int = 7) -> list[dict[str, Any]]:
    """A believable player pool for one sport-site config."""

    generator = random.Random(seed)
    pool: list[dict[str, Any]] = []
    layout = _POSITIONS[config.sport]

    for away, home in _TEAMS[:games]:
        game_id = f"{away}@{home}"
        for team in (away, home):
            opponent = home if team == away else away
            for position, count in layout.items():
                for index in range(count):
                    salary = generator.randrange(3_000, 11_000, 100)
                    # Scale into each site's cap so a lineup is roughly
                    # affordable regardless of which cap applies.
                    salary = int(salary * config.salary_cap / 50_000 / config.roster_size * 9)
                    salary = max(salary, 2_000)
                    projection = max(salary / 1_000 * 3.0 + generator.uniform(-5, 5), 2.0)
                    pool.append(
                        {
                            "player_id": f"{config.sport.lower()}:{team}-{position}-{index}",
                            "name": f"{team} {position}{index}",
                            "positions": [position],
                            "roster_positions": [],
                            "team": team,
                            "opponent": opponent,
                            "game_id": game_id,
                            "salary": salary,
                            "projected_points": round(projection, 2),
                            "ceiling": round(projection * generator.uniform(1.3, 1.8), 2),
                            "floor": round(projection * 0.55, 2),
                            "projected_ownership": round(generator.uniform(0.5, 35.0), 2),
                        }
                    )

    return pool


def make_game_logs(
    player_id: str,
    games: int = 12,
    opportunity: float = 30.0,
    stats: dict[str, float] | None = None,
    seed: int = 3,
) -> list[dict[str, Any]]:
    """A run of game logs with mild noise on both opportunity and output."""

    generator = random.Random(seed)
    base = stats or {"pts": 22, "reb": 6, "ast": 5, "stl": 1, "blk": 0.5, "tov": 2, "fg3m": 2}

    logs = []
    for index in range(games):
        noise = generator.uniform(0.75, 1.25)
        logs.append(
            {
                "player_id": player_id,
                "game_date": f"2026-01-{index + 1:02d}",
                "opponent": "OPP",
                "home": index % 2 == 0,
                "opportunity": round(opportunity * generator.uniform(0.85, 1.15), 1),
                "stats": {key: round(value * noise, 2) for key, value in base.items()},
            }
        )

    return list(reversed(logs))


def unbuilt_sport() -> str:
    """A sport that has no stat collector yet.

    Asked of the registry rather than hardcoded. Tests that need an
    unbuilt sport broke every time a collector was added -- three times
    so far, once per sport -- and worse, one of them silently started
    downloading real seasons instead of raising, which turned a two
    second suite into a two minute one. Deriving the name means adding
    the next collector cannot break them.
    """

    import pytest

    from dfs.ingest.stats import PLANNED

    if not PLANNED:
        pytest.skip("every sport now has a collector")

    return sorted(PLANNED)[0]
