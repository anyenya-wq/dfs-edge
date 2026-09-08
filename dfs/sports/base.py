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
from collections.abc import Mapping, Sequence


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
    min_games: int = 2
    stack_shapes: tuple[StackRule, ...] = ()
    # Positions whose scoring table differs from the rest of the sport
    # (pitchers, goalies). Scored from `alt_scoring` when present.
    alt_scoring_positions: tuple[str, ...] = ()
    alt_scoring: Mapping[str, float] = dataclasses.field(default_factory=dict)
    # False until a human has checked these numbers against the site's
    # published rules for the current season. Surfaced in the UI.
    rules_verified: bool = False

    @property
    def roster_size(self) -> int:
        return sum(slot.count for slot in self.roster)

    @property
    def key(self) -> str:
        return f"{self.sport}:{self.site}"

    def scoring_table(self, positions: Sequence[str]) -> Mapping[str, float]:
        """The scoring table that applies to a player at `positions`."""

        if any(position in self.alt_scoring_positions for position in positions):
            return self.alt_scoring
        return self.scoring

    def score_stat_line(self, stats: Mapping[str, float], positions: Sequence[str]) -> float:
        """Fantasy points for a completed stat line.

        Used to turn historical box scores into the training signal for
        projections, and to resolve projections against actuals. Unknown
        stat keys are ignored rather than raising, because box-score
        feeds carry many fields no scoring system uses.
        """

        table = self.scoring_table(positions)
        total = sum(float(value) * table[stat] for stat, value in stats.items() if stat in table)

        for bonus in self.bonuses:
            if float(stats.get(bonus.stat, 0.0)) >= bonus.threshold:
                total += bonus.points

        return round(total, 4)
