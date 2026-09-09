"""Soccer roster and scoring rules.

Named EPL throughout, which is narrower than the rules are. Both sites
run one soccer scoring system across every competition they offer --
Premier League, MLS, Champions League -- so these tables are correct for
a UCL or MLS slate too. What is *not* correct for one is the history:
the collector reads a Premier League feed, so a Champions League slate
loads with no game logs and every player falls back to the site's own
average. See the note in the roadmap; the tables here are not the part
that is wrong.

Scoring is read from the sites: DraftKings from its published Classic
Soccer rules, FanDuel from a contest's own Rules & Scoring tab. Three
things about the shape are worth knowing before reading the numbers.

Both sites score by position group, and not in the same way. FanDuel
runs three separate tables -- forwards and midfielders, defenders,
goalkeepers -- where a defender gets a clean sheet and a forward does
not. DraftKings publishes one table for everyone with two lines
restricted inside it (a clean sheet for defenders, interceptions for
anyone but the keeper), which comes to the same three-way split.

The two sites disagree about what soccer *is*. DraftKings pays for
accurate passes at 0.02 each, so a midfielder who completes ninety
passes banks nearly two points before doing anything else, and pays a
flat +1 for a tackle won or a foul drawn. FanDuel pays nothing for
passing and 1.6 for a tackle, an interception, a clearance or a blocked
shot. A DraftKings midfielder has a floor; a FanDuel defender has one.
They are close to different sports and a projection does not carry
across.

Goals are worth 10 on DraftKings and 15 on FanDuel against smaller
peripheral rates, so FanDuel is the more top-heavy of the two -- which
is the opposite of the usual direction and matters for how a tournament
lineup should be built.

That aside, the sport's difficulty is structural. Scoring is rare and
lumpy: a striker who takes six shots and scores none returns almost
nothing, and there is no equivalent of the steady counting-stat floor a
basketball player provides. That makes rotation risk dominant. A
midfielder rested for a midweek European fixture scores zero, and
confirmed lineups arrive about an hour before kickoff. As in NBA, the
research layer earns its keep by catching that window rather than by
improving a season-long model.
"""

from __future__ import annotations

from dfs.sports.base import PositionScoring, RosterSlot, SportConfig, StackRule

# Attacking returns concentrate in the same team-match: the player who
# crosses and the player who heads it in are both paid, and a clean
# sheet pays every defender plus the keeper at once. Those are the two
# shapes worth enforcing.
def _stacks(forward: str, midfield: str, defence: str) -> tuple[StackRule, ...]:
    """The same two shapes, in whichever vocabulary the site uses.

    DraftKings exports F/M/D/GK and FanDuel exports FWD/MID/DEF/GK.
    A stack anchor is matched against the position string as written,
    so an anchor of "F" silently matches nothing in a FanDuel pool --
    no error, just a stacking rule that never fires.
    """

    return (
        StackRule(
            name="attack_stack",
            anchor=forward,
            partners=(forward, midfield),
            min_partners=1,
            description="Striker plus a creative midfielder from the same side.",
        ),
        StackRule(
            name="defence_stack",
            anchor="GK",
            partners=(defence,),
            min_partners=2,
            description="Keeper and two defenders for a correlated clean sheet.",
        ),
    )


_DK_STACKS = _stacks("F", "M", "D")
_FD_STACKS = _stacks("FWD", "MID", "DEF")

# ----------------------------------------------------------------------
# DraftKings
# ----------------------------------------------------------------------
#
# One published table headed "All Players (GK,D,M,F)" with two lines
# restricted inside it, so it is written here as the three tables it
# actually is.
#
# A shot on goal pays twice: DraftKings' own note says it counts as a
# shot as well, so `shot` and `shot_on_goal` both apply and a shot on
# target is worth 2. The collector must therefore report total shots,
# not shots that missed.

_DK_OUTFIELD = {
    "goal": 10.0,
    "assist": 6.0,
    "shot": 1.0,
    "shot_on_goal": 1.0,
    "cross": 0.7,
    # DraftKings calls this an assisted shot: the final pass leading to
    # an attempt on goal. The same event every other feed calls a key
    # pass or a chance created.
    "created_chance": 1.0,
    "accurate_pass": 0.02,
    "fouls_drawn": 1.0,
    "fouls_conceded": -0.5,
    "tackle_won": 1.0,
    "interception": 0.5,
    "yellow_card": -1.5,
    # Two yellows do not add a red on top. The published note is
    # explicit: a player sent off for a second booking loses 3 in
    # total, which is what two yellows already cost.
    "red_card": -3.0,
    "shootout_goal": 1.5,
    "shootout_miss": -1.0,
}

# Defenders, and only defenders, are paid for the clean sheet on the
# outfield table. Sixty minutes and no goals conceded in regular or
# extra time.
_DK_DEFENDER = {**_DK_OUTFIELD, "clean_sheet": 3.0}

# Interceptions are restricted to (D,M,F), so the keeper's table drops
# them; everything else on the outfield table still applies to him.
_DK_KEEPER = {
    **{key: value for key, value in _DK_OUTFIELD.items() if key != "interception"},
    "save": 2.0,
    "goal_allowed": -2.0,
    "clean_sheet": 5.0,
    "win": 5.0,
    # On top of the +2 for the save itself, so a saved penalty is 5.
    "penalty_save": 3.0,
    "shootout_save": 1.5,
}

# ----------------------------------------------------------------------
# FanDuel
# ----------------------------------------------------------------------
#
# Three tables as published, under the headings FWD/MID, DEF and GK.
#
# The keeper table is short because FanDuel's is: clean sheet, goals
# against, saves, saved penalties, wins, and nothing else. No goal, no
# assist, no card. That is the site's omission and not a gap here -- a
# keeper who scores or is booked is scored for neither.

_FD_OUTFIELD = {
    "goal": 15.0,
    "assist": 7.0,
    "shot": 1.0,
    "shot_on_goal": 4.0,
    "cross": 0.5,
    "created_chance": 2.5,
    "blocked_shot": 1.6,
    "clearance": 1.6,
    "interception": 1.6,
    "tackle_won": 1.6,
    "fouls_drawn": 1.0,
    "penalty_miss": -3.0,
    "yellow_card": -1.0,
    "red_card": -3.0,
}

_FD_DEFENDER = {**_FD_OUTFIELD, "clean_sheet": 5.0, "goal_allowed": -0.6}

_FD_KEEPER = {
    "clean_sheet": 8.0,
    "goal_allowed": -2.5,
    "save": 2.5,
    "penalty_save": 2.5,
    "win": 6.0,
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
    scoring=_DK_OUTFIELD,
    # Minutes are the opportunity stat for the same reason as basketball,
    # but the risk is discrete rather than continuous: a rotated player
    # plays zero, not fewer.
    opportunity_stat="minutes",
    # No per-team cap in the published rules -- six of the eight from
    # one side is a legal lineup. What is required is three different
    # teams across at least two matches.
    max_per_team=None,
    min_teams=3,
    min_games=2,
    stack_shapes=_DK_STACKS,
    position_scoring=(
        PositionScoring(("GK", "G"), _DK_KEEPER),
        PositionScoring(("D", "DF", "DEF"), _DK_DEFENDER),
    ),
    rules_verified=True,
)

FD_EPL = SportConfig(
    sport="EPL",
    site="FD",
    # Dollars, not thousands. FanDuel prices soccer from $5 to $23
    # against a $100 cap, where every other sport here is priced in
    # tens of thousands -- so anything that assumes a large cap, a
    # fixture included, has to cope with this one.
    salary_cap=100,
    # Four from a combined forward-and-midfield pool, two defenders, a
    # keeper. Seven, not the nine assumed before, and the combined slot
    # is the same grouping the scoring tables use.
    roster=(
        RosterSlot("FWD/MID", ("FWD", "F", "FW", "ST", "MID", "M", "MF"), count=4),
        RosterSlot("DEF", ("DEF", "D", "DF"), count=2),
        RosterSlot("GK", ("GK", "G")),
    ),
    scoring=_FD_OUTFIELD,
    opportunity_stat="minutes",
    # Still assumed: the entry screen shows the roster and the cap but
    # not the per-team limit.
    max_per_team=4,
    min_games=2,
    stack_shapes=_FD_STACKS,
    position_scoring=(
        PositionScoring(("GK", "G"), _FD_KEEPER),
        PositionScoring(("DEF", "D", "DF"), _FD_DEFENDER),
    ),
    rules_verified=True,
)
