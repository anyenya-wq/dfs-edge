"""Lineup construction: legality, correlation, and multi-lineup discipline."""

from __future__ import annotations

import itertools

import pytest

from dfs.ingest.salaries import expand_roster_eligibility
from dfs.optimizer.lineup import (
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
