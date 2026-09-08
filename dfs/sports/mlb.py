"""MLB roster and scoring rules.

Baseball has the highest variance of any daily fantasy sport and the
clearest correlation structure. Hitters in the same lineup bat in
sequence, so a scoring inning pays several of them at once -- a
four-run inning is a single, a walk, a double, and a home run credited
to four different rosterable players. Stacking consecutive batting-order
slots from one team is therefore not a preference but the core strategy,
which is why the stack shapes below are order-aware.

Pitchers score on an entirely different table from hitters and are
negatively correlated with the opposing lineup, so rostering a pitcher
against your own hitting stack is self-defeating. The optimizer enforces
that as a constraint rather than trusting the objective to notice.
"""

from __future__ import annotations

from dfs.sports.base import RosterSlot, SportConfig, StackRule

_STACKS = (
    StackRule(
        name="team_stack_4",
        anchor="ANY_HITTER",
        partners=("C", "1B", "2B", "3B", "SS", "OF"),
        min_partners=3,
        description="Four hitters from one team, ideally consecutive in the order.",
    ),
    StackRule(
        name="team_stack_5",
        anchor="ANY_HITTER",
        partners=("C", "1B", "2B", "3B", "SS", "OF"),
        min_partners=4,
        description="Five-man stack for tournaments; needs a big inning to pay.",
    ),
)

_DK_HITTING = {
    "single": 3.0,
    "double": 5.0,
    "triple": 8.0,
    "hr": 10.0,
    "rbi": 2.0,
    "run": 2.0,
    "bb": 2.0,
    "hbp": 2.0,
    "sb": 5.0,
}

_DK_PITCHING = {
    "ip": 2.25,
    "k": 2.0,
    "win": 4.0,
    "er": -2.0,
    "hit_allowed": -0.6,
    "bb_allowed": -0.6,
    "hbp_allowed": -0.6,
    "complete_game": 2.5,
    "complete_game_shutout": 2.5,
    "no_hitter": 5.0,
}

_FD_HITTING = {
    "single": 3.0,
    "double": 6.0,
    "triple": 9.0,
    "hr": 12.0,
    "rbi": 3.5,
    "run": 3.2,
    "bb": 3.0,
    "hbp": 3.0,
    "sb": 6.0,
}

_FD_PITCHING = {
    "ip": 3.0,
    "k": 3.0,
    "win": 6.0,
    "er": -3.0,
}

DK_MLB = SportConfig(
    sport="MLB",
    site="DK",
    salary_cap=50_000,
    roster=(
        RosterSlot("P", ("P", "SP", "RP"), count=2),
        RosterSlot("C", ("C",)),
        RosterSlot("1B", ("1B",)),
        RosterSlot("2B", ("2B",)),
        RosterSlot("3B", ("3B",)),
        RosterSlot("SS", ("SS",)),
        RosterSlot("OF", ("OF",), count=3),
    ),
    scoring=_DK_HITTING,
    opportunity_stat="plate_appearances",
    # Five hitters from one team is the documented DraftKings ceiling,
    # which is what makes the five-man stack the maximum legal shape.
    max_per_team=5,
    min_games=2,
    stack_shapes=_STACKS,
    alt_scoring_positions=("P", "SP", "RP"),
    alt_scoring=_DK_PITCHING,
)

FD_MLB = SportConfig(
    sport="MLB",
    site="FD",
    salary_cap=35_000,
    roster=(
        RosterSlot("P", ("P", "SP", "RP")),
        RosterSlot("C/1B", ("C", "1B", "C/1B")),
        RosterSlot("2B", ("2B",)),
        RosterSlot("3B", ("3B",)),
        RosterSlot("SS", ("SS",)),
        RosterSlot("OF", ("OF",), count=3),
        RosterSlot("UTIL", ("C", "1B", "2B", "3B", "SS", "OF", "C/1B")),
    ),
    scoring=_FD_HITTING,
    opportunity_stat="plate_appearances",
    max_per_team=4,
    min_games=2,
    stack_shapes=_STACKS,
    alt_scoring_positions=("P", "SP", "RP"),
    alt_scoring=_FD_PITCHING,
)
