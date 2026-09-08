"""Projecting a team defence, where the matchup is most of the answer.

A defence is not projected the way a player is. Measured over the 2023
and 2024 seasons, a defence's own recent form carries almost no
information about its next game -- projecting from it beats the
season-average baseline by +0.007, which is noise. What does carry
information is the offence it is about to face: how many fantasy points
that offence has conceded to defences so far is worth +0.046 on its own.

Blending the two halves is worth more than either. The weight was chosen
on 2023 and reported on 2024, never the other way round:

    weight on own form   2023    2024 (held out)
                   0.0  +0.018   +0.075
                   0.5  +0.062   +0.088
                   1.0  +0.007   -0.000

Half and half is the peak on the tuning season, and the curve is flat
enough around it that the exact number is not load-bearing. The result
that is load-bearing is the one at the bottom: a defence projected from
its own history alone is worth nothing at all.

Why this is not just an opponent multiplier. `opponent_multipliers`
scales a player's own rate, which assumes the player's rate is the
signal and the opponent is a modifier. For defences that assumption is
backwards, so the matchup enters as a level rather than a multiplier.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from typing import Any

from dfs.sports import SportConfig, score_stat_line

# Chosen on 2023, reported on held-out 2024. See the module docstring.
OWN_FORM_WEIGHT = 0.5

# Below this, an opponent's concession rate is a rumour: two soft games
# would set the level for the season. The league mean stands in instead.
MIN_OPPONENT_GAMES = 4


def concession_levels(
    logs: Sequence[Mapping[str, Any]],
    config: SportConfig,
) -> tuple[dict[str, float], float]:
    """Fantasy points each offence has conceded to defences, and the mean.

    `logs` are defence game logs, each carrying the `opponent` it faced.
    A team's level is the average score defences have posted against it.
    """

    conceded: dict[str, list[float]] = {}
    every: list[float] = []

    for log in logs:
        opponent = log.get("opponent")
        if not opponent:
            continue

        points = score_stat_line(dict(log.get("stats", {})), ["DST"], config)
        conceded.setdefault(str(opponent).upper(), []).append(points)
        every.append(points)

    league = statistics.fmean(every) if every else 0.0

    levels = {
        team: statistics.fmean(scores)
        for team, scores in conceded.items()
        if len(scores) >= MIN_OPPONENT_GAMES
    }

    return levels, league


def blend_with_matchup(
    own_form: float,
    opponent: str | None,
    levels: Mapping[str, float],
    league_mean: float,
    weight: float = OWN_FORM_WEIGHT,
) -> tuple[float, str]:
    """Combine a defence's own projection with its matchup.

    Returns the blended points and a note saying what happened, because
    a number that silently halved toward something invisible is a number
    nobody can check.
    """

    level = levels.get(str(opponent).upper()) if opponent else None

    if level is None:
        # No usable read on the opponent. Falling back to own form is
        # the weak projection, and saying so is better than implying a
        # matchup adjustment that was never made.
        return own_form, "No matchup read on this opponent; own form only."

    blended = weight * own_form + (1.0 - weight) * level
    direction = "soft" if level > league_mean else "tough"

    return blended, (
        f"Blended with the matchup: {opponent} has conceded {level:.1f} to "
        f"defences against a league {league_mean:.1f}, a {direction} draw."
    )
