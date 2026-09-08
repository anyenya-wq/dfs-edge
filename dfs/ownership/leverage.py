"""Projected ownership and tournament leverage.

Ownership is the half of tournament strategy that a projection model
cannot see. Two lineups with identical projected totals are worth wildly
different amounts if one is rostered by twelve percent of the field and
the other by half a percent: the first splits its prize many ways, the
second does not. So the quantity a tournament lineup should maximise is
not projected points but projected points *relative to what the field
will own*.

The model here is deliberately simple, because the thing it must get
right is the ordering, not the level. Ownership is driven overwhelmingly
by perceived value -- points per thousand dollars of salary -- with a
secondary pull toward expensive players everyone has heard of. Both are
computable from the pool itself, which means this runs with no external
data at all.

One constraint makes the output self-consistent: every entry in a
contest rosters exactly `roster_size` players, so ownership across the
whole pool must sum to `roster_size * 100` percent. Normalising to that
total is what turns arbitrary scores into numbers that behave like
percentages, and it is why the projections here stay sane on a
six-game slate and a two-game one alike.

Replace this with real ownership data the moment you can get it -- the
industry projections are genuinely better, because they see contest
entry behaviour this cannot. Until then this is a reasonable prior, and
because it is normalised it degrades gracefully rather than absurdly.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from typing import Any

# How sharply ownership concentrates on the best values, measured
# against *relative* value rather than a z-score. The distinction
# matters: z-scoring divides by the pool's own spread, so on a slate
# where every player offers similar value it amplifies pure noise into
# enormous ownership differences. Relative value has no such feedback --
# a pool with no real value spread correctly yields near-flat ownership.
VALUE_SENSITIVITY = 4.0

# Ownership's secondary pull toward high salary at equal value.
# Expensive players are familiar, and the field rosters familiar names
# somewhat beyond what their value alone justifies.
SALARY_SENSITIVITY = 0.8

# No single player is rostered by more than about this share of a field,
# even the most obvious play on a slate.
MAX_OWNERSHIP = 65.0

# A floor so no rosterable player projects to exactly zero, which would
# make leverage ratios undefined.
MIN_OWNERSHIP = 0.2


def _z_scores(values: Sequence[float]) -> list[float]:
    if len(values) < 2:
        return [0.0] * len(values)

    mean = statistics.fmean(values)
    deviation = statistics.pstdev(values)

    if deviation == 0:
        return [0.0] * len(values)

    return [(value - mean) / deviation for value in values]


def value_per_thousand(player: Mapping[str, Any]) -> float:
    """Projected points per $1,000 of salary -- the field's core heuristic."""

    salary = float(player.get("salary") or 0.0)
    if salary <= 0:
        return 0.0
    return float(player.get("projected_points") or 0.0) / (salary / 1_000.0)


def _relative(values: Sequence[float]) -> list[float]:
    """Each value as a proportional deviation from the mean.

    Unlike a z-score this does not divide by the spread, so a pool with
    little genuine variation produces little variation in the output
    instead of magnifying noise to fill the scale.
    """

    if not values:
        return []

    mean = statistics.fmean(values)
    if mean == 0:
        return [0.0] * len(values)

    return [(value - mean) / abs(mean) for value in values]


def _distribute(weights: Sequence[float], budget: float) -> list[float]:
    """Split `budget` across `weights`, respecting the ownership ceiling.

    Clipping a share at the ceiling frees the excess, which belongs to
    the remaining players -- a contest still distributes the full
    budget. Simple clipping would silently lose it, which is what makes
    naive ownership projections fail to sum to roster_size * 100.
    """

    total = sum(weights)
    if total <= 0:
        return [0.0] * len(weights)

    shares = [budget * weight / total for weight in weights]
    capped: list[bool] = [False] * len(shares)

    # Each pass caps whoever now exceeds the ceiling and reallocates
    # their overflow among the rest. Converges in a handful of rounds.
    for _ in range(20):
        excess = 0.0
        for index, share in enumerate(shares):
            if not capped[index] and share > MAX_OWNERSHIP:
                excess += share - MAX_OWNERSHIP
                shares[index] = MAX_OWNERSHIP
                capped[index] = True

        if excess <= 1e-9:
            break

        free_weight = sum(
            weight for index, weight in enumerate(weights) if not capped[index]
        )
        if free_weight <= 0:
            break

        for index, weight in enumerate(weights):
            if not capped[index]:
                shares[index] += excess * weight / free_weight

    return shares


def project_ownership(
    pool: Sequence[Mapping[str, Any]],
    roster_size: int,
    *,
    value_sensitivity: float = VALUE_SENSITIVITY,
    salary_sensitivity: float = SALARY_SENSITIVITY,
) -> dict[str, float]:
    """Estimate the percentage of entries that will roster each player.

    Returns percentages summing to `roster_size * 100`, the total
    ownership any contest necessarily distributes across its pool.
    """

    playable = [player for player in pool if float(player.get("projected_points") or 0.0) > 0]
    if not playable:
        return {}

    values = _relative([value_per_thousand(player) for player in playable])
    salaries = _relative([float(player.get("salary") or 0.0) for player in playable])

    # Exponential in the score, which reproduces ownership's actual
    # shape: a long flat tail of near-zero players and a short head
    # rising very steeply.
    weights = [
        math.exp(value_sensitivity * value + salary_sensitivity * salary)
        for value, salary in zip(values, salaries)
    ]

    shares = _distribute(weights, roster_size * 100.0)

    return {
        str(player["player_id"]): round(max(share, MIN_OWNERSHIP), 2)
        for player, share in zip(playable, shares)
    }


def _percentile_ranks(values: Sequence[float]) -> list[float]:
    """Fractional rank in [0, 1], ties sharing the average rank."""

    if not values:
        return []

    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)

    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        average = (position + end) / 2.0
        for index in range(position, end + 1):
            ranks[order[index]] = average / max(len(values) - 1, 1)
        position = end + 1

    return ranks


def apply_leverage(pool: list[dict[str, Any]], roster_size: int) -> list[dict[str, Any]]:
    """Attach ownership, value, and leverage to every player in a pool.

    `leverage` is the projection's percentile rank minus ownership's,
    so it runs from -1 to +1. Positive means the player produces more
    than the field's interest in him implies -- the tournament play.
    Negative is chalk: fine in cash games, expensive in tournaments.

    Ownership already supplied by a real data source is left alone;
    only missing values are modelled.
    """

    modelled = project_ownership(pool, roster_size)

    for player in pool:
        key = str(player["player_id"])
        if player.get("projected_ownership") in (None, ""):
            player["projected_ownership"] = modelled.get(key, MIN_OWNERSHIP)
        player["value"] = round(value_per_thousand(player), 3)

    projections = [float(player.get("projected_points") or 0.0) for player in pool]
    ownerships = [float(player.get("projected_ownership") or 0.0) for player in pool]

    projection_ranks = _percentile_ranks(projections)
    ownership_ranks = _percentile_ranks(ownerships)

    for player, projection_rank, ownership_rank in zip(pool, projection_ranks, ownership_ranks):
        player["leverage"] = round(projection_rank - ownership_rank, 4)

    return pool


def gpp_score(
    player: Mapping[str, Any],
    ceiling_weight: float = 0.65,
    ownership_penalty: float = 0.12,
) -> float:
    """The blended objective a tournament lineup actually maximises.

    Exposed separately from the optimizer so a research board can rank
    the pool by the same number the solver optimises. A player who looks
    mediocre by projection and excellent by this is precisely the kind
    the ranking exists to surface.
    """

    projection = float(player.get("projected_points") or 0.0)
    ceiling = float(player.get("ceiling") or projection)
    ownership = float(player.get("projected_ownership") or 0.0)

    blended = (1.0 - ceiling_weight) * projection + ceiling_weight * ceiling
    return round(blended - ownership_penalty * ownership, 3)


def leverage_board(pool: Sequence[Mapping[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    """The most leveraged plays on a slate, best first."""

    rows = [
        {
            "player_id": player["player_id"],
            "name": player.get("name"),
            "team": player.get("team"),
            "salary": player.get("salary"),
            "projection": player.get("projected_points"),
            "ceiling": player.get("ceiling"),
            "ownership": player.get("projected_ownership"),
            "value": player.get("value"),
            "leverage": player.get("leverage", 0.0),
            "gpp_score": gpp_score(player),
        }
        for player in pool
        if float(player.get("projected_points") or 0.0) > 0
    ]

    return sorted(rows, key=lambda row: -row["leverage"])[:limit]
