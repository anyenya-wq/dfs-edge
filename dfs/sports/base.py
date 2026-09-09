"""Roster, scoring, and correlation rules for a (sport, site) pair.

The unit of configuration is a *sport-site pair*, not a sport. DraftKings
and FanDuel disagree about nearly everything that matters to an
optimizer: NFL is full PPR on DraftKings and half PPR on FanDuel, the
salary caps differ by ten thousand dollars, and FanDuel NBA has no flex
slots at all where DraftKings has three. A config keyed only by sport
would have to branch on site at every use, so the pair is the key.

Scoring tables live in data, not in code branches, so a rule change is a
one-line edit to a dict rather than a patch to the engine. This matters
more than it looks: both sites revise scoring between seasons, and the
tables in `dfs/sports/*.py` are transcribed from published rules that
were current when written. Verify them against the site's live rules
before trusting a projection built on them -- `SportConfig.rules_verified`
records whether a human has done that, and the Streamlit board surfaces
the answer rather than hiding it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence


@dataclasses.dataclass(frozen=True)
class RosterSlot:
    """One roster requirement, e.g. two slots taking RB, WR, or TE.

    `eligible` is the set of listed positions that may fill the slot. A
    player's own position string may name several ("PG/SG"), so
    eligibility is an intersection test, not equality.
    """

    name: str
    eligible: tuple[str, ...]
    count: int = 1

    def accepts(self, positions: Sequence[str]) -> bool:
        return any(position in self.eligible for position in positions)


@dataclasses.dataclass(frozen=True)
class Bonus:
    """A threshold bonus: `points` once `stat` reaches `threshold`.

    Kept separate from the linear scoring table because these are step
    functions. They matter for ceiling estimates far more than for means
    -- a 100-yard rushing bonus is invisible in an average and decisive
    in the top decile of outcomes.
    """

    stat: str
    threshold: float
    points: float


@dataclasses.dataclass(frozen=True)
class DerivedStat:
    """A stat no feed carries, computed from ones that it does.

    A quality start is not a field in any box score; it is a definition
    applied to innings pitched and earned runs. Both are already stored,
    so the choice is where to apply it.

    Applied here, at scoring time, rather than in the collector. That
    means it holds for history that was already collected -- otherwise
    adding a scoring line would silently mis-score every stored game
    until someone re-downloaded a season, and the projections built on
    that history would be wrong in the meantime with nothing to show
    for it.

    `requires` names the stats that must be present for the rule to
    apply at all, which is what keeps it off the lines it does not
    describe: a hitter has no innings pitched, so a hitter can never
    accidentally record a quality start.
    """

    name: str
    requires: tuple[str, ...]
    rule: "Callable[[Mapping[str, float]], float]"


@dataclasses.dataclass(frozen=True)
class TieredScore:
    """A step function over one stat, e.g. a defence's points allowed.

    Separate from `Bonus` because this is a full partition rather than a
    single threshold: every value falls in exactly one band, and the
    bands run from strongly positive to strongly negative.

    Applied only when `stat` is present in the line, and that condition
    is load-bearing. A wide receiver's box score has no `points_allowed`
    key at all; reading absence as zero would award him the shutout
    bonus, which is both wrong and large.
    """

    stat: str
    # (inclusive upper bound, points), lowest bound first. The final
    # entry should use `math.inf` so every value lands somewhere.
    bands: tuple[tuple[float, float], ...]

    def points_for(self, value: float) -> float:
        for bound, points in self.bands:
            if value <= bound:
                return points
        return self.bands[-1][1]


@dataclasses.dataclass(frozen=True)
class PositionScoring:
    """A scoring table that applies only to players at `positions`.

    Most sports need one exception -- a pitcher, a goalie, a defence --
    and `SportConfig.alt_scoring` says that in one line. Soccer needs
    two: FanDuel scores forwards and midfielders, defenders, and
    goalkeepers from three separate tables, and the difference is not
    cosmetic. A defender's clean sheet is most of a defender's floor,
    and paying it to a forward, or failing to pay it to a defender,
    misprices the position rather than nudging it.
    """

    positions: tuple[str, ...]
    table: Mapping[str, float]


@dataclasses.dataclass(frozen=True)
class StackRule:
    """A correlation shape worth enforcing during lineup construction.

    Correlation is the part of DFS that a naive optimizer gets wrong.
    Maximizing the sum of independent projections implicitly assumes
    outcomes are independent, which is false in exactly the cases that
    win tournaments: a quarterback's touchdown is his receiver's
    touchdown. `min_partners` teammates from `partners` are required
    alongside every rostered `anchor`.

    `bring_back` requires players from the *opposing* team in the same
    game, which captures the shootout scenario -- both offenses score,
    both sides of the stack pay off.
    """

    name: str
    anchor: str
    partners: tuple[str, ...]
    min_partners: int = 1
    bring_back: int = 0
    description: str = ""


@dataclasses.dataclass(frozen=True)
class SportConfig:
    """Everything the optimizer and scorer need for one sport-site pair."""

    sport: str
    site: str
    salary_cap: int
    roster: tuple[RosterSlot, ...]
    scoring: Mapping[str, float]
    bonuses: tuple[Bonus, ...] = ()
    # Opportunity is the stat a projection should be built on top of:
    # minutes for basketball, snaps for football. Per-opportunity rates
    # are far more stable across games than raw fantasy points, so the
    # projection engine splits "how much will they play" from "how well
    # do they play" and this names the former.
    opportunity_stat: str = "minutes"
    # Both sites cap players from a single team and require entries to
    # span multiple games, which prevents a lineup from riding one
    # blowout. None means the site imposes no limit.
    max_per_team: int | None = None
    # Positions that do not count toward `max_per_team`. DraftKings caps
    # *hitters* from one team at five, not players: a five-man stack
    # alongside that same team's pitcher is a legal lineup, and counting
    # the pitcher would rule it out.
    team_cap_excludes: tuple[str, ...] = ()
    min_games: int = 2
    # Distinct teams a lineup must span. Separate from `max_per_team`:
    # DraftKings soccer requires three different teams among eight
    # players but caps none of them, so a six-man side is legal and a
    # two-team lineup is not. A cap cannot express that.
    min_teams: int = 1
    stack_shapes: tuple[StackRule, ...] = ()
    # Positions whose scoring table differs from the rest of the sport
    # (pitchers, goalies). Scored from `alt_scoring` when present. This
    # is shorthand for a single `PositionScoring` entry, and is kept
    # because one exception is the common case; a sport needing more
    # than one uses the general form below instead. Both go through the
    # same lookup, so there is one rule for which table wins.
    alt_scoring_positions: tuple[str, ...] = ()
    alt_scoring: Mapping[str, float] = dataclasses.field(default_factory=dict)
    # Checked in order, first match wins, ahead of the pair above.
    position_scoring: tuple[PositionScoring, ...] = ()
    # Step functions over a single stat, scored on top of the linear
    # table. Only football uses these, for points allowed.
    tiers: tuple[TieredScore, ...] = ()
    # Stats computed from the line rather than read from it.
    derived: tuple[DerivedStat, ...] = ()
    # False until a human has checked these numbers against the site's
    # published rules for the current season. Surfaced in the UI.
    rules_verified: bool = False

    @property
    def roster_size(self) -> int:
        return sum(slot.count for slot in self.roster)

    @property
    def key(self) -> str:
        return f"{self.sport}:{self.site}"

    def counts_toward_team_cap(self, positions: Sequence[str]) -> bool:
        """Whether a player at `positions` counts against the team limit."""

        if not self.team_cap_excludes:
            return True

        return not any(position in self.team_cap_excludes for position in positions)

    @property
    def scoring_tables(self) -> tuple[PositionScoring, ...]:
        """Every positional exception, in the order they are checked."""

        if not self.alt_scoring_positions:
            return self.position_scoring

        return self.position_scoring + (
            PositionScoring(self.alt_scoring_positions, self.alt_scoring),
        )

    def scoring_table(self, positions: Sequence[str]) -> Mapping[str, float]:
        """The scoring table that applies to a player at `positions`."""

        for entry in self.scoring_tables:
            if any(position in entry.positions for position in positions):
                return entry.table
        return self.scoring

    def with_derived(self, stats: Mapping[str, float]) -> Mapping[str, float]:
        """`stats` plus anything the rules derive from it.

        A value already in the line wins: a feed that starts publishing
        the stat directly should not be overruled by a definition.
        """

        if not self.derived:
            return stats

        line = dict(stats)

        for derived in self.derived:
            if derived.name in line:
                continue
            if not all(stat in line for stat in derived.requires):
                continue
            value = derived.rule(line)
            if value:
                line[derived.name] = value

        return line

    def score_stat_line(self, stats: Mapping[str, float], positions: Sequence[str]) -> float:
        """Fantasy points for a completed stat line.

        Used to turn historical box scores into the training signal for
        projections, and to resolve projections against actuals. Unknown
        stat keys are ignored rather than raising, because box-score
        feeds carry many fields no scoring system uses.
        """

        table = self.scoring_table(positions)
        stats = self.with_derived(stats)
        total = sum(float(value) * table[stat] for stat, value in stats.items() if stat in table)

        for bonus in self.bonuses:
            if float(stats.get(bonus.stat, 0.0)) >= bonus.threshold:
                total += bonus.points

        for tier in self.tiers:
            # Presence, not value: a line without the stat is a player
            # the tier does not describe, not one who scored zero on it.
            if tier.stat in stats:
                total += tier.points_for(float(stats[tier.stat]))

        return round(total, 4)
