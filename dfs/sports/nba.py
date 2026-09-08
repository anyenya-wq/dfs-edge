"""NBA roster and scoring rules.

Basketball is the sport where projection quality dominates everything
else, because minutes are both the largest driver of fantasy points and
the thing that changes late. A starter ruled out an hour before lock
hands twenty-five minutes to a backup whose salary has not moved, and
that repricing is the single most reliable edge in the sport. The
projection engine therefore treats minutes as a separate forecast from
per-minute production, and the research layer exists mostly to catch
these late changes.

The two sites diverge on defensive stats and double-doubles: FanDuel
pays three points for a steal or block where DraftKings pays two, and
DraftKings alone pays a double-double bonus. That pushes DraftKings
toward high-usage big men and FanDuel toward defensive specialists.

Roster structure differs more than in any other sport. DraftKings has
three flexible slots (G, F, UTIL); FanDuel has none, requiring two of
each guard and forward position and exactly one center.
"""

from __future__ import annotations

from dfs.sports.base import Bonus, RosterSlot, SportConfig, StackRule

# Basketball correlation is weak and partly negative -- teammates
# compete for the same shots -- so the useful shape is a game stack on
# a high-total game rather than a team stack. Pace lifts both sides.
_STACKS = (
    StackRule(
        name="game_stack",
        anchor="ANY",
        partners=("PG", "SG", "SF", "PF", "C"),
        min_partners=1,
        bring_back=1,
        description="Players from both sides of a high-pace, high-total game.",
    ),
)

_DK_SCORING = {
    "pts": 1.0,
    "fg3m": 0.5,
    "reb": 1.25,
    "ast": 1.5,
    "stl": 2.0,
    "blk": 2.0,
    "tov": -0.5,
}

_FD_SCORING = {
    "pts": 1.0,
    "reb": 1.2,
    "ast": 1.5,
    "stl": 3.0,
    "blk": 3.0,
    "tov": -1.0,
}

DK_NBA = SportConfig(
    sport="NBA",
    site="DK",
    salary_cap=50_000,
    roster=(
        RosterSlot("PG", ("PG",)),
        RosterSlot("SG", ("SG",)),
        RosterSlot("SF", ("SF",)),
        RosterSlot("PF", ("PF",)),
        RosterSlot("C", ("C",)),
        RosterSlot("G", ("PG", "SG")),
        RosterSlot("F", ("SF", "PF")),
        RosterSlot("UTIL", ("PG", "SG", "SF", "PF", "C")),
    ),
    scoring=_DK_SCORING,
    # Double-double and triple-double bonuses are combination rules over
    # several stats at once, which the flat Bonus type cannot express.
    # They are applied in `dfs/sports/bonuses.py` instead.
    bonuses=(),
    opportunity_stat="minutes",
    max_per_team=None,
    min_games=2,
    stack_shapes=_STACKS,
)

FD_NBA = SportConfig(
    sport="NBA",
    site="FD",
    salary_cap=60_000,
    roster=(
        RosterSlot("PG", ("PG",), count=2),
        RosterSlot("SG", ("SG",), count=2),
        RosterSlot("SF", ("SF",), count=2),
        RosterSlot("PF", ("PF",), count=2),
        RosterSlot("C", ("C",)),
    ),
    scoring=_FD_SCORING,
    bonuses=(),
    opportunity_stat="minutes",
    max_per_team=4,
    min_games=2,
    stack_shapes=_STACKS,
)
