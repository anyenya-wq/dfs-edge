"""Bonuses that read several stats at once.

`Bonus` in `base.py` is a threshold on a single stat, which covers
football's yardage bonuses and hockey's shot and block bonuses. It
cannot express "a double-double", which asks whether two *different*
categories each cleared ten. Those live here as functions instead.
"""

from __future__ import annotations

from collections.abc import Mapping

# The categories DraftKings counts toward a double-double. Field goals
# and free throws are deliberately excluded -- only these five count.
_NBA_DOUBLE_CATEGORIES = ("pts", "reb", "ast", "stl", "blk")


def nba_double_bonus(stats: Mapping[str, float], site: str) -> float:
    """DraftKings double-double and triple-double bonuses.

    FanDuel pays neither, so this returns zero there. The bonuses do not
    stack: a triple-double is worth three points total, not three plus
    the double-double's one and a half.
    """

    if site.upper() != "DK":
        return 0.0

    doubles = sum(1 for stat in _NBA_DOUBLE_CATEGORIES if float(stats.get(stat, 0.0)) >= 10)

    if doubles >= 3:
        return 3.0
    if doubles == 2:
        return 1.5
    return 0.0


def apply_combination_bonuses(
    points: float,
    stats: Mapping[str, float],
    sport: str,
    site: str,
) -> float:
    """Add any multi-stat bonuses the sport defines to a base score."""

    if sport.upper() == "NBA":
        return points + nba_double_bonus(stats, site)
    return points
