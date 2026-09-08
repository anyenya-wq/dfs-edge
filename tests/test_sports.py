"""Rules configuration: rosters, scoring tables, and site differences."""

from __future__ import annotations

import pytest

from dfs.sports import CONFIGS, SITES, SPORTS, get_config, score_stat_line
from dfs.sports.bonuses import nba_double_bonus


def test_every_sport_site_pair_is_configured():
    assert len(CONFIGS) == len(SPORTS) * len(SITES)
    for sport in SPORTS:
        for site in SITES:
            assert get_config(sport, site).key == f"{sport}:{site}"


def test_unknown_pair_names_what_is_available():
    with pytest.raises(KeyError, match="Available"):
        get_config("CRICKET", "DK")


@pytest.mark.parametrize("key", sorted(CONFIGS))
def test_roster_is_internally_consistent(key):
    config = CONFIGS[key]
    assert config.roster_size == sum(slot.count for slot in config.roster)
    assert 8 <= config.roster_size <= 10
    assert config.salary_cap > 0
    # Every slot must accept at least one position, or it can never fill.
    for slot in config.roster:
        assert slot.eligible


def test_lookup_is_case_insensitive():
    assert get_config("nba", "dk") is get_config("NBA", "DK")


def test_nfl_reception_scoring_differs_by_site():
    """Full PPR on DraftKings, half on FanDuel -- the key site difference."""

    line = {"rec": 8, "rec_yd": 80, "rec_td": 1}
    dk = score_stat_line(line, ["WR"], get_config("NFL", "DK"))
    fd = score_stat_line(line, ["WR"], get_config("NFL", "FD"))

    assert dk - fd == pytest.approx(4.0)


def test_draftkings_yardage_bonus_is_a_step_function():
    config = get_config("NFL", "DK")
    under = score_stat_line({"rush_yd": 99}, ["RB"], config)
    over = score_stat_line({"rush_yd": 100}, ["RB"], config)

    assert over - under == pytest.approx(3.1)


def test_fanduel_pays_no_yardage_bonus():
    config = get_config("NFL", "FD")
    under = score_stat_line({"rush_yd": 99}, ["RB"], config)
    over = score_stat_line({"rush_yd": 100}, ["RB"], config)

    assert over - under == pytest.approx(0.1)


def test_double_double_bonus_is_draftkings_only():
    line = {"pts": 20, "reb": 11, "ast": 4, "stl": 1, "blk": 0, "tov": 2, "fg3m": 2}

    assert nba_double_bonus(line, "DK") == 1.5
    assert nba_double_bonus(line, "FD") == 0.0


def test_triple_double_replaces_rather_than_stacks():
    line = {"pts": 20, "reb": 11, "ast": 10, "stl": 1, "blk": 0, "tov": 2}
    assert nba_double_bonus(line, "DK") == 3.0


def test_pitchers_score_on_their_own_table():
    config = get_config("MLB", "DK")
    stats = {"ip": 7, "k": 10, "win": 1, "er": 1}

    as_pitcher = score_stat_line(stats, ["SP"], config)
    as_hitter = score_stat_line(stats, ["OF"], config)

    # A hitter's table has none of these keys, so it scores nothing.
    assert as_pitcher > 30
    assert as_hitter == 0.0


def test_goalies_score_on_their_own_table():
    config = get_config("NHL", "DK")
    stats = {"win": 1, "save": 30, "goal_against": 2}
    assert score_stat_line(stats, ["G"], config) > score_stat_line(stats, ["C"], config)


def test_unknown_stat_keys_are_ignored():
    config = get_config("NBA", "DK")
    clean = score_stat_line({"pts": 10}, ["PG"], config)
    noisy = score_stat_line({"pts": 10, "minutes": 38, "plus_minus": -7}, ["PG"], config)
    assert clean == noisy


def test_soccer_tables_are_flagged_unverified():
    """The soccer numbers are the least certain; the flag must say so."""

    from dfs.sports import VERIFICATION_NOTES

    for key in ("EPL:DK", "EPL:FD"):
        assert "UNVERIFIED" in VERIFICATION_NOTES[key]
        assert not CONFIGS[key].rules_verified


# ----------------------------------------------------------------------
# MLB:FD, read off FanDuel's own Rules & Scoring tab
# ----------------------------------------------------------------------


FD_MLB_RULES = {
    # Hitting, exactly as the page lists it.
    "single": 3, "double": 6, "triple": 9, "hr": 12,
    "rbi": 3.5, "run": 3.2, "bb": 3, "hbp": 3, "sb": 6,
}

FD_MLB_PITCHING_RULES = {"er": -3, "ip": 3, "quality_start": 4, "k": 3, "win": 6}


def test_fanduel_mlb_hitting_matches_the_published_table():
    config = get_config("MLB", "FD")

    for stat, points in FD_MLB_RULES.items():
        assert config.scoring[stat] == points, stat


def test_fanduel_mlb_pitching_matches_the_published_table():
    """The quality start was missing until the rules page was read."""

    config = get_config("MLB", "FD")

    for stat, points in FD_MLB_PITCHING_RULES.items():
        assert config.alt_scoring[stat] == points, stat


def test_the_published_table_has_nothing_the_config_lacks():
    """A row on the page with no key here is a scoring line going unpaid."""

    config = get_config("MLB", "FD")

    assert not set(FD_MLB_RULES) - set(config.scoring)
    assert not set(FD_MLB_PITCHING_RULES) - set(config.alt_scoring)


def test_draftkings_mlb_pays_no_quality_start():
    """The two pitcher tables differ here, and it is worth four points."""

    assert "quality_start" not in get_config("MLB", "DK").alt_scoring
