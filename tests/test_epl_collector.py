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
    # goal 10 + assist 6 + two tackles at 0.7
    assert points == pytest.approx(10 + 6 + 1.4)


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


def test_a_striker_and_a_defender_score_the_same_line_differently():
    """A conceded goal costs the defender and not the striker.

    Clean sheets are zeroed on both sides so the charge is the only
    thing that differs -- a fixture conceding two goals while also
    keeping a clean sheet cannot happen, and would make the comparison
    measure the +5 rather than the -2.
    """

    config = get_config("EPL", "DK")
    forward = _record(position="FWD", goals_conceded="2", clean_sheets="0")
    defender = _record(position="DEF", goals_conceded="2", clean_sheets="0")

    forward_points = score_stat_line(forward["stats"], ["F"], config)
    defender_points = score_stat_line(defender["stats"], ["D"], config)

    assert forward_points - defender_points == pytest.approx(2.0)


def test_goalkeeper_saves_are_read():
    record = _record(position="GK", saves="6", penalties_saved="1")
    assert record["stats"]["save"] == 6
    assert record["stats"]["penalty_save"] == 1


# --- the gap ----------------------------------------------------------

def test_the_missing_stats_are_named_rather_than_implied():
    """The most important fact about soccer here, pinned in a test."""

    assert set(MISSING_STATS) == {"shot_on_goal", "created_chance", "cross"}
    for stat in MISSING_STATS:
        assert stat not in STAT_COLUMNS.values()


def test_the_missing_stats_are_ones_the_site_actually_pays_for():
    """If they were unscored, their absence would not matter."""

    scoring = get_config("EPL", "DK").scoring
    for stat in MISSING_STATS:
        assert stat in scoring


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
