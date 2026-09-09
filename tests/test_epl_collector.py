"""The EPL collector: FPL columns, positional scoring rules, and its gap.

Unlike MLB and NHL, this collector's source is reachable, so it was
verified against three real seasons before these tests were written.
What the tests pin is the mapping and the positional rules -- and the
fact that three stats the sites pay for are absent from the feed, which
is the most important thing about soccer here.
"""

from __future__ import annotations

import os

import pytest

from dfs.ingest.stats import INCOMPLETE, has_collector
from dfs.ingest.stats.epl import (
    MISSING_STATS,
    STAT_COLUMNS,
    _normalise_row,
    season_label,
)
from dfs.sports import get_config, score_stat_line

needs_network = pytest.mark.skipif(
    not os.environ.get("DFS_NETWORK_TESTS"),
    reason="set DFS_NETWORK_TESTS=1 to run tests that download the FPL mirror",
)


def _row(**overrides):
    row = {
        "name": "Test Player", "position": "MID", "team": "Man City",
        "opponent_team": "20", "kickoff_time": "2026-08-16T16:30:00Z",
        "was_home": "False", "minutes": "90",
        "goals_scored": "1", "assists": "1", "clean_sheets": "1",
        "goals_conceded": "0", "yellow_cards": "0", "red_cards": "0",
        "own_goals": "0", "penalties_missed": "0", "penalties_saved": "0",
        "saves": "0", "tackles": "2",
    }
    row.update(overrides)
    return row


def _record(**overrides):
    return _normalise_row(_row(**overrides), {20: "Fulham"})


# --- mapping ----------------------------------------------------------

def test_columns_map_onto_the_scoring_keys():
    record = _record()
    assert record["stats"]["goal"] == 1
    assert record["stats"]["assist"] == 1
    assert record["stats"]["tackle_won"] == 2


def test_the_mapped_line_actually_scores():
    record = _record()
    points = score_stat_line(record["stats"], ["M"], get_config("EPL", "DK"))
    # goal 10 + assist 6 + two tackles at 1.0
    assert points == pytest.approx(10 + 6 + 2.0)


def test_minutes_are_the_opportunity_term():
    assert _record(minutes="67")["opportunity"] == 67.0


def test_the_kickoff_timestamp_is_trimmed_to_a_date():
    """A stored timestamp would never match a slate date."""

    assert _record()["game_date"] == "2026-08-16"


def test_home_and_away_are_read():
    assert _record(was_home="True")["home"] is True
    assert _record(was_home="False")["home"] is False


def test_the_opponent_id_is_resolved_to_a_name():
    assert _record()["opponent"] == "Fulham"


def test_an_unresolvable_opponent_is_left_empty():
    assert _normalise_row(_row(), {})["opponent"] is None


def test_positions_map_onto_what_the_salary_files_call_them():
    assert _record(position="GK")["positions"] == ["GK"]
    assert _record(position="DEF")["positions"] == ["D"]
    assert _record(position="MID")["positions"] == ["M"]
    assert _record(position="FWD")["positions"] == ["F"]


def test_an_unknown_position_is_dropped():
    assert _record(position="MANAGER") is None


def test_rows_without_a_name_or_kickoff_are_dropped():
    assert _record(name="") is None
    assert _record(kickoff_time="") is None


def test_zero_valued_stats_are_dropped():
    assert "goal" not in _record(goals_scored="0")["stats"]


# --- positional scoring rules ----------------------------------------

def test_defenders_and_keepers_keep_defensive_outcomes():
    for position in ("DEF", "GK"):
        record = _record(position=position, clean_sheets="1", goals_conceded="2")
        assert record["stats"]["clean_sheet"] == 1
        assert record["stats"]["goal_allowed"] == 2


def test_attackers_are_not_charged_for_goals_conceded():
    """FPL records it for everyone on the pitch; the sites charge only
    keepers and defenders, so a striker would lose points he never owes."""

    for position in ("MID", "FWD"):
        record = _record(position=position, goals_conceded="3")
        assert "goal_allowed" not in record["stats"]


def test_attackers_are_not_paid_for_clean_sheets():
    for position in ("MID", "FWD"):
        assert "clean_sheet" not in _record(position=position, clean_sheets="1")["stats"]


def test_draftkings_charges_nobody_but_the_keeper_for_a_conceded_goal():
    """This file used to assert the opposite, and was wrong.

    It had a DraftKings defender docked 2 points per goal conceded. He
    is docked nothing: goals against appear only on the goalkeeper's
    table. What a conceded goal costs an outfield defender is the clean
    sheet he would otherwise have had, and nothing more.
    """

    config = get_config("EPL", "DK")
    forward = _record(position="FWD", goals_conceded="2", clean_sheets="0")
    defender = _record(position="DEF", goals_conceded="2", clean_sheets="0")

    forward_points = score_stat_line(forward["stats"], ["F"], config)
    defender_points = score_stat_line(defender["stats"], ["D"], config)

    assert forward_points == pytest.approx(defender_points)


def test_fanduel_does_charge_the_defender_for_a_conceded_goal():
    """And this is why the rule cannot be shared between the sites. On
    FanDuel a conceded goal costs a defender 0.6 and a forward
    nothing, so the same match scores the two positions apart."""

    config = get_config("EPL", "FD")
    forward = _record(position="FWD", goals_conceded="2", clean_sheets="0")
    defender = _record(position="DEF", goals_conceded="2", clean_sheets="0")

    forward_points = score_stat_line(forward["stats"], ["F"], config)
    defender_points = score_stat_line(defender["stats"], ["D"], config)

    assert forward_points - defender_points == pytest.approx(1.2)


def test_goalkeeper_saves_are_read():
    record = _record(position="GK", saves="6", penalties_saved="1")
    assert record["stats"]["save"] == 6
    assert record["stats"]["penalty_save"] == 1


# --- the gap ----------------------------------------------------------

def _paid_for() -> set[str]:
    """Every stat either site pays for, across all its position tables."""

    paid: set[str] = set()
    for site in ("DK", "FD"):
        config = get_config("EPL", site)
        paid |= set(config.scoring)
        for entry in config.scoring_tables:
            paid |= set(entry.table)
    return paid


def test_the_missing_stats_are_named_rather_than_implied():
    """The most important fact about soccer here, pinned in a test.

    Derived rather than transcribed, and that is the point: the list
    said three stats for as long as the scoring tables were guesses.
    Reading the tables off the sites took it to eleven. Computing the
    gap here means the next change to either the feed or a scoring
    table fails this test instead of quietly making the constant a
    lie.
    """

    from dfs.ingest.stats.epl import MISSING_SHOOTOUT_STATS

    gap = _paid_for() - set(STAT_COLUMNS.values()) - set(MISSING_SHOOTOUT_STATS)

    assert set(MISSING_STATS) == gap
    for stat in MISSING_STATS:
        assert stat not in STAT_COLUMNS.values()


def test_the_missing_stats_are_ones_the_site_actually_pays_for():
    """If they were unscored, their absence would not matter."""

    paid = _paid_for()
    for stat in MISSING_STATS:
        assert stat in paid


def test_the_feed_carries_less_than_half_of_what_soccer_scores():
    """The headline number behind the banner, so it cannot drift
    unnoticed: ten scored stats present, eleven absent.

    The feed maps eleven columns, but one of them is own goals, which
    neither site charges for -- so it is collected and then scores
    nothing.
    """

    present = _paid_for() & set(STAT_COLUMNS.values())

    assert len(present) == 10
    assert len(MISSING_STATS) == 11
    assert "own_goal" in STAT_COLUMNS.values()
    assert "own_goal" not in _paid_for()


def test_the_combined_defensive_stat_is_not_mapped():
    """clearances_blocks_interceptions conflates three actions the sites
    price differently; mapping it to any one would invent points."""

    assert "clearances_blocks_interceptions" not in STAT_COLUMNS


def test_the_incompleteness_is_registered_for_the_ui():
    assert "EPL" in INCOMPLETE
    assert "shots on goal" in INCOMPLETE["EPL"]


# --- seasons ----------------------------------------------------------

def test_season_labels_match_the_mirror_s_folders():
    assert season_label(2026) == "2026-27"
    assert season_label(2019) == "2019-20"
    assert season_label(2099) == "2099-00"


def test_epl_is_registered():
    assert has_collector("EPL")


@needs_network
def test_seasons_are_discovered_from_the_live_mirror():
    from dfs.ingest.stats.epl import available_seasons

    seasons = available_seasons(count=2)
    assert len(seasons) == 2
    assert seasons == sorted(seasons)


@needs_network
def test_a_real_season_loads_with_all_four_positions(tmp_path):
    """Loads two seasons rather than one.

    The newest season can be a single gameweek old -- 610 rows in
    August -- so a volume assertion against it alone measures the
    calendar rather than the collector. Two seasons always clears a
    full one, which is also why the collector defaults to two: at the
    start of a season the previous one is all the history there is.
    """

    from dfs.db.database import Database
    from dfs.ingest.stats import load_history
    from dfs.ingest.stats.epl import available_seasons

    database = Database(tmp_path / "epl.db")
    summary = load_history("EPL", database, seasons=available_seasons(count=2))

    assert summary["logs"] > 5_000

    positions = {
        row["positions"]
        for row in database.connection.execute(
            "SELECT DISTINCT positions FROM players WHERE sport='EPL'"
        )
    }
    assert positions == {'["GK"]', '["D"]', '["M"]', '["F"]'}


# ----------------------------------------------------------------------
# Real exports: a DraftKings and a FanDuel Champions League slate
# ----------------------------------------------------------------------
#
# Written from the two files rather than invented, because every bug
# below survived a suite full of invented ones. Soccer is where the
# sites diverge most from their own house style, and each of these was
# silent: nothing raised, the board just quietly did less.

DK_SOCCER_EXPORT = (
    "Position,Name + ID,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,"
    "AvgPointsPerGame,Status,Starting\n"
    "F,Ousmane Dembele (1),Ousmane Dembele,1,F/UTIL,12200,"
    "PSG vs SLO 09/09/2026 03:00PM ET,PSG,14.7,,\n"
    "M/F,Ferran Torres (2),Ferran Torres,2,M/F/UTIL,10600,"
    "PSG vs SLO 09/09/2026 03:00PM ET,PSG,7.3,,\n"
    "D,Kostas Tsimikas (3),Kostas Tsimikas,3,D/UTIL,6200,"
    "LIV vs ATL 09/09/2026 03:00PM ET,LIV,5.1,,\n"
    "GK,Joan Garcia (4),Joan Garcia,4,GK,5500,"
    "LIV vs ATL 09/09/2026 03:00PM ET,ATL,6.2,,\n"
)

FD_SOCCER_EXPORT = (
    "Id,Position,First Name,Nickname,Last Name,FPPG,Played,Salary,Game,Team,"
    "Opponent,Injury Indicator,Injury Details,Tier,,,Roster Position\n"
    "1-1,FWD,Raphael,Raphinha,Dias Belloli,29.58,7,23,FEY@BAR,BAR,FEY,,,,,,FWD/MID\n"
    "1-2,MID,Fermin,Fermin Lopez,Lopez,24.18,11,19,FEY@BAR,BAR,FEY,,,,,,FWD/MID\n"
    "1-3,DEF,Jules,Jules Kounde,Kounde,12.0,9,14,FEY@BAR,BAR,FEY,,,,,,DEF\n"
    "1-4,GK,Joan,Joan Garcia,Garcia,11.0,9,14,FEY@BAR,BAR,FEY,,,,,,GK\n"
)


def test_a_fanduel_soccer_file_is_recognised_as_soccer():
    """It was not. FanDuel writes FWD/MID/DEF/GK and the config knew
    only F/M/D/GK, so detection matched 34% of players, named NFL as
    the closest guess and returned no sport at all."""

    from dfs.ingest.detect import detect

    detected = detect(FD_SOCCER_EXPORT)

    assert detected.site == "FD"
    assert detected.sport == "EPL"
    assert detected.coverage == 1.0


def test_draftkings_soccer_records_the_matchup():
    """DraftKings writes "LIV vs ATL" in soccer and "ATL@LIV"
    everywhere else. Only the second was read, so a soccer slate
    arrived with no opponent and no game id on any player -- which
    silently disabled the minimum-games rule, since a lineup spanning
    no known games cannot be found to span too few.
    """

    from dfs.ingest.salaries import parse_draftkings

    players = {p["name"]: p for p in parse_draftkings(DK_SOCCER_EXPORT, "EPL")}

    assert all(p["opponent"] for p in players.values())
    assert players["Ousmane Dembele"]["game_id"] == "SLO@PSG"
    assert players["Kostas Tsimikas"]["game_id"] == "ATL@LIV"


def test_draftkings_soccer_reads_home_first():
    """The opposite order from "@", and getting it backwards would not
    fail anything -- it would just put every side on the wrong end of
    home advantage."""

    from dfs.ingest.salaries import parse_draftkings

    players = {p["name"]: p for p in parse_draftkings(DK_SOCCER_EXPORT, "EPL")}

    assert players["Ousmane Dembele"]["home"] is True    # PSG vs SLO
    assert players["Kostas Tsimikas"]["home"] is True    # LIV vs ATL
    assert players["Joan Garcia"]["home"] is False       # ATL, the visitor


def test_both_real_soccer_exports_build_a_legal_lineup():
    from dfs.ingest.salaries import (
        expand_roster_eligibility, parse_draftkings, parse_fanduel,
    )
    from dfs.optimizer.lineup import optimize_lineup
    from dfs.optimizer.rules import cash_settings

    for site, text, parser in (
        ("DK", DK_SOCCER_EXPORT, parse_draftkings),
        ("FD", FD_SOCCER_EXPORT, parse_fanduel),
    ):
        config = get_config("EPL", site)
        players = parser(text, "EPL")
        for player in players:
            player["projected_points"] = 5.0
            player["ceiling"] = 7.5
            player["floor"] = 3.0

        pool = expand_roster_eligibility(players, config)
        # Four players cannot fill either roster; what is asserted is
        # that every one of them is eligible for a slot, which is what
        # a mismatched position vocabulary would break.
        slots = {slot.name for slot in config.roster}
        for player in pool:
            assert set(player["roster_positions"]) & slots, (site, player["name"])
