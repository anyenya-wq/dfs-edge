"""Lineup construction: legality, correlation, and multi-lineup discipline."""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from dfs.ingest.salaries import expand_roster_eligibility
from dfs.optimizer.lineup import (
    _eligible_slots,
    InfeasibleLineup,
    exposure_report,
    optimize_lineup,
    optimize_lineups,
)
from dfs.optimizer.rules import OptimizerSettings, StackRequirement, cash_settings, gpp_settings
from dfs.sports import CONFIGS, get_config
from tests.fixtures import make_pool


def _pool(config, **kwargs):
    return expand_roster_eligibility(make_pool(config, **kwargs), config)


@pytest.mark.parametrize("key", sorted(CONFIGS))
def test_every_sport_site_pair_builds_a_legal_lineup(key):
    config = CONFIGS[key]
    lineup = optimize_lineup(_pool(config), config, cash_settings(config))

    assert len(lineup.players) == config.roster_size
    assert lineup.total_salary <= config.salary_cap


@pytest.mark.parametrize("key", sorted(CONFIGS))
def test_every_slot_is_filled_by_an_eligible_player(key):
    config = CONFIGS[key]
    lineup = optimize_lineup(_pool(config), config, cash_settings(config))

    slots = {slot.name: slot for slot in config.roster}
    counts: dict[str, int] = {}

    for player in lineup.players:
        slot = slots[player.slot]
        assert slot.accepts(list(player.positions)), (
            f"{player.name} {player.positions} cannot fill {slot.name}"
        )
        counts[player.slot] = counts.get(player.slot, 0) + 1

    for slot in config.roster:
        assert counts.get(slot.name, 0) == slot.count


def test_no_player_appears_twice_in_a_lineup():
    config = get_config("NBA", "DK")
    lineup = optimize_lineup(_pool(config), config, cash_settings(config))
    assert len(lineup.player_ids()) == len(lineup.players)


def test_cash_settings_spend_almost_the_whole_cap():
    config = get_config("NBA", "DK")
    lineup = optimize_lineup(_pool(config), config, cash_settings(config))
    assert lineup.total_salary >= config.salary_cap * 0.98


def test_minimum_games_is_respected():
    config = get_config("NBA", "DK")
    lineup = optimize_lineup(_pool(config), config, cash_settings(config))
    games = {player.opponent and tuple(sorted((player.team, player.opponent)))
             for player in lineup.players}
    assert len(games) >= config.min_games


def test_minimum_games_survives_one_game_being_the_whole_slate():
    """The version above passed against a rule that was not enforced.

    Its pool spans four games and no reason to concentrate, so a
    lineup satisfied the floor by accident. Here one game is made
    twenty times better than the rest, which is the only condition
    under which the constraint has to do anything -- and under it, the
    optimizer used to return a single-game lineup that DraftKings
    rejects at upload.
    """

    config = get_config("NFL", "DK")
    pool = _pool(config)
    best = pool[0]["game_id"]
    for player in pool:
        if player["game_id"] == best:
            for key in ("projected_points", "ceiling", "floor"):
                player[key] *= 20

    lineup = optimize_lineup(pool, config, cash_settings(config))
    games = {tuple(sorted((player.team, player.opponent))) for player in lineup.players}

    assert len(games) >= config.min_games == 2


def test_team_cap_is_respected_where_the_site_imposes_one():
    config = get_config("NBA", "FD")
    lineup = optimize_lineup(_pool(config), config, cash_settings(config))
    counts: dict[str, int] = {}
    for player in lineup.players:
        counts[player.team] = counts.get(player.team, 0) + 1
    assert max(counts.values()) <= config.max_per_team


def test_locked_player_is_always_rostered():
    config = get_config("NBA", "DK")
    pool = _pool(config)
    # Someone the optimizer would never take on merit.
    worst = min(pool, key=lambda p: p["projected_points"] / p["salary"])

    settings = cash_settings(config)
    settings.locks = frozenset({worst["player_id"]})

    lineup = optimize_lineup(pool, config, settings)
    assert worst["player_id"] in lineup.player_ids()


def test_banned_player_is_never_rostered():
    config = get_config("NBA", "DK")
    pool = _pool(config)
    best = max(pool, key=lambda p: p["projected_points"])

    settings = cash_settings(config)
    settings.bans = frozenset({best["player_id"]})

    lineup = optimize_lineup(pool, config, settings)
    assert best["player_id"] not in lineup.player_ids()


def test_quarterback_stack_is_enforced():
    config = get_config("NFL", "DK")
    settings = gpp_settings(config, lineups=1)
    settings.max_exposure = 1.0
    settings.stacks = (
        StackRequirement(
            kind="anchor", anchor_positions=("QB",),
            partner_positions=("WR", "TE"), count=2, bring_back=1,
        ),
    )

    lineup = optimize_lineup(_pool(config), config, settings)
    quarterback = next(p for p in lineup.players if "QB" in p.positions)

    teammates = [
        p for p in lineup.players
        if p.team == quarterback.team
        and p.player_id != quarterback.player_id
        and set(p.positions) & {"WR", "TE"}
    ]
    opposition = [p for p in lineup.players if p.team == quarterback.opponent]

    assert len(teammates) >= 2
    assert len(opposition) >= 1


def test_team_stack_is_enforced():
    config = get_config("MLB", "DK")
    settings = gpp_settings(config, lineups=1)
    settings.max_exposure = 1.0
    settings.stacks = (
        StackRequirement(
            kind="team",
            partner_positions=("C", "1B", "2B", "3B", "SS", "OF"),
            count=4, teams=1,
        ),
    )

    lineup = optimize_lineup(_pool(config), config, settings)
    counts: dict[str, int] = {}
    for player in lineup.players:
        if not set(player.positions) & {"P", "SP", "RP"}:
            counts[player.team] = counts.get(player.team, 0) + 1

    assert max(counts.values()) >= 4


def test_defense_is_never_paired_with_the_offense_it_faces():
    config = get_config("NFL", "DK")
    lineup = optimize_lineup(_pool(config), config, cash_settings(config))

    defenses = [p for p in lineup.players if set(p.positions) & {"DST", "DEF"}]
    for defense in defenses:
        opposing = [p for p in lineup.players if p.team == defense.opponent]
        assert not opposing, f"{defense.name} rostered against own offense"


def test_hitters_are_never_paired_against_a_rostered_pitcher():
    config = get_config("MLB", "DK")
    lineup = optimize_lineup(_pool(config), config, cash_settings(config))

    pitchers = [p for p in lineup.players if set(p.positions) & {"P", "SP", "RP"}]
    for pitcher in pitchers:
        opposing = [
            p for p in lineup.players
            if p.team == pitcher.opponent and not set(p.positions) & {"P", "SP", "RP"}
        ]
        assert not opposing, f"hitters rostered against {pitcher.name}"


def test_ceiling_weight_changes_which_players_are_chosen():
    config = get_config("NBA", "DK")
    pool = _pool(config)

    mean_settings = cash_settings(config)
    ceiling_settings = cash_settings(config)
    ceiling_settings.ceiling_weight = 1.0

    by_mean = optimize_lineup(pool, config, mean_settings)
    by_ceiling = optimize_lineup(pool, config, ceiling_settings)

    assert by_ceiling.total_ceiling >= by_mean.total_ceiling


def test_ownership_penalty_lowers_lineup_ownership():
    config = get_config("NBA", "DK")
    pool = _pool(config)

    plain = optimize_lineup(pool, config, cash_settings(config))

    contrarian_settings = cash_settings(config)
    contrarian_settings.ownership_penalty = 0.5
    contrarian = optimize_lineup(pool, config, contrarian_settings)

    assert contrarian.total_ownership < plain.total_ownership


def test_multi_lineup_build_returns_distinct_lineups():
    config = get_config("NFL", "DK")
    settings = gpp_settings(config, lineups=10)
    settings.random_seed = 42

    lineups = optimize_lineups(_pool(config), config, settings, count=10)

    assert len(lineups) == 10
    assert len({frozenset(l.player_ids()) for l in lineups}) == 10


def test_minimum_uniqueness_is_respected():
    config = get_config("NFL", "DK")
    settings = gpp_settings(config, lineups=8)
    settings.min_unique = 3
    settings.random_seed = 7

    lineups = optimize_lineups(_pool(config), config, settings, count=8)

    for first, second in itertools.combinations(lineups, 2):
        overlap = len(first.player_ids() & second.player_ids())
        assert overlap <= config.roster_size - 3


def test_exposure_cap_is_respected():
    config = get_config("NFL", "DK")
    settings = gpp_settings(config, lineups=20)
    settings.max_exposure = 0.4
    settings.random_seed = 42

    lineups = optimize_lineups(_pool(config), config, settings, count=20)

    for row in exposure_report(lineups):
        assert row["exposure"] <= 0.4 + 1e-9


def test_exposure_report_sums_to_the_roster():
    config = get_config("NBA", "DK")
    settings = gpp_settings(config, lineups=5)
    settings.random_seed = 1
    lineups = optimize_lineups(_pool(config), config, settings, count=5)

    total = sum(row["count"] for row in exposure_report(lineups))
    assert total == len(lineups) * config.roster_size


def test_a_pool_too_small_to_fill_the_roster_is_rejected():
    config = get_config("NBA", "DK")
    with pytest.raises(InfeasibleLineup, match="available"):
        optimize_lineup(_pool(config)[:3], config, cash_settings(config))


def test_an_unaffordable_pool_is_rejected():
    config = get_config("NBA", "DK")
    pool = _pool(config)
    for player in pool:
        player["salary"] = config.salary_cap  # every player alone busts the cap

    with pytest.raises(InfeasibleLineup):
        optimize_lineup(pool, config, cash_settings(config))


def test_a_wrong_sport_pool_is_rejected_rather_than_silently_solved():
    """An NBA pool must not produce an NFL lineup."""

    nba = get_config("NBA", "DK")
    nfl = get_config("NFL", "DK")

    with pytest.raises(InfeasibleLineup):
        optimize_lineup(_pool(nba), nfl, cash_settings(nfl))


def test_multi_lineup_build_stops_early_rather_than_raising():
    """Running out of room is a shorter list, not an exception."""

    config = get_config("NBA", "DK")
    settings = OptimizerSettings(min_unique=8)
    lineups = optimize_lineups(_pool(config, games=2), config, settings, count=50)

    assert 0 < len(lineups) < 50


# ----------------------------------------------------------------------
# The team cap counts only the positions the site says it counts
# ----------------------------------------------------------------------


def _is_pitcher(player) -> bool:
    return any(position in ("P", "SP", "RP") for position in player.positions)


def test_a_five_hitter_stack_may_also_roster_that_team_s_pitcher():
    """DraftKings caps *hitters* at five from one team, not players.

    The lineup this allows is a real construction -- five bats and the
    pitcher opposing them is self-defeating, but five bats and their own
    pitcher is not, and it was previously impossible to build.
    """

    config = get_config("MLB", "DK")
    pool = _pool(config)

    # Make one team's hitters and one of its pitchers overwhelmingly the
    # best available, so the optimizer wants six of them.
    for player in pool:
        player["projected_points"] = 60.0 if player["team"] == "AAA" else 1.0

    lineup = optimize_lineup(pool, config, cash_settings(config))
    rostered = [p for p in lineup.players if p.team == "AAA"]
    hitters = [p for p in rostered if not _is_pitcher(p)]

    assert len(hitters) <= 5
    assert len(rostered) > 5, "the pitcher should not count against the hitter cap"


def test_the_hitter_cap_itself_still_binds():
    config = get_config("MLB", "DK")
    pool = _pool(config)

    for player in pool:
        player["projected_points"] = 60.0 if player["team"] == "AAA" else 1.0

    lineup = optimize_lineup(pool, config, cash_settings(config))
    hitters = [
        p for p in lineup.players
        if p.team == "AAA" and not _is_pitcher(p)
    ]

    assert len(hitters) == 5


def test_a_sport_with_no_exclusion_caps_every_player():
    """FanDuel NFL caps four players from a team, with no carve-out."""

    config = get_config("NFL", "FD")
    pool = _pool(config)

    for player in pool:
        player["projected_points"] = 60.0 if player["team"] == "AAA" else 1.0

    lineup = optimize_lineup(pool, config, cash_settings(config))

    assert sum(1 for p in lineup.players if p.team == "AAA") <= 4


# ----------------------------------------------------------------------
# Who is playing: the salary file lists the roster, not the starters
# ----------------------------------------------------------------------


def test_a_banned_pitcher_never_appears_however_good_he_looks():
    """The control that matters most in baseball.

    A salary file lists forty pitchers and about a dozen start. A relief
    pitcher priced like a starter projects on his rate per inning and
    will win the optimizer outright, on innings he is not going to
    throw.
    """

    config = get_config("MLB", "DK")
    pool = _pool(config)
    pitchers = [p for p in pool if _is_pitcher_row(p)]
    star = pitchers[0]

    for player in pool:
        player["projected_points"] = 200.0 if player is star else 5.0

    settings = cash_settings(config)
    settings.bans = frozenset({str(star["player_id"])})

    lineup = optimize_lineup(pool, config, settings)

    assert str(star["player_id"]) not in {p.player_id for p in lineup.players}


def test_naming_the_confirmed_starters_excludes_the_rest():
    """A whitelist: name the dozen who start, not the forty who do not."""

    config = get_config("MLB", "DK")
    pool = _pool(config)
    pitchers = [str(p["player_id"]) for p in pool if _is_pitcher_row(p)]
    confirmed = set(pitchers[:4])

    settings = cash_settings(config)
    settings.bans = frozenset(set(pitchers) - confirmed)

    lineup = optimize_lineup(pool, config, settings)
    rostered = {p.player_id for p in lineup.players if _is_pitcher(p)}

    assert rostered <= confirmed


def test_a_locked_player_appears_even_when_the_projection_says_otherwise():
    config = get_config("MLB", "DK")
    pool = _pool(config)
    for player in pool:
        player["projected_points"] = 5.0
    unloved = pool[7]
    unloved["projected_points"] = 0.1

    settings = cash_settings(config)
    settings.locks = frozenset({str(unloved["player_id"])})

    lineup = optimize_lineup(pool, config, settings)

    assert str(unloved["player_id"]) in {p.player_id for p in lineup.players}


def test_banning_so_many_players_that_nothing_is_legal_says_so():
    """Better than a silent empty result the user has to interpret."""

    config = get_config("MLB", "DK")
    pool = _pool(config)
    settings = cash_settings(config)
    settings.bans = frozenset(str(p["player_id"]) for p in pool if _is_pitcher_row(p))

    with pytest.raises(InfeasibleLineup):
        optimize_lineup(pool, config, settings)


def _is_pitcher_row(player) -> bool:
    return any(str(p).upper() in ("P", "SP", "RP") for p in player.get("positions") or [])


# ----------------------------------------------------------------------
# Batting-order stacks: a block of the order, not four scattered hitters
# ----------------------------------------------------------------------


LINEUP_CARD = ["C", "1B", "2B", "3B", "SS", "OF", "OF", "OF", "OF"]


def _with_batting_orders(pool):
    """Give every team a believable lineup card."""

    from collections import defaultdict

    slots = defaultdict(list)
    for player in pool:
        if _is_pitcher_row(player):
            continue
        filled = slots[player["team"]]
        if len(filled) < 9 and player["positions"][0] == LINEUP_CARD[len(filled)]:
            filled.append(player)
            player["batting_order"] = len(filled)
    return pool


def _has_consecutive_block(orders, count) -> bool:
    """Whether `orders` contains a run of `count` slots, wrapping at 9."""

    present = set(orders)
    for start in range(1, 10):
        window = {((start - 1 + step) % 9) + 1 for step in range(count)}
        if window <= present:
            return True
    return False


def test_a_team_stack_becomes_a_block_of_the_batting_order():
    """One big inning pays a block at once. The same four hitters
    scattered through the order need four separate innings."""

    import dataclasses
    from collections import defaultdict

    config = get_config("MLB", "DK")
    pool = _with_batting_orders(_pool(config))
    for index, player in enumerate(pool):
        player["projected_points"] = 8.0 + (index % 7)

    settings = gpp_settings(config, 1)
    settings.stacks = tuple(
        dataclasses.replace(stack, consecutive=True) if stack.kind == "team" else stack
        for stack in settings.stacks
    )

    lineup = optimize_lineup(pool, config, settings)

    by_id = {str(player["player_id"]): player for player in pool}
    orders = defaultdict(list)
    for slot in lineup.players:
        player = by_id[slot.player_id]
        if player.get("batting_order"):
            orders[player["team"]].append(player["batting_order"])

    stack = next(s for s in settings.stacks if s.kind == "team")
    assert any(
        _has_consecutive_block(slots, stack.count) for slots in orders.values()
    ), dict(orders)


def test_the_order_wraps_because_it_is_a_cycle():
    """The 8, 9 and 1 hitters bat in succession just as 2, 3, 4 do."""

    assert _has_consecutive_block([8, 9, 1, 2], 4)
    assert _has_consecutive_block([9, 1, 2], 3)
    assert not _has_consecutive_block([1, 2, 4, 5], 4)


def test_a_team_with_no_posted_lineup_is_left_under_the_looser_rule():
    """Enforcing an order nobody has published yet would mean no lineup
    at all rather than a looser one."""

    import dataclasses

    config = get_config("MLB", "DK")
    pool = _pool(config)          # deliberately no batting orders
    for index, player in enumerate(pool):
        player["projected_points"] = 8.0 + (index % 7)

    settings = gpp_settings(config, 1)
    settings.stacks = tuple(
        dataclasses.replace(stack, consecutive=True) if stack.kind == "team" else stack
        for stack in settings.stacks
    )

    lineup = optimize_lineup(pool, config, settings)

    assert len(lineup.players) == config.roster_size


def test_minimum_distinct_teams_is_respected():
    """DraftKings soccer wants three different sides among eight.

    Built so an unconstrained solver would not comply: one team's
    players are made far the best in the pool, so the cheapest way to a
    high total is to take them all. The floor has to be what stops it,
    which is also what makes this test bite -- the control below shows
    the same pool concentrating when the floor is lifted.
    """

    config = get_config("EPL", "DK")
    pool = _pool(config)
    for player in pool:
        if player["team"] == pool[0]["team"]:
            player["projected_points"] *= 6
            player["ceiling"] *= 6
            player["floor"] *= 6

    lineup = optimize_lineup(pool, config, cash_settings(config))
    teams = {player.team for player in lineup.players}

    assert len(teams) >= config.min_teams == 3


def test_without_the_floor_the_same_pool_piles_into_one_team():
    """The control. If this failed, the test above would pass whether
    or not the constraint existed."""

    config = dataclasses.replace(get_config("EPL", "DK"), min_teams=1)
    pool = _pool(config)
    for player in pool:
        if player["team"] == pool[0]["team"]:
            player["projected_points"] *= 6
            player["ceiling"] *= 6
            player["floor"] *= 6

    lineup = optimize_lineup(pool, config, cash_settings(config))
    teams = {player.team for player in lineup.players}

    assert len(teams) < 3


def test_a_distinct_team_floor_does_not_forbid_a_big_side():
    """Three teams among eight players leaves room for six from one,
    which DraftKings permits and a per-team cap would not."""

    assert get_config("EPL", "DK").max_per_team is None


# ----------------------------------------------------------------------
# Slot eligibility comes from the site, not from memory
# ----------------------------------------------------------------------


def test_published_roster_positions_replace_derived_ones_rather_than_adding():
    """A catcher-eligible first baseman put in the C slot.

    The site said 1B for this slate. The player's stored positions
    still said C, from a file for another slate or the other site. The
    two were unioned, so the solver believed both and DraftKings
    rejected the lineup at upload -- naming the player, saying nothing
    about why.

    Fixing the pool's eligibility was not enough on its own: the solver
    derives slots again here, and this is the path that was still
    widening them.
    """

    config = get_config("MLB", "DK")
    player = {
        "name": "Salvador Perez",
        "roster_positions": ["1B"],
        "positions": ["C", "1B"],
    }

    assert _eligible_slots(player, config) == ["1B"]


def test_a_player_the_site_lists_at_two_slots_keeps_both():
    config = get_config("MLB", "DK")
    player = {"roster_positions": ["C", "1B"], "positions": ["1B", "C"]}

    assert _eligible_slots(player, config) == ["1B", "C"]


def test_positions_are_still_used_when_the_site_publishes_no_slots():
    config = get_config("MLB", "DK")
    player = {"roster_positions": [], "positions": ["SS"]}

    assert _eligible_slots(player, config) == ["SS"]


def test_positions_are_used_when_published_names_are_not_slot_names():
    """FanDuel soccer lists FWD and MID against a slot called FWD/MID,
    so the published names intersect nothing and the positions are the
    only way into the slot."""

    config = get_config("EPL", "FD")
    player = {"roster_positions": ["FWD", "MID"], "positions": ["FWD"]}

    assert _eligible_slots(player, config) == ["FWD/MID"]


def test_no_lineup_places_a_player_where_the_site_did_not_list_him():
    """End to end, with the stored positions deliberately wrong.

    Every player is given every position, which is what an overwritten
    `players` row looks like at its worst. Only the published roster
    positions should decide, so no lineup may use a slot the pool did
    not publish for that player.
    """

    config = get_config("MLB", "DK")
    pool = _pool(config)
    everything = ["C", "1B", "2B", "3B", "SS", "OF", "P"]
    published = {}
    for player in pool:
        published[player["player_id"]] = set(player["roster_positions"])
        player["positions"] = everything

    lineups = optimize_lineups(pool, config, gpp_settings(config), count=10)

    assert lineups
    for lineup in lineups:
        for player in lineup.players:
            allowed = published[player.player_id]
            assert player.slot in allowed, f"{player.name} placed at {player.slot}"
