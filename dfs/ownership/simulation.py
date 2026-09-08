"""Monte Carlo optimal rates, and leverage measured against them.

The rank-based leverage in `leverage.py` is a cheap proxy with a known
flaw: it compares a player's projection rank to his ownership rank, and
projection rank ignores salary. An expensive player with a mediocre
projection therefore scores as "leveraged" when he is simply a bad play
nobody wants for good reason.

The metric that does not have this problem is *optimal rate* -- how
often a player appears in the best possible lineup, across many
simulated versions of the slate. It has no salary artifact because it
is measured by actually building lineups, so a player only shows up when
he earns a roster spot at his price under some plausible outcome.

The simulation samples each player's score from a distribution centred
on his projection with his own estimated spread, then solves for the
optimal lineup of that sampled world, and counts appearances over many
such worlds. A player who is optimal in 30% of simulations but owned by
5% of the field is the tournament play the whole exercise is looking
for; the reverse is chalk you can profitably fade.

This costs one solve per iteration, so a few hundred iterations is
seconds rather than milliseconds. Worth it before a tournament, too
slow to sit inside an interactive loop -- which is why the cheap proxy
still exists.
"""

from __future__ import annotations

import copy
import random
from collections.abc import Mapping, Sequence
from typing import Any

from dfs.optimizer.lineup import InfeasibleLineup, optimize_lineup
from dfs.optimizer.rules import OptimizerSettings
from dfs.sports import SportConfig

DEFAULT_ITERATIONS = 200


def _sample_pool(
    pool: Sequence[Mapping[str, Any]],
    generator: random.Random,
) -> list[dict[str, Any]]:
    """One simulated version of the slate.

    Each player's score is drawn from a normal centred on his projection
    with his own standard deviation, falling back to a 30% coefficient
    of variation when no spread was estimated. Draws are clipped at zero
    -- a player cannot score negative fantasy points in most formats,
    and letting him would distort the optimizer's choices.
    """

    sampled = []
    for player in pool:
        projection = float(player.get("projected_points") or 0.0)
        if projection <= 0:
            continue

        stdev = float(player.get("stdev") or 0.0) or projection * 0.30
        draw = max(generator.gauss(projection, stdev), 0.0)

        record = dict(player)
        record["projected_points"] = draw
        # The optimizer blends toward the ceiling in GPP mode; in a
        # sampled world the draw *is* the outcome, so ceiling must
        # follow it rather than stay at its unsampled value.
        record["ceiling"] = draw
        sampled.append(record)

    return sampled


def simulate_optimal_rates(
    pool: Sequence[Mapping[str, Any]],
    config: SportConfig,
    settings: OptimizerSettings | None = None,
    iterations: int = DEFAULT_ITERATIONS,
    seed: int | None = 17,
) -> dict[str, float]:
    """Fraction of simulated slates in which each player is optimal.

    Ownership penalties and randomness are stripped from the settings
    used here: the question is which lineup is best in a given world,
    not which is best after adjusting for what the field will do.
    """

    base = copy.deepcopy(settings) if settings else OptimizerSettings()
    base.ownership_penalty = 0.0
    base.randomness = 0.0
    base.ceiling_weight = 0.0
    base.max_exposure = 1.0

    generator = random.Random(seed)
    appearances: dict[str, int] = {}
    solved = 0

    for _ in range(iterations):
        try:
            lineup = optimize_lineup(_sample_pool(pool, generator), config, base)
        except InfeasibleLineup:
            continue

        solved += 1
        for player_id in lineup.player_ids():
            appearances[player_id] = appearances.get(player_id, 0) + 1

    if not solved:
        return {}

    return {
        player_id: round(100.0 * count / solved, 2)
        for player_id, count in appearances.items()
    }


def apply_simulated_leverage(
    pool: list[dict[str, Any]],
    config: SportConfig,
    settings: OptimizerSettings | None = None,
    iterations: int = DEFAULT_ITERATIONS,
    seed: int | None = 17,
) -> list[dict[str, Any]]:
    """Attach optimal rate and simulation-based leverage to a pool.

    `optimal_rate` is a percentage of simulated slates; ownership is a
    percentage of the field. Both are on the same scale, so their
    difference is directly meaningful: +20 means the player deserves a
    roster spot twenty points more often than the field will give him
    one.

    Assumes ownership has already been attached, by `apply_leverage` or
    from a real data source.
    """

    rates = simulate_optimal_rates(pool, config, settings, iterations, seed)

    for player in pool:
        player_id = str(player["player_id"])
        rate = rates.get(player_id, 0.0)
        ownership = float(player.get("projected_ownership") or 0.0)
        player["optimal_rate"] = rate
        player["sim_leverage"] = round(rate - ownership, 2)

    return pool


def simulated_leverage_board(
    pool: Sequence[Mapping[str, Any]],
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Plays whose optimal rate most exceeds their projected ownership."""

    rows = [
        {
            "player_id": player["player_id"],
            "name": player.get("name"),
            "team": player.get("team"),
            "salary": player.get("salary"),
            "projection": player.get("projected_points"),
            "ceiling": player.get("ceiling"),
            "ownership": player.get("projected_ownership"),
            "optimal_rate": player.get("optimal_rate", 0.0),
            "leverage": player.get("sim_leverage", 0.0),
        }
        for player in pool
        if player.get("optimal_rate") is not None
    ]

    return sorted(rows, key=lambda row: -row["leverage"])[:limit]
