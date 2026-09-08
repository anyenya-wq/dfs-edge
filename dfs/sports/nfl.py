"""NFL roster and scoring rules.

The two sites differ most here on reception scoring: DraftKings pays a
full point per catch, FanDuel a half. That single number reorders the
player pool -- possession receivers and pass-catching backs are worth
roughly a point and a half more on DraftKings than on FanDuel, which is
enough to move them past a boom-bust deep threat at the same salary.
Never carry a projection built for one site over to the other.

DraftKings also pays yardage bonuses that FanDuel does not. They are
step functions at 100 rushing, 100 receiving, and 300 passing yards, so
they contribute little to a mean projection and a great deal to a
ceiling -- which is why they are modelled as bonuses rather than folded
into the linear rates.
"""

from __future__ import annotations

import math

from dfs.sports.base import Bonus, RosterSlot, SportConfig, StackRule, TieredScore

# Football's correlation structure is the strongest in daily fantasy.
# A quarterback cannot throw a touchdown without a receiver catching
# one, so their scores move together; the bring-back captures the
# shootout, where the opposing offense forces the game to stay open.
_STACKS = (
    StackRule(
        name="qb_stack",
        anchor="QB",
        partners=("WR", "TE"),
        min_partners=1,
        description="Pair the quarterback with a pass catcher from his own team.",
    ),
    StackRule(
        name="qb_stack_bring_back",
        anchor="QB",
        partners=("WR", "TE"),
        min_partners=1,
        bring_back=1,
        description="Quarterback, a teammate pass catcher, and one opposing skill player.",
    ),
)

_DK_SCORING = {
    "pass_yd": 0.04,
    "pass_td": 4.0,
    "pass_int": -1.0,
    "rush_yd": 0.1,
    "rush_td": 6.0,
    "rec": 1.0,
    "rec_yd": 0.1,
    "rec_td": 6.0,
    "fumble_lost": -1.0,
    "two_point_conv": 2.0,
    "return_td": 6.0,
    "fumble_recovery_td": 6.0,
}

_FD_SCORING = {
    "pass_yd": 0.04,
    "pass_td": 4.0,
    "pass_int": -1.0,
    "rush_yd": 0.1,
    "rush_td": 6.0,
    "rec": 0.5,
    "rec_yd": 0.1,
    "rec_td": 6.0,
    "fumble_lost": -2.0,
    "two_point_conv": 2.0,
    "return_td": 6.0,
}

# Defence scores on a different axis from everyone else -- takeaways
# and points prevented rather than yards gained -- so it gets its own
# table rather than sharing keys with the skill positions. Keeping them
# apart also means a stray key in one table cannot silently pay out in
# the other.
#
# Both sites use the same shape here, and very nearly the same numbers.
# The one thing to watch is that a defence's floor is unusually deep:
# the bottom points-allowed band is -4, so a unit facing a good offence
# can post a negative score, which no skill player can do.
_DST_SCORING = {
    "sack": 1.0,
    "def_int": 2.0,
    "fumble_recovery": 2.0,
    "safety": 2.0,
    "def_td": 6.0,
    "return_td": 6.0,
    "blocked_kick": 2.0,
}

# Points allowed, banded. Read as "up to this many points, this many
# fantasy points": a shutout pays 10, and 35 or more costs 4.
_POINTS_ALLOWED = TieredScore(
    stat="points_allowed",
    bands=(
        (0, 10.0),
        (6, 7.0),
        (13, 4.0),
        (20, 1.0),
        (27, 0.0),
        (34, -1.0),
        (math.inf, -4.0),
    ),
)


DK_NFL = SportConfig(
    sport="NFL",
    site="DK",
    salary_cap=50_000,
    roster=(
        RosterSlot("QB", ("QB",)),
        RosterSlot("RB", ("RB",), count=2),
        RosterSlot("WR", ("WR",), count=3),
        RosterSlot("TE", ("TE",)),
        RosterSlot("FLEX", ("RB", "WR", "TE")),
        RosterSlot("DST", ("DST",)),
    ),
    scoring=_DK_SCORING,
    bonuses=(
        Bonus("pass_yd", 300, 3.0),
        Bonus("rush_yd", 100, 3.0),
        Bonus("rec_yd", 100, 3.0),
    ),
    opportunity_stat="snaps",
    max_per_team=None,
    min_games=2,
    stack_shapes=_STACKS,
    alt_scoring_positions=("DST", "DEF"),
    alt_scoring=_DST_SCORING,
    tiers=(_POINTS_ALLOWED,),
)

FD_NFL = SportConfig(
    sport="NFL",
    site="FD",
    salary_cap=60_000,
    roster=(
        RosterSlot("QB", ("QB",)),
        RosterSlot("RB", ("RB",), count=2),
        RosterSlot("WR", ("WR",), count=3),
        RosterSlot("TE", ("TE",)),
        RosterSlot("FLEX", ("RB", "WR", "TE")),
        RosterSlot("DEF", ("DST", "DEF")),
    ),
    scoring=_FD_SCORING,
    bonuses=(),
    opportunity_stat="snaps",
    # FanDuel caps skill players from one team at four, which rules out
    # the deepest onslaught stacks that DraftKings permits.
    max_per_team=4,
    min_games=2,
    stack_shapes=_STACKS,
    alt_scoring_positions=("DST", "DEF"),
    alt_scoring=_DST_SCORING,
    tiers=(_POINTS_ALLOWED,),
)
