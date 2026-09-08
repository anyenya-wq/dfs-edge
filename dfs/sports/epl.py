"""EPL soccer roster and scoring rules.

Soccer is the hardest of the five to project and the one where these
transcribed tables are least certain -- treat every number here as
unverified until checked against the live rules page. Both sites have
revised soccer scoring more often than their major-sport tables, and
FanDuel has withdrawn soccer contests from some markets entirely, so
confirm the contest exists before building against it.

The sport's difficulty is structural, not incidental. Scoring is rare
and lumpy: a striker who takes six shots and scores none returns almost
nothing, and there is no equivalent of the steady counting-stat floor a
basketball player provides. Both sites compensate by paying for
peripheral actions -- crosses, chances created, tackles, saves -- and
those peripherals are where a defender's or midfielder's floor actually
comes from.

That makes rotation risk the dominant concern. A midfielder rested for a
midweek European fixture scores zero, and confirmed lineups arrive about
an hour before kickoff. As in NBA, the research layer earns its keep by
catching that window rather than by improving a season-long model.
"""

from __future__ import annotations

from dfs.sports.base import RosterSlot, SportConfig, StackRule

# Attacking returns concentrate in the same team-match: the player who
# crosses and the player who heads it in are both paid, and a clean
# sheet pays every defender plus the keeper at once. Those are the two
# shapes worth enforcing.
_STACKS = (
    StackRule(
        name="attack_stack",
        anchor="F",
        partners=("F", "M"),
        min_partners=1,
        description="Striker plus a creative midfielder from the same side.",
    ),
    StackRule(
        name="defence_stack",
        anchor="GK",
        partners=("D",),
        min_partners=2,
        description="Keeper and two defenders for a correlated clean sheet.",
    ),
)

# UNVERIFIED. Core attacking and disciplinary values are transcribed
# with reasonable confidence; the peripheral rates (crosses, chances
# created, tackles) are the ones most likely to be stale.
_DK_SCORING = {
    "goal": 10.0,
    "assist": 6.0,
    "shot_on_goal": 1.0,
    "created_chance": 1.0,
    "cross": 0.7,
    "tackle_won": 0.7,
    "interception": 0.7,
    "clean_sheet": 5.0,
    "goal_allowed": -1.0,
    "yellow_card": -1.0,
    "red_card": -5.0,
    "own_goal": -5.0,
    "penalty_miss": -5.0,
}

# UNVERIFIED, and lower confidence than the DraftKings table above.
_DK_KEEPER = {
    "goal": 10.0,
    "assist": 6.0,
    "save": 0.5,
    "penalty_save": 5.0,
    "clean_sheet": 5.0,
    "goal_allowed": -1.0,
    "yellow_card": -1.0,
    "red_card": -5.0,
    "own_goal": -5.0,
}

_FD_SCORING = {
    "goal": 12.0,
    "assist": 9.0,
    "shot_on_goal": 1.2,
    "created_chance": 1.5,
    "cross": 0.7,
    "tackle_won": 1.0,
    "interception": 0.5,
    "clean_sheet": 6.0,
    "goal_allowed": -1.5,
    "yellow_card": -2.0,
    "red_card": -6.0,
    "own_goal": -6.0,
}

_FD_KEEPER = {
    "goal": 12.0,
    "assist": 9.0,
    "save": 1.0,
    "penalty_save": 8.0,
    "clean_sheet": 6.0,
    "goal_allowed": -1.5,
    "yellow_card": -2.0,
    "red_card": -6.0,
}

DK_EPL = SportConfig(
    sport="EPL",
    site="DK",
    salary_cap=50_000,
    roster=(
        RosterSlot("F", ("F", "FW", "ST"), count=2),
        RosterSlot("M", ("M", "MF"), count=2),
        RosterSlot("D", ("D", "DF"), count=2),
        RosterSlot("GK", ("GK", "G")),
        RosterSlot("UTIL", ("F", "FW", "ST", "M", "MF", "D", "DF")),
    ),
    scoring=_DK_SCORING,
    # Minutes are the opportunity stat for the same reason as basketball,
    # but the risk is discrete rather than continuous: a rotated player
    # plays zero, not fewer.
    opportunity_stat="minutes",
    max_per_team=4,
    min_games=2,
    stack_shapes=_STACKS,
    alt_scoring_positions=("GK", "G"),
    alt_scoring=_DK_KEEPER,
)

FD_EPL = SportConfig(
    sport="EPL",
    site="FD",
    salary_cap=60_000,
    roster=(
        RosterSlot("F", ("F", "FW", "ST"), count=2),
        RosterSlot("M", ("M", "MF"), count=3),
        RosterSlot("D", ("D", "DF"), count=3),
        RosterSlot("GK", ("GK", "G")),
    ),
    scoring=_FD_SCORING,
    opportunity_stat="minutes",
    max_per_team=4,
    min_games=2,
    stack_shapes=_STACKS,
    alt_scoring_positions=("GK", "G"),
    alt_scoring=_FD_KEEPER,
)
