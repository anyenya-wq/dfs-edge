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
    # FanDuel soccer is seven; every other pair is eight to ten.
    assert 7 <= config.roster_size <= 10
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


@pytest.mark.parametrize("site", ["DK", "FD"])
@pytest.mark.parametrize("stat, threshold", [
    ("rush_yd", 100), ("rec_yd", 100), ("pass_yd", 300),
])
def test_nfl_yardage_bonus_is_a_step_function_on_both_sites(site, stat, threshold):
    """Both sites pay all three, which is not what this file used to say.

    It asserted FanDuel paid none. Read from a FanDuel contest's own
    Rules & Scoring tab, which lists 100+ ReY, 100+ RuY and 300+ PaY at
    three points each. A missing bonus is invisible in a mean and
    decisive in a ceiling, so the tournament build was the half that
    was wrong.
    """

    config = get_config("NFL", site)
    rate = config.scoring[stat]
    under = score_stat_line({stat: threshold - 1}, ["RB"], config)
    over = score_stat_line({stat: threshold}, ["RB"], config)

    assert over - under == pytest.approx(3.0 + rate)


@pytest.mark.parametrize("site", ["DK", "FD"])
def test_a_returned_extra_point_pays_the_defence_two(site):
    config = get_config("NFL", site)
    line = {"points_allowed": 24, "extra_point_return": 1}

    assert score_stat_line(line, ["DST"], config) == pytest.approx(2.0)


@pytest.mark.parametrize("site", ["DK", "FD"])
def test_nfl_scoring_tables_have_been_read_from_the_sites(site):
    """Guards the flag itself. An unverified table still produces
    confident-looking numbers, so the only thing standing between a
    guessed rule and a real entry is this boolean and the warning the
    board hangs off it."""

    assert get_config("NFL", site).rules_verified


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


def test_soccer_tables_have_been_read_from_the_sites():
    """This asserted the opposite until the rules pages were read.

    Soccer was the least certain of the five and the flag said so.
    Both tables are now transcribed from source -- DraftKings' Classic
    Soccer rules and a FanDuel contest's Rules & Scoring tab -- so the
    flag has to move with them or it stops meaning anything.
    """

    for key in ("EPL:DK", "EPL:FD"):
        assert CONFIGS[key].rules_verified


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


# ----------------------------------------------------------------------
# MLB:DK, read from DraftKings' published MLB Classic rules
# ----------------------------------------------------------------------


DK_MLB_HITTING = {
    "single": 3, "double": 5, "triple": 8, "hr": 10,
    "rbi": 2, "run": 2, "bb": 2, "hbp": 2, "sb": 5,
}

DK_MLB_PITCHING = {
    "ip": 2.25, "k": 2, "win": 4, "er": -2,
    "hit_allowed": -0.6, "bb_allowed": -0.6, "hbp_allowed": -0.6,
    "complete_game": 2.5, "complete_game_shutout": 2.5, "no_hitter": 5,
}


def test_draftkings_mlb_matches_the_published_tables():
    config = get_config("MLB", "DK")

    for stat, points in DK_MLB_HITTING.items():
        assert config.scoring[stat] == points, stat
    for stat, points in DK_MLB_PITCHING.items():
        assert config.alt_scoring[stat] == points, stat


def test_an_inning_pitched_is_three_quarters_of_a_point_per_out():
    """The rules give both forms: 2.25 per inning, 0.75 per out."""

    config = get_config("MLB", "DK")

    assert config.score_stat_line({"ip": 1.0}, ["P"]) == 2.25
    assert config.score_stat_line({"ip": 1 / 3}, ["P"]) == pytest.approx(0.75)


def test_draftkings_mlb_roster_and_cap_match_the_published_rules():
    config = get_config("MLB", "DK")

    assert config.salary_cap == 50_000
    assert config.roster_size == 10
    assert config.min_games == 2

    slots = {slot.name: slot.count for slot in config.roster}
    assert slots == {"P": 2, "C": 1, "1B": 1, "2B": 1, "3B": 1, "SS": 1, "OF": 3}


def test_the_team_limit_counts_hitters_and_not_pitchers():
    """"No more than 5 hitters from any one team" -- hitters.

    A five-man stack alongside that same team's pitcher is six players
    from one team and a legal lineup. Counting the pitcher would rule it
    out.
    """

    config = get_config("MLB", "DK")

    assert config.max_per_team == 5
    assert config.counts_toward_team_cap(["OF"])
    assert config.counts_toward_team_cap(["C", "1B"])
    assert not config.counts_toward_team_cap(["P"])
    assert not config.counts_toward_team_cap(["SP"])


def test_a_sport_without_an_exclusion_counts_everyone():
    assert get_config("NFL", "FD").counts_toward_team_cap(["QB"])
    assert get_config("NBA", "DK").counts_toward_team_cap(["PG"])


# ----------------------------------------------------------------------
# The NFL tables, as published
# ----------------------------------------------------------------------
#
# Transcribed from the sites: DraftKings from its NFL Classic rules
# page, FanDuel from a contest's own Rules & Scoring tab. Kept verbatim
# and asserted whole rather than spot-checked, because the failure mode
# these guard is a single wrong rate that nothing else notices -- a
# projection built on it is not obviously wrong, it is just quietly
# beaten. Change a number here only with the site open.

PUBLISHED_NFL_OFFENCE = {
    "DK": {
        "pass_yd": 0.04, "pass_td": 4.0, "pass_int": -1.0,
        "rush_yd": 0.1, "rush_td": 6.0,
        "rec": 1.0, "rec_yd": 0.1, "rec_td": 6.0,
        "fumble_lost": -1.0, "two_point_conv": 2.0,
        "return_td": 6.0, "fumble_recovery_td": 6.0,
    },
    "FD": {
        "pass_yd": 0.04, "pass_td": 4.0, "pass_int": -1.0,
        "rush_yd": 0.1, "rush_td": 6.0,
        "rec": 0.5, "rec_yd": 0.1, "rec_td": 6.0,
        "fumble_lost": -2.0, "two_point_conv": 2.0,
        "return_td": 6.0, "fumble_recovery_td": 6.0,
    },
}

# Identical on the two sites, which is worth stating rather than
# assuming: it was assumed here for a while, and the assumption was
# only checked afterwards.
PUBLISHED_NFL_DEFENCE = {
    "sack": 1.0, "def_int": 2.0, "fumble_recovery": 2.0, "safety": 2.0,
    "def_td": 6.0, "return_td": 6.0, "blocked_kick": 2.0,
    "extra_point_return": 2.0,
}

PUBLISHED_POINTS_ALLOWED = [
    (0, 10.0), (3, 7.0), (6, 7.0), (7, 4.0), (13, 4.0),
    (14, 1.0), (20, 1.0), (21, 0.0), (27, 0.0),
    (28, -1.0), (34, -1.0), (35, -4.0), (52, -4.0),
]


@pytest.mark.parametrize("site", ["DK", "FD"])
def test_nfl_offence_table_matches_what_the_site_publishes(site):
    assert get_config("NFL", site).scoring == PUBLISHED_NFL_OFFENCE[site]


@pytest.mark.parametrize("site", ["DK", "FD"])
def test_nfl_defence_table_matches_what_the_site_publishes(site):
    assert get_config("NFL", site).alt_scoring == PUBLISHED_NFL_DEFENCE


@pytest.mark.parametrize("site", ["DK", "FD"])
@pytest.mark.parametrize("allowed, points", PUBLISHED_POINTS_ALLOWED)
def test_nfl_points_allowed_bands_match_what_the_site_publishes(site, allowed, points):
    config = get_config("NFL", site)

    assert score_stat_line({"points_allowed": allowed}, ["DST"], config) == points


@pytest.mark.parametrize("site", ["DK", "FD"])
def test_nfl_pays_all_three_yardage_bonuses(site):
    bonuses = {(b.stat, b.threshold): b.points for b in get_config("NFL", site).bonuses}

    assert bonuses == {
        ("pass_yd", 300): 3.0,
        ("rush_yd", 100): 3.0,
        ("rec_yd", 100): 3.0,
    }


# ----------------------------------------------------------------------
# The soccer tables, as published
# ----------------------------------------------------------------------
#
# DraftKings publishes one table headed "All Players (GK,D,M,F)" with
# two lines restricted inside it, plus a goalkeeper table. FanDuel
# publishes three. Both come to the same three-way split, so both are
# written out that way and asserted whole.

DK_SOCCER_OUTFIELD = {
    "goal": 10.0, "assist": 6.0, "shot": 1.0, "shot_on_goal": 1.0,
    "cross": 0.7, "created_chance": 1.0, "accurate_pass": 0.02,
    "fouls_drawn": 1.0, "fouls_conceded": -0.5, "tackle_won": 1.0,
    "interception": 0.5, "yellow_card": -1.5, "red_card": -3.0,
    "shootout_goal": 1.5, "shootout_miss": -1.0,
}

DK_SOCCER_KEEPER_EXTRAS = {
    "save": 2.0, "goal_allowed": -2.0, "clean_sheet": 5.0,
    "win": 5.0, "penalty_save": 3.0, "shootout_save": 1.5,
}

FD_SOCCER_OUTFIELD = {
    "goal": 15.0, "assist": 7.0, "shot": 1.0, "shot_on_goal": 4.0,
    "cross": 0.5, "created_chance": 2.5, "blocked_shot": 1.6,
    "clearance": 1.6, "interception": 1.6, "tackle_won": 1.6,
    "fouls_drawn": 1.0, "penalty_miss": -3.0,
    "yellow_card": -1.0, "red_card": -3.0,
}

FD_SOCCER_KEEPER = {
    "clean_sheet": 8.0, "goal_allowed": -2.5, "save": 2.5,
    "penalty_save": 2.5, "win": 6.0,
}


def test_draftkings_soccer_tables_match_what_the_site_publishes():
    config = get_config("EPL", "DK")

    assert config.scoring_table(["M"]) == DK_SOCCER_OUTFIELD
    assert config.scoring_table(["F"]) == DK_SOCCER_OUTFIELD
    assert config.scoring_table(["D"]) == {**DK_SOCCER_OUTFIELD, "clean_sheet": 3.0}
    assert config.scoring_table(["GK"]) == {
        **{k: v for k, v in DK_SOCCER_OUTFIELD.items() if k != "interception"},
        **DK_SOCCER_KEEPER_EXTRAS,
    }


def test_fanduel_soccer_tables_match_what_the_site_publishes():
    config = get_config("EPL", "FD")

    assert config.scoring_table(["M"]) == FD_SOCCER_OUTFIELD
    assert config.scoring_table(["F"]) == FD_SOCCER_OUTFIELD
    assert config.scoring_table(["D"]) == {
        **FD_SOCCER_OUTFIELD, "clean_sheet": 5.0, "goal_allowed": -0.6,
    }
    assert config.scoring_table(["GK"]) == FD_SOCCER_KEEPER


@pytest.mark.parametrize("site", ["DK", "FD"])
def test_a_forward_is_never_paid_a_clean_sheet(site):
    """The whole reason soccer needs three tables rather than two.

    A clean sheet is 3 to 8 points depending on site and position, and
    it lands on the one stat every collector reports for every player
    on the team. Paid to a forward it is not a rounding error -- it is
    most of a cheap striker's projection, invented.
    """

    config = get_config("EPL", site)
    line = {"clean_sheet": 1, "minutes": 90}

    assert score_stat_line(line, ["F"], config) == 0.0
    assert score_stat_line(line, ["M"], config) == 0.0
    assert score_stat_line(line, ["D"], config) > 0.0
    assert score_stat_line(line, ["GK"], config) > 0.0


def test_a_draftkings_shot_on_target_is_worth_two():
    """DraftKings' own note: a shot on goal counts as a shot as well.

    Which means the collector has to report total shots and not shots
    that missed, or every attacker is short a point per attempt.
    """

    config = get_config("EPL", "DK")

    assert score_stat_line({"shot": 1, "shot_on_goal": 1}, ["F"], config) == 2.0


def test_neither_site_charges_for_an_own_goal():
    """Both tables used to. Neither published table has the line, and
    DraftKings' notes mention own goals only as something that counts
    against a keeper's goals-against and clean sheet -- never as a
    penalty against the scorer."""

    for site in ("DK", "FD"):
        config = get_config("EPL", site)
        for position in ("F", "M", "D", "GK"):
            assert "own_goal" not in config.scoring_table([position])


def test_draftkings_soccer_wants_three_teams_and_caps_none():
    """A distinct-teams floor is not a per-team cap, and the config
    carried the cap it does not have while missing the floor it does.
    Six players from one side is a legal DraftKings soccer lineup."""

    config = get_config("EPL", "DK")

    assert config.min_teams == 3
    assert config.max_per_team is None
    assert config.roster_size == 8
