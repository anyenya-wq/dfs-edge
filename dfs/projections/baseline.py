"""Opportunity-based baseline projections.

The model separates two questions that a raw fantasy-points average
conflates: how much will this player play, and how productive is he when
he plays. Keeping them apart is the single most valuable structural
choice available, because the two behave completely differently.
Per-minute production is one of the most stable quantities in sports and
barely moves game to game. Playing time is volatile and is what actually
changes on the day -- an injury ahead of someone on the depth chart
hands them fifteen extra minutes and the site's salary has not moved.

A model built on fantasy-point averages cannot express that. It sees a
player who scored twelve a game and projects twelve, whether tonight he
plays twenty minutes or forty. Splitting the terms lets the news layer
revise the volatile half by itself:

    points = projected_opportunity x per_opportunity_rate x adjustments

Both halves are exponentially weighted, so recent games dominate without
discarding older ones. Recency matters more for the rate than most
people assume -- role changes show up there before they show up in a
box-score average.

Two guards keep the model honest on thin evidence. Rates from few games
are shrunk toward a positional prior, so a player with two big games is
not projected as though those two games were the truth. And the variance
estimate is floored, because a player with a short history looks
deceptively consistent and would otherwise be handed an unearned ceiling
-- which in tournament mode is exactly the error that gets them
rostered.
"""

from __future__ import annotations

import dataclasses
import math
import statistics
from collections.abc import Mapping, Sequence
from typing import Any

from dfs.sports import SportConfig, score_stat_line

# Games until an observation's weight halves. Four is about two weeks of
# an NBA schedule and four weeks of an NFL one, which is roughly the
# horizon over which a role change becomes visible.
DEFAULT_HALF_LIFE = 4.0

# Shrinkage strength. With `k` set to 5, a player with five games of
# history is weighted half on his own rate and half on the positional
# prior. Rookies and post-trade players need this most.
SHRINK_STRENGTH = 5.0

# Floor on the coefficient of variation. Fantasy scoring is genuinely
# noisy -- even a metronomic player swings 25% game to game -- and a
# short history that happens to look tight would otherwise produce a
# ceiling the player cannot actually reach.
MIN_COEFFICIENT_OF_VARIATION = 0.25

# The ceiling is the ~90th percentile outcome and the floor the ~10th.
# Tournaments are won by ceilings, cash games survived on floors, and
# the optimizer switches which one it maximises by contest mode.
CEILING_Z = 1.28
FLOOR_Z = 1.28


@dataclasses.dataclass
class PlayerProjection:
    player_id: str
    name: str
    projected_points: float
    projected_opportunity: float
    per_opportunity_rate: float
    stdev: float
    floor: float
    ceiling: float
    games_used: int
    model: str = "baseline"
    notes: str = ""

    def as_record(self) -> dict[str, Any]:
        return {
            "player_id": self.player_id,
            "projected_points": self.projected_points,
            "projected_opportunity": self.projected_opportunity,
            "stdev": self.stdev,
            "floor": self.floor,
            "ceiling": self.ceiling,
            "model": self.model,
        }


def ewma(values: Sequence[float], half_life: float = DEFAULT_HALF_LIFE) -> float:
    """Exponentially weighted mean, `values` most recent first.

    Weight decays by half every `half_life` observations, so the most
    recent game counts roughly four times a game from three weeks ago.
    """

    if not values:
        return 0.0

    decay = 0.5 ** (1.0 / max(half_life, 0.1))
    weights = [decay**index for index in range(len(values))]
    total_weight = sum(weights)

    if total_weight == 0:
        return 0.0

    return sum(value * weight for value, weight in zip(values, weights)) / total_weight


def weighted_stdev(values: Sequence[float], mean: float, half_life: float = DEFAULT_HALF_LIFE) -> float:
    """Exponentially weighted standard deviation about `mean`."""

    if len(values) < 2:
        return 0.0

    decay = 0.5 ** (1.0 / max(half_life, 0.1))
    weights = [decay**index for index in range(len(values))]
    total_weight = sum(weights)

    variance = sum(
        weight * (value - mean) ** 2 for value, weight in zip(values, weights)
    ) / total_weight

    return math.sqrt(max(variance, 0.0))


def score_logs(logs: Sequence[Mapping[str, Any]], positions: Sequence[str], config: SportConfig) -> list[float]:
    """Fantasy points for each log under this site's scoring rules.

    Scoring is recomputed from the stored stat line rather than read
    from a cached column, so the same history yields DraftKings and
    FanDuel numbers without a second fetch -- and so a corrected scoring
    table applies retroactively to everything already collected.
    """

    return [score_stat_line(dict(log.get("stats", {})), list(positions), config) for log in logs]


def positional_prior(
    pool_rates: Mapping[str, Sequence[float]],
    positions: Sequence[str],
    fallback: float = 0.0,
) -> float:
    """Median per-opportunity rate among players sharing a position.

    The median rather than the mean, because a handful of stars would
    otherwise drag the prior up and systematically over-project every
    thinly-observed replacement player shrunk toward it.
    """

    rates: list[float] = []
    for position in positions:
        rates.extend(pool_rates.get(position, []))

    if not rates:
        return fallback

    return float(statistics.median(rates))


def project_player(
    player: Mapping[str, Any],
    logs: Sequence[Mapping[str, Any]],
    config: SportConfig,
    *,
    pool_rates: Mapping[str, Sequence[float]] | None = None,
    opponent_multiplier: float = 1.0,
    opportunity_override: float | None = None,
    points_multiplier: float = 1.0,
    half_life: float = DEFAULT_HALF_LIFE,
) -> PlayerProjection:
    """Project one player for one slate.

    `opportunity_override` is how the research layer intervenes: when
    news says a starter is out and the backup is taking thirty minutes
    instead of his usual twelve, that number is supplied here and the
    rate is left alone. `points_multiplier` is the blunter instrument
    for a player who is playing but limited.
    """

    positions = list(player.get("positions") or [])
    name = str(player.get("name", player.get("player_id", "unknown")))

    scored = score_logs(logs, positions, config)
    opportunities = [float(log.get("opportunity") or 0.0) for log in logs]

    # Games where the player did not appear carry no information about
    # his rate and would drag it toward zero, so they are dropped from
    # the rate calculation. They are not dropped from the opportunity
    # calculation, where a string of inactives is exactly the signal.
    played = [
        (points, opportunity)
        for points, opportunity in zip(scored, opportunities)
        if opportunity > 0
    ]

    games_used = len(played)
    notes = ""

    if played:
        rates = [points / opportunity for points, opportunity in played]
        observed_rate = ewma(rates, half_life)
    else:
        rates = []
        observed_rate = 0.0

    # Shrink toward the positional prior in proportion to how little we
    # have seen. Full weight on the player's own rate needs many games.
    prior = positional_prior(pool_rates or {}, positions, fallback=observed_rate)
    weight = games_used / (games_used + SHRINK_STRENGTH) if games_used else 0.0
    rate = weight * observed_rate + (1.0 - weight) * prior

    if games_used and weight < 0.5:
        notes = f"Thin history ({games_used} games); shrunk {(1 - weight):.0%} toward positional prior."

    if opportunity_override is not None:
        projected_opportunity = float(opportunity_override)
        notes = (notes + " Opportunity overridden by research.").strip()
    else:
        projected_opportunity = ewma(opportunities, half_life)

    projected = projected_opportunity * rate * opponent_multiplier * points_multiplier

    # Dispersion is measured on realised fantasy points, not on the
    # rate, because that is the quantity a lineup's outcome depends on.
    if len(scored) >= 2:
        mean_points = ewma(scored, half_life)
        stdev = weighted_stdev(scored, mean_points, half_life)
    else:
        stdev = 0.0

    stdev = max(stdev, projected * MIN_COEFFICIENT_OF_VARIATION)

    if not games_used:
        notes = "No usable history; projection rests entirely on the positional prior."

    return PlayerProjection(
        player_id=str(player["player_id"]),
        name=name,
        projected_points=round(projected, 3),
        projected_opportunity=round(projected_opportunity, 3),
        per_opportunity_rate=round(rate, 5),
        stdev=round(stdev, 3),
        floor=round(max(projected - FLOOR_Z * stdev, 0.0), 3),
        ceiling=round(projected + CEILING_Z * stdev, 3),
        games_used=games_used,
        notes=notes.strip(),
    )


def build_pool_rates(
    players: Sequence[Mapping[str, Any]],
    logs_by_player: Mapping[str, Sequence[Mapping[str, Any]]],
    config: SportConfig,
    half_life: float = DEFAULT_HALF_LIFE,
) -> dict[str, list[float]]:
    """Per-opportunity rates grouped by position, for the shrinkage prior.

    Built only from players with enough history to be informative --
    including thin players here would make the prior partly a function
    of the noise it exists to damp.
    """

    rates: dict[str, list[float]] = {}

    for player in players:
        logs = logs_by_player.get(str(player["player_id"]), [])
        positions = list(player.get("positions") or [])
        scored = score_logs(logs, positions, config)
        opportunities = [float(log.get("opportunity") or 0.0) for log in logs]

        played = [
            points / opportunity
            for points, opportunity in zip(scored, opportunities)
            if opportunity > 0
        ]

        if len(played) < 3:
            continue

        rate = ewma(played, half_life)
        for position in positions:
            rates.setdefault(position, []).append(rate)

    return rates


def opponent_multipliers(
    logs: Sequence[Mapping[str, Any]],
    config: SportConfig,
    positions_by_player: Mapping[str, Sequence[str]],
) -> dict[tuple[str, str], float]:
    """How much each opponent inflates or suppresses a position's rate.

    Keyed by (opponent, position). A value of 1.10 means players at that
    position have historically scored ten percent above their own
    per-opportunity baseline against that opponent.

    Deliberately measured on rates rather than on totals. A slow team
    suppresses fantasy points mostly by running fewer possessions, which
    is a pace effect already captured by the opportunity term; counting
    it again here would double-penalise every player facing them.
    """

    by_position: dict[str, list[float]] = {}
    by_opponent: dict[tuple[str, str], list[float]] = {}

    for log in logs:
        opportunity = float(log.get("opportunity") or 0.0)
        if opportunity <= 0:
            continue

        player = str(log.get("player_id", ""))
        positions = list(positions_by_player.get(player, []))
        if not positions:
            continue

        rate = score_stat_line(dict(log.get("stats", {})), positions, config) / opportunity
        opponent = (log.get("opponent") or "").upper()

        for position in positions:
            by_position.setdefault(position, []).append(rate)
            if opponent:
                by_opponent.setdefault((opponent, position), []).append(rate)

    league: dict[str, float] = {
        position: float(statistics.median(rates))
        for position, rates in by_position.items()
        if rates
    }

    multipliers: dict[tuple[str, str], float] = {}
    for (opponent, position), rates in by_opponent.items():
        baseline = league.get(position, 0.0)
        # Fewer than four observations is noise, not a matchup read.
        if baseline <= 0 or len(rates) < 4:
            continue
        multipliers[(opponent, position)] = round(
            float(statistics.median(rates)) / baseline, 4
        )

    return multipliers


def clamp_multiplier(value: float, limit: float = 0.25) -> float:
    """Hold a matchup adjustment inside +/-`limit`.

    Unclamped multipliers built from small samples reach absurd values
    and then dominate the projection they were meant to nudge. A matchup
    is worth a quarter of a player's output at the very most.
    """

    return max(1.0 - limit, min(1.0 + limit, value))
