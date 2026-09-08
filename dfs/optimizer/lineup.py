"""Integer-programming lineup construction.

Formulated as an assignment problem rather than a selection problem:
the variables are (player, slot) pairs, not players. Choosing players
first and fitting them to slots afterwards is the tempting shortcut and
it is wrong -- with flexible slots, whether a set of players forms a
legal lineup is itself a matching problem, so an optimizer that ignores
slots will happily return nine players who cannot legally be arranged.
Solving the assignment directly makes every returned lineup valid by
construction.

Everything else hangs off the player-used indicator, which is just the
sum of a player's slot variables. Salary, team limits, stacks, and
uniqueness are all linear in that, which keeps the whole model inside
what CBC solves in well under a second for a normal slate.

Multi-lineup generation is sequential rather than an n-best enumeration:
solve, add a constraint forbidding lineups too similar to the one just
found, solve again. Exposure caps tighten as the build proceeds, so a
player who has hit his limit is banned outright from later solves.
"""

from __future__ import annotations

import dataclasses
import random
from collections.abc import Mapping, Sequence
from typing import Any

import pulp

from dfs.optimizer.rules import OptimizerSettings, StackRequirement
from dfs.sports import SportConfig


class InfeasibleLineup(RuntimeError):
    """No legal lineup exists under the current constraints."""


@dataclasses.dataclass
class LineupPlayer:
    player_id: str
    name: str
    slot: str
    positions: tuple[str, ...]
    team: str | None
    opponent: str | None
    salary: int
    projection: float
    ceiling: float
    ownership: float
    # Carried so a built lineup can be exported for upload without
    # going back to the database to look each player up again.
    dk_id: str | None = None
    fd_id: str | None = None


@dataclasses.dataclass
class Lineup:
    players: list[LineupPlayer]
    total_salary: int
    total_projection: float
    total_ceiling: float
    total_ownership: float
    mode: str

    def player_ids(self) -> set[str]:
        return {player.player_id for player in self.players}

    def as_record(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "total_salary": self.total_salary,
            "total_projection": round(self.total_projection, 3),
            "total_ceiling": round(self.total_ceiling, 3),
            "total_ownership": round(self.total_ownership, 3),
            "players": [dataclasses.asdict(player) for player in self.players],
        }


def _eligible_slots(player: Mapping[str, Any], config: SportConfig) -> list[str]:
    """Slot names this player may legally fill.

    Trusts the site's published roster positions when present, since
    they are authoritative, and falls back to matching the player's
    listed positions against each slot's eligible set.
    """

    published = {str(position).upper() for position in player.get("roster_positions") or []}
    slot_names = {slot.name.upper() for slot in config.roster}
    named = published & slot_names

    positions = [str(position).upper() for position in player.get("positions") or []]
    derived = {slot.name for slot in config.roster if slot.accepts(positions)}

    return sorted(named | derived)


def _score(player: Mapping[str, Any], settings: OptimizerSettings, jitter: float) -> float:
    """The value the solver maximises for one player.

    Blends mean and ceiling by `ceiling_weight`, subtracts an ownership
    penalty, then applies multiplicative noise. The noise is what makes
    a twenty-lineup build explore genuinely different territory instead
    of returning the same optimum with the cheapest legal substitutions.
    """

    projection = float(player.get("projected_points") or 0.0)
    ceiling = float(player.get("ceiling") or projection)
    ownership = float(player.get("projected_ownership") or 0.0)

    base = (1.0 - settings.ceiling_weight) * projection + settings.ceiling_weight * ceiling
    base -= settings.ownership_penalty * ownership

    return base * (1.0 + jitter)


def _add_stack_constraints(
    problem: pulp.LpProblem,
    stack: StackRequirement,
    used: Mapping[str, pulp.LpVariable],
    pool: Sequence[Mapping[str, Any]],
    index: int,
) -> None:
    """Encode one correlation requirement as linear constraints."""

    by_team: dict[str, list[Mapping[str, Any]]] = {}
    for player in pool:
        team = (player.get("team") or "").upper()
        if team:
            by_team.setdefault(team, []).append(player)

    def has_position(player: Mapping[str, Any], positions: Sequence[str]) -> bool:
        listed = {str(position).upper() for position in player.get("positions") or []}
        return bool(listed & {position.upper() for position in positions})

    def opponents_of(team: str) -> list[Mapping[str, Any]]:
        return [
            player
            for player in pool
            if (player.get("opponent") or "").upper() == team
        ]

    if stack.kind == "anchor":
        # For every anchor-eligible player: rostering him forces
        # `count` partners from his own team into the lineup. Written as
        # partners - count * anchor >= 0, which is vacuous when the
        # anchor is not selected.
        for player in pool:
            if not has_position(player, stack.anchor_positions):
                continue

            team = (player.get("team") or "").upper()
            anchor_id = str(player["player_id"])
            if not team or anchor_id not in used:
                continue

            partners = [
                used[str(other["player_id"])]
                for other in by_team.get(team, [])
                if str(other["player_id"]) != anchor_id
                and has_position(other, stack.partner_positions)
                and str(other["player_id"]) in used
            ]

            if len(partners) < stack.count:
                # Too few candidates for this anchor to ever satisfy the
                # rule, so forbid him rather than making the model
                # infeasible for the whole slate.
                problem += used[anchor_id] == 0, f"stack{index}_anchor_impossible_{anchor_id}"
                continue

            problem += (
                pulp.lpSum(partners) >= stack.count * used[anchor_id],
                f"stack{index}_partners_{anchor_id}",
            )

            if stack.bring_back:
                opposition = [
                    used[str(other["player_id"])]
                    for other in opponents_of(team)
                    if str(other["player_id"]) in used
                    and has_position(other, stack.partner_positions)
                ]
                if opposition:
                    problem += (
                        pulp.lpSum(opposition) >= stack.bring_back * used[anchor_id],
                        f"stack{index}_bringback_{anchor_id}",
                    )
        return

    # Team stacks: at least `teams` teams must each contribute `count`
    # players. An indicator per team turns "some team is stacked" into
    # something linear.
    stacked = {
        team: pulp.LpVariable(f"stack{index}_team_{team}", cat="Binary")
        for team in by_team
    }

    for team, players in by_team.items():
        members = [
            used[str(player["player_id"])]
            for player in players
            if str(player["player_id"]) in used
            and (not stack.partner_positions or has_position(player, stack.partner_positions))
        ]

        if len(members) < stack.count:
            problem += stacked[team] == 0, f"stack{index}_team_impossible_{team}"
            continue

        problem += (
            pulp.lpSum(members) >= stack.count * stacked[team],
            f"stack{index}_team_min_{team}",
        )

        if stack.bring_back:
            opposition = [
                used[str(player["player_id"])]
                for player in opponents_of(team)
                if str(player["player_id"]) in used
            ]
            if opposition:
                problem += (
                    pulp.lpSum(opposition) >= stack.bring_back * stacked[team],
                    f"stack{index}_team_bringback_{team}",
                )

    if stack.consecutive:
        _require_consecutive_order(problem, stack, index, by_team, used, stacked)

    problem += (
        pulp.lpSum(stacked.values()) >= stack.teams,
        f"stack{index}_team_count",
    )


def _require_consecutive_order(problem, stack, index, by_team, used, stacked) -> None:
    """Make a chosen team's stack a block of the batting order.

    Expressed as windows. For each run of `count` consecutive slots, a
    binary says the stack starts there; choosing a team requires
    choosing one of its windows, and choosing a window requires every
    hitter in it. The order wraps, because it is a cycle -- the 8, 9 and
    1 hitters bat in succession just as 2, 3 and 4 do.

    A team whose lineup is not posted has no order to be consecutive in,
    and is left under the plain "any `count` players" rule instead. The
    alternative is a constraint nothing can satisfy, which would produce
    no lineup rather than a looser one.
    """

    for team, players in by_team.items():
        by_order = {}
        for player in players:
            order = player.get("batting_order")
            player_id = str(player["player_id"])
            if order and player_id in used:
                by_order.setdefault(int(order), []).append(used[player_id])

        # Nothing to enforce: either no posted lineup, or too few of its
        # hitters are in the pool to fill a window.
        if len(by_order) < stack.count:
            continue

        windows = []
        for start in sorted(by_order):
            slots = [((start - 1 + step) % 9) + 1 for step in range(stack.count)]
            if not all(slot in by_order for slot in slots):
                continue

            window = pulp.LpVariable(
                f"stack{index}_window_{team}_{start}", cat="Binary"
            )
            windows.append(window)

            for slot in slots:
                # One variable per slot: a hitter listed at that spot
                # must be rostered if this window is chosen. Several
                # players can share a slot only if the file is odd, so
                # the sum covers that without assuming it.
                problem += (
                    pulp.lpSum(by_order[slot]) >= window,
                    f"stack{index}_window_{team}_{start}_slot_{slot}",
                )

        if not windows:
            problem += stacked[team] == 0, f"stack{index}_no_window_{team}"
            continue

        problem += (
            pulp.lpSum(windows) >= stacked[team],
            f"stack{index}_window_choice_{team}",
        )


def optimize_lineup(
    pool: Sequence[Mapping[str, Any]],
    config: SportConfig,
    settings: OptimizerSettings | None = None,
    *,
    exclude: Sequence[set[str]] = (),
    banned: frozenset[str] = frozenset(),
    jitters: Mapping[str, float] | None = None,
) -> Lineup:
    """Solve for the single best legal lineup.

    `exclude` holds previously built lineups that the new one must
    differ from; `banned` holds players who have hit their exposure cap.
    Both exist so `optimize_lineups` can drive this repeatedly.
    """

    settings = settings or OptimizerSettings()
    jitters = jitters or {}

    candidates = [
        player
        for player in pool
        if float(player.get("projected_points") or 0.0) > 0
        and str(player["player_id"]) not in settings.bans
        and str(player["player_id"]) not in banned
        and _eligible_slots(player, config)
    ]

    if len(candidates) < config.roster_size:
        raise InfeasibleLineup(
            f"Only {len(candidates)} projected players available for a "
            f"{config.roster_size}-slot {config.key} roster."
        )

    problem = pulp.LpProblem("dfs_lineup", pulp.LpMaximize)

    # x[(player, slot)] = this player fills this slot.
    assign: dict[tuple[str, str], pulp.LpVariable] = {}
    for player in candidates:
        player_key = str(player["player_id"])
        for slot in _eligible_slots(player, config):
            assign[(player_key, slot)] = pulp.LpVariable(
                f"x_{player_key}_{slot}".replace(" ", "_"), cat="Binary"
            )

    # used[player] = 1 when the player occupies any slot.
    used: dict[str, pulp.LpVariable] = {}
    for player in candidates:
        player_key = str(player["player_id"])
        slots = [assign[(player_key, slot)] for slot in _eligible_slots(player, config)]
        if not slots:
            continue
        indicator = pulp.LpVariable(f"u_{player_key}".replace(" ", "_"), cat="Binary")
        used[player_key] = indicator
        problem += pulp.lpSum(slots) == indicator, f"link_{player_key}"

    by_id = {str(player["player_id"]): player for player in candidates}

    problem += pulp.lpSum(
        _score(by_id[player_key], settings, jitters.get(player_key, 0.0)) * variable
        for player_key, variable in used.items()
    )

    # Exactly the required number of players in each slot.
    for slot in config.roster:
        occupants = [
            variable for (_, slot_name), variable in assign.items() if slot_name == slot.name
        ]
        if len(occupants) < slot.count:
            raise InfeasibleLineup(
                f"Slot {slot.name} needs {slot.count} players but only "
                f"{len(occupants)} in the pool are eligible."
            )
        problem += pulp.lpSum(occupants) == slot.count, f"slot_{slot.name}"

    cap = settings.resolved_cap(config)
    salary_expression = pulp.lpSum(
        int(by_id[player_key]["salary"]) * variable for player_key, variable in used.items()
    )
    problem += salary_expression <= cap, "salary_cap"

    if settings.min_salary:
        problem += salary_expression >= settings.min_salary, "salary_floor"

    for player_key in settings.locks:
        if player_key in used:
            problem += used[player_key] == 1, f"lock_{player_key}"

    max_per_team = settings.resolved_max_per_team(config)
    teams: dict[str, list[pulp.LpVariable]] = {}
    for player_key, variable in used.items():
        player = by_id[player_key]
        team = (player.get("team") or "").upper()
        # Some sites cap only part of the roster. DraftKings' MLB limit
        # is five hitters from one team, so a pitcher from that team
        # does not count against it.
        if team and config.counts_toward_team_cap(
            [str(position).upper() for position in player.get("positions") or []]
        ):
            teams.setdefault(team, []).append(variable)

    if max_per_team:
        for team, variables in teams.items():
            problem += pulp.lpSum(variables) <= max_per_team, f"team_cap_{team}"

    # Minimum distinct games. An indicator per game, forced on by any
    # rostered player from it, then a floor on the sum.
    min_games = settings.resolved_min_games(config)
    games: dict[str, list[pulp.LpVariable]] = {}
    for player_key, variable in used.items():
        game = by_id[player_key].get("game_id")
        if game:
            games.setdefault(str(game), []).append(variable)

    if min_games > 1 and len(games) >= min_games:
        indicators = {}
        for game, variables in games.items():
            safe = game.replace("@", "_at_")
            indicator = pulp.LpVariable(f"game_{safe}", cat="Binary")
            indicators[game] = indicator
            for variable in variables:
                problem += indicator >= variable, f"game_on_{safe}_{id(variable)}"
        problem += pulp.lpSum(indicators.values()) >= min_games, "min_games"

    if settings.no_opposing_defense:
        _forbid_defense_against_own_offense(problem, used, by_id)

    if settings.no_hitters_vs_own_pitcher and config.sport == "MLB":
        _forbid_hitters_against_own_pitcher(problem, used, by_id)

    for index, stack in enumerate(settings.stacks):
        _add_stack_constraints(problem, stack, used, candidates, index)

    # Force each new lineup away from the ones already built.
    for index, previous in enumerate(exclude):
        overlap = [used[player_key] for player_key in previous if player_key in used]
        if overlap:
            problem += (
                pulp.lpSum(overlap) <= config.roster_size - settings.min_unique,
                f"unique_{index}",
            )

    status = problem.solve(pulp.PULP_CBC_CMD(msg=False))

    if pulp.LpStatus[status] != "Optimal":
        raise InfeasibleLineup(
            f"Solver returned {pulp.LpStatus[status]}. The constraints are "
            "probably contradictory -- check locks, exposure caps, and stack rules."
        )

    selected: list[LineupPlayer] = []
    for (player_key, slot), variable in assign.items():
        if variable.value() and variable.value() > 0.5:
            player = by_id[player_key]
            projection = float(player.get("projected_points") or 0.0)
            selected.append(
                LineupPlayer(
                    player_id=player_key,
                    name=str(player.get("name", player_key)),
                    slot=slot,
                    positions=tuple(player.get("positions") or []),
                    team=(player.get("team") or None),
                    opponent=(player.get("opponent") or None),
                    salary=int(player["salary"]),
                    projection=round(projection, 3),
                    ceiling=round(float(player.get("ceiling") or projection), 3),
                    ownership=round(float(player.get("projected_ownership") or 0.0), 3),
                    dk_id=player.get("dk_id"),
                    fd_id=player.get("fd_id"),
                )
            )

    order = {slot.name: index for index, slot in enumerate(config.roster)}
    selected.sort(key=lambda player: (order.get(player.slot, 99), -player.salary))

    return Lineup(
        players=selected,
        total_salary=sum(player.salary for player in selected),
        total_projection=sum(player.projection for player in selected),
        total_ceiling=sum(player.ceiling for player in selected),
        total_ownership=sum(player.ownership for player in selected),
        mode=settings.mode,
    )


def _forbid_defense_against_own_offense(
    problem: pulp.LpProblem,
    used: Mapping[str, pulp.LpVariable],
    by_id: Mapping[str, Mapping[str, Any]],
) -> None:
    """Block a defence paired with the offense it is playing against.

    Their scoring is directly opposed -- the defence is paid for sacks
    and interceptions, the quarterback punished for them -- so the pair
    guarantees one side of the lineup fails.
    """

    for defense_key, defense in by_id.items():
        positions = {str(position).upper() for position in defense.get("positions") or []}
        if not positions & {"DST", "DEF", "D"}:
            continue

        opponent = (defense.get("opponent") or "").upper()
        if not opponent or defense_key not in used:
            continue

        for other_key, other in by_id.items():
            if other_key == defense_key or other_key not in used:
                continue
            other_positions = {str(position).upper() for position in other.get("positions") or []}
            if (other.get("team") or "").upper() == opponent and other_positions & {
                "QB", "RB", "WR", "TE"
            }:
                problem += (
                    used[defense_key] + used[other_key] <= 1,
                    f"dst_vs_offense_{defense_key}_{other_key}",
                )


def _forbid_hitters_against_own_pitcher(
    problem: pulp.LpProblem,
    used: Mapping[str, pulp.LpVariable],
    by_id: Mapping[str, Mapping[str, Any]],
) -> None:
    """Block hitters facing a pitcher you have rostered."""

    for pitcher_key, pitcher in by_id.items():
        positions = {str(position).upper() for position in pitcher.get("positions") or []}
        if not positions & {"P", "SP", "RP"}:
            continue

        opponent = (pitcher.get("opponent") or "").upper()
        if not opponent or pitcher_key not in used:
            continue

        for hitter_key, hitter in by_id.items():
            if hitter_key == pitcher_key or hitter_key not in used:
                continue
            hitter_positions = {str(position).upper() for position in hitter.get("positions") or []}
            if (hitter.get("team") or "").upper() == opponent and not hitter_positions & {
                "P", "SP", "RP"
            }:
                problem += (
                    used[pitcher_key] + used[hitter_key] <= 1,
                    f"pitcher_vs_hitter_{pitcher_key}_{hitter_key}",
                )


def optimize_lineups(
    pool: Sequence[Mapping[str, Any]],
    config: SportConfig,
    settings: OptimizerSettings | None = None,
    count: int = 1,
) -> list[Lineup]:
    """Build `count` lineups, respecting uniqueness and exposure caps.

    Stops early rather than raising when the constraints run out of
    room: twelve good lineups is a better answer than an exception on
    the thirteenth, and hitting the wall usually means the exposure cap
    and lineup count are in tension, which the caller can see from the
    length of the result.
    """

    settings = settings or OptimizerSettings()
    generator = random.Random(settings.random_seed)

    lineups: list[Lineup] = []
    seen: list[set[str]] = []
    appearances: dict[str, int] = {}

    for _ in range(count):
        banned = {
            player_key
            for player_key, times in appearances.items()
            if times >= max(1, round(settings.max_exposure * count))
            and player_key not in settings.locks
        }

        jitters = (
            {
                str(player["player_id"]): generator.uniform(-settings.randomness, settings.randomness)
                for player in pool
            }
            if settings.randomness
            else {}
        )

        try:
            lineup = optimize_lineup(
                pool,
                config,
                settings,
                exclude=seen,
                banned=frozenset(banned),
                jitters=jitters,
            )
        except InfeasibleLineup:
            break

        lineups.append(lineup)
        seen.append(lineup.player_ids())
        for player_key in lineup.player_ids():
            appearances[player_key] = appearances.get(player_key, 0) + 1

    return lineups


def exposure_report(lineups: Sequence[Lineup]) -> list[dict[str, Any]]:
    """How often each player appears across a build, most-used first."""

    if not lineups:
        return []

    counts: dict[str, dict[str, Any]] = {}
    for lineup in lineups:
        for player in lineup.players:
            record = counts.setdefault(
                player.player_id,
                {"player_id": player.player_id, "name": player.name, "team": player.team, "count": 0},
            )
            record["count"] += 1

    total = len(lineups)
    for record in counts.values():
        record["exposure"] = round(record["count"] / total, 4)

    return sorted(counts.values(), key=lambda record: -record["count"])
