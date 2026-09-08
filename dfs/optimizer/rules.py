"""Constraint settings for lineup construction.

Separated from the solver so that a contest strategy is a data object
you can store, compare, and reuse rather than a pile of keyword
arguments at a call site.
"""

from __future__ import annotations

import dataclasses

from dfs.sports import SportConfig


@dataclasses.dataclass
class StackRequirement:
    """A correlation constraint the solver must satisfy.

    Two shapes cover every sport:

    `anchor` -- whenever a player at an anchor position is rostered, a
    minimum number of partners from his own team must join him. This is
    football's quarterback stack: it does not require that any
    quarterback be rostered, only that whichever one is brings a
    receiver.

    `team` -- some number of teams must each contribute at least `count`
    players. This is baseball's batting-order stack and hockey's line
    stack, where the correlation belongs to the team rather than to any
    one anchoring player.

    `bring_back` adds players from the opposing side of the same game,
    which is how you express a shootout: both offenses producing is what
    makes the whole stack pay at once.
    """

    kind: str = "anchor"
    anchor_positions: tuple[str, ...] = ()
    partner_positions: tuple[str, ...] = ()
    count: int = 1
    bring_back: int = 0
    teams: int = 1
    label: str = ""


@dataclasses.dataclass
class OptimizerSettings:
    """Everything that shapes a lineup other than the projections."""

    mode: str = "cash"

    max_salary: int | None = None
    # Cash lineups should spend nearly the whole cap; leaving two
    # thousand dollars unspent is leaving projected points on the table.
    # Tournaments can justify it when the savings buy a leverage play.
    min_salary: int | None = None

    max_per_team: int | None = None
    min_games: int | None = None

    locks: frozenset[str] = frozenset()
    bans: frozenset[str] = frozenset()

    # Across a multi-lineup build, no player may appear in more than
    # this share of entries. The main defence against building twenty
    # lineups that are really one lineup with substitutions.
    max_exposure: float = 1.0
    # Minimum roster spots by which each lineup must differ from every
    # earlier one.
    min_unique: int = 1

    # 0.0 maximises the mean projection, 1.0 maximises the ceiling.
    # Cash games want the mean, tournaments want something well above 0.
    ceiling_weight: float = 0.0
    # Points deducted per percentage point of projected ownership. The
    # lever that turns a chalk-seeking optimizer into a contrarian one.
    ownership_penalty: float = 0.0
    # Jitter applied to each player's score, as a fraction. Produces
    # genuinely varied lineups instead of near-duplicates clustered on
    # the same optimum.
    randomness: float = 0.0
    random_seed: int | None = None

    stacks: tuple[StackRequirement, ...] = ()

    # A defence scores by stopping an offense, so rostering your own
    # quarterback's opponent defence is a direct internal contradiction.
    no_opposing_defense: bool = True
    # Same logic in baseball: your pitcher wins by retiring the hitters
    # you would be rostering against him.
    no_hitters_vs_own_pitcher: bool = True

    def resolved_cap(self, config: SportConfig) -> int:
        return self.max_salary if self.max_salary is not None else config.salary_cap

    def resolved_max_per_team(self, config: SportConfig) -> int | None:
        return self.max_per_team if self.max_per_team is not None else config.max_per_team

    def resolved_min_games(self, config: SportConfig) -> int:
        return self.min_games if self.min_games is not None else config.min_games


def cash_settings(config: SportConfig) -> OptimizerSettings:
    """A sensible cash-game default: maximise the mean, spend the cap.

    Cash games pay a flat amount to roughly the top half of the field,
    so the goal is to beat a median entry, not to win. Ceilings and
    ownership are irrelevant to that -- what matters is the highest
    expected total with the least variance, which means floors and full
    cap usage. No stacking either: correlation raises variance, which is
    the opposite of what a cash lineup wants.
    """

    return OptimizerSettings(
        mode="cash",
        min_salary=int(config.salary_cap * 0.98),
        ceiling_weight=0.0,
        ownership_penalty=0.0,
        max_exposure=1.0,
        stacks=(),
    )


def gpp_settings(config: SportConfig, lineups: int = 20) -> OptimizerSettings:
    """A tournament default: chase ceilings, fade chalk, stack.

    Tournaments pay steeply at the very top, so the objective is not the
    most likely score but the best chance at an extreme one. That flips
    all three levers: weight the ceiling, penalise ownership (a great
    lineup everyone else also entered is worth much less than a good
    lineup nobody has), and correlate through stacks so the outcomes
    that go right go right together.

    Exposure is capped at 40% and randomness added, because entering
    twenty near-identical lineups gives you one bet at twenty times the
    stake rather than twenty bets.
    """

    stacks = tuple(_stack_from_shape(shape) for shape in config.stack_shapes[:1])

    return OptimizerSettings(
        mode="gpp",
        min_salary=int(config.salary_cap * 0.93),
        ceiling_weight=0.65,
        ownership_penalty=0.12,
        max_exposure=0.4 if lineups > 3 else 1.0,
        min_unique=2,
        randomness=0.06,
        stacks=stacks,
    )


def _stack_from_shape(shape) -> StackRequirement:
    """Translate a sport's declared correlation shape into a constraint."""

    if shape.anchor.startswith("ANY"):
        return StackRequirement(
            kind="team",
            partner_positions=shape.partners,
            count=shape.min_partners + 1,
            bring_back=shape.bring_back,
            teams=1,
            label=shape.name,
        )

    return StackRequirement(
        kind="anchor",
        anchor_positions=(shape.anchor,),
        partner_positions=shape.partners,
        count=shape.min_partners,
        bring_back=shape.bring_back,
        label=shape.name,
    )
