"""NHL roster and scoring rules.

Hockey correlates by line, not by team. The three forwards on a scoring
line are on the ice together for nearly every shift, so a goal usually
pays two or three of them at once through the primary and secondary
assist. A first-line stack plus the defenceman quarterbacking that
team's power play is the standard tournament shape, and it depends on
line data that neither site provides -- confirmed lines come out shortly
before puck drop, which is the operational challenge of the sport.

Goalies score on a separate table and are strongly negatively correlated
with the opposing skaters, so the optimizer blocks a goalie stacked
against its own skaters. Goalie confirmation is the other timing
problem: an unconfirmed starter is worth nothing at all.
"""

from __future__ import annotations

from dfs.sports.base import Bonus, RosterSlot, SportConfig, StackRule

_STACKS = (
    StackRule(
        name="line_stack",
        anchor="ANY_SKATER",
        partners=("C", "W", "LW", "RW"),
        min_partners=2,
        description="Three forwards from one scoring line.",
    ),
    StackRule(
        name="line_stack_with_dman",
        anchor="ANY_SKATER",
        partners=("C", "W", "LW", "RW", "D"),
        min_partners=3,
        description="A full line plus the power-play defenceman.",
    ),
)

_DK_SKATER = {
    "goal": 8.5,
    "assist": 5.0,
    "sog": 1.5,
    "blocked_shot": 1.3,
    "short_handed_point": 2.0,
    "shootout_goal": 1.5,
}

_DK_GOALIE = {
    "win": 6.0,
    "save": 0.7,
    "goal_against": -3.5,
    "shutout": 4.0,
    "ot_loss": 0.2,
}

_FD_SKATER = {
    "goal": 12.0,
    "assist": 8.0,
    "sog": 1.6,
    "blocked_shot": 1.6,
    "short_handed_point": 2.0,
}

_FD_GOALIE = {
    "win": 6.0,
    "save": 0.6,
    "goal_against": -3.0,
}

DK_NHL = SportConfig(
    sport="NHL",
    site="DK",
    salary_cap=50_000,
    roster=(
        RosterSlot("C", ("C",), count=2),
        RosterSlot("W", ("W", "LW", "RW"), count=3),
        RosterSlot("D", ("D",), count=2),
        RosterSlot("G", ("G",)),
        RosterSlot("UTIL", ("C", "W", "LW", "RW", "D")),
    ),
    scoring=_DK_SKATER,
    # DraftKings pays these step bonuses to skaters. As with football's
    # yardage bonuses they barely move a mean and materially raise a
    # ceiling, which is where tournament lineups are decided.
    bonuses=(
        Bonus("goal", 3, 3.0),
        Bonus("sog", 5, 3.0),
        Bonus("blocked_shot", 3, 3.0),
    ),
    opportunity_stat="toi",
    max_per_team=None,
    min_games=2,
    stack_shapes=_STACKS,
    alt_scoring_positions=("G",),
    alt_scoring=_DK_GOALIE,
)

FD_NHL = SportConfig(
    sport="NHL",
    site="FD",
    salary_cap=55_000,
    roster=(
        RosterSlot("C", ("C",), count=2),
        RosterSlot("W", ("W", "LW", "RW"), count=4),
        RosterSlot("D", ("D",), count=2),
        RosterSlot("G", ("G",)),
    ),
    scoring=_FD_SKATER,
    bonuses=(),
    opportunity_stat="toi",
    max_per_team=4,
    min_games=2,
    stack_shapes=_STACKS,
    alt_scoring_positions=("G",),
    alt_scoring=_FD_GOALIE,
)
