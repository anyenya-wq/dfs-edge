"""The hoopR NBA collector: mapping, exhibitions, and DNP handling."""

from __future__ import annotations

import os
from datetime import date

import pandas as pd
import pytest

from dfs.db.database import Database
from dfs.ingest.stats import has_collector, load_history, log_counts
from dfs.ingest.stats.nba import (
    MIN_GAMES_FOR_REAL_TEAM,
    _drop_exhibitions,
    _normalise_row,
    _positions,
)
from dfs.sports import get_config, score_stat_line

needs_network = pytest.mark.skipif(
    not os.environ.get("DFS_NETWORK_TESTS"),
    reason="set DFS_NETWORK_TESTS=1 to run tests that download from hoopR",
)


def _row(**overrides):
    row = {
        "athlete_display_name": "Test Player",
        "athlete_position_abbreviation": "PG",
        "game_date": date(2026, 1, 15),
        "team_abbreviation": "dal",
        "opponent_team_abbreviation": "phx",
        "home_away": "home",
        "did_not_play": False,
        "minutes": 34.0,
        "points": 28.0,
        "rebounds": 6.0,
        "assists": 9.0,
        "steals": 2.0,
        "blocks": 1.0,
        "turnovers": 3.0,
        "three_point_field_goals_made": 4.0,
    }
    row.update(overrides)
    return row


def test_columns_map_onto_the_scoring_keys():
    record = _normalise_row(_row())
    assert record["stats"] == {
        "pts": 28.0, "reb": 6.0, "ast": 9.0,
        "stl": 2.0, "blk": 1.0, "tov": 3.0, "fg3m": 4.0,
    }


def test_the_mapped_stats_actually_score():
    """The mapping is only right if the scoring table recognises it."""

    record = _normalise_row(_row())
    points = score_stat_line(record["stats"], ["PG"], get_config("NBA", "DK"))
    # 28 + 4(0.5) + 6(1.25) + 9(1.5) + 2(2) + 1(2) - 3(0.5). No
    # double-double: only points clears ten, since assists stop at nine.
    assert points == pytest.approx(28 + 2 + 7.5 + 13.5 + 4 + 2 - 1.5)


def test_the_double_double_bonus_turns_on_at_ten():
    """One assist either side of the boundary, to pin the step down."""

    config = get_config("NBA", "DK")
    nine = _normalise_row(_row(assists=9.0))
    ten = _normalise_row(_row(assists=10.0))

    without = score_stat_line(nine["stats"], ["PG"], config)
    with_bonus = score_stat_line(ten["stats"], ["PG"], config)

    # The extra assist is worth its own 1.5 plus the 1.5 bonus it unlocks.
    assert with_bonus - without == pytest.approx(3.0)


def test_minutes_are_the_opportunity_term():
    """NBA declares minutes as its opportunity stat; no proxy needed."""

    assert _normalise_row(_row(minutes=31.5))["opportunity"] == 31.5


def test_a_did_not_play_is_kept_with_zero_opportunity():
    """The projection engine needs the zeros to read availability."""

    record = _normalise_row(_row(did_not_play=True))
    assert record is not None
    assert record["opportunity"] == 0.0
    assert record["stats"] == {}


def test_missing_minutes_become_zero_rather_than_null():
    assert _normalise_row(_row(minutes=None))["opportunity"] == 0.0


def test_zero_valued_stats_are_dropped():
    record = _normalise_row(_row(blocks=0.0, steals=0.0))
    assert "blk" not in record["stats"]
    assert "stl" not in record["stats"]


def test_generic_guard_and_forward_labels_expand():
    """ESPN's 'G' is genuinely eligible at either guard spot."""

    assert _positions("G") == ["PG", "SG"]
    assert _positions("F") == ["SF", "PF"]
    assert _positions("C") == ["C"]
    assert _positions("PG") == ["PG"]


def test_rows_without_a_name_date_or_position_are_dropped():
    assert _normalise_row(_row(athlete_display_name=None)) is None
    assert _normalise_row(_row(game_date=None)) is None
    assert _normalise_row(_row(athlete_position_abbreviation="")) is None


def test_teams_are_upper_cased_for_the_salary_join():
    record = _normalise_row(_row())
    assert record["team"] == "DAL"
    assert record["opponent"] == "PHX"


def test_home_away_is_read():
    assert _normalise_row(_row(home_away="home"))["home"] is True
    assert _normalise_row(_row(home_away="away"))["home"] is False


def test_player_id_matches_the_salary_parser():
    from dfs.ingest.salaries import parse_salaries

    record = _normalise_row(_row(athlete_display_name="Nikola Jokić"))
    csv = (
        "Position,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,AvgPointsPerGame\n"
        "C,Nikola Jokic,1,C,11800,DEN@LAL 01/15/2026 10:00PM ET,DEN,58.1\n"
    )
    assert parse_salaries(csv, "NBA", "DK")[0]["player_id"] == record["player_id"]


def _frame(rows):
    return pd.DataFrame(rows)


def test_exhibition_teams_are_dropped():
    """All-Star rows carry season_type 2, so only games played separates them."""

    real = [
        {"team_abbreviation": "DAL", "game_id": game}
        for game in range(MIN_GAMES_FOR_REAL_TEAM + 5)
    ]
    allstar = [
        {"team_abbreviation": "STARS", "game_id": 9001},
        {"team_abbreviation": "WORLD", "game_id": 9002},
    ]

    kept = _drop_exhibitions(_frame(real + allstar))
    assert set(kept["team_abbreviation"]) == {"DAL"}


def test_a_team_on_the_threshold_is_kept():
    rows = [
        {"team_abbreviation": "DAL", "game_id": game}
        for game in range(MIN_GAMES_FOR_REAL_TEAM)
    ]
    assert len(_drop_exhibitions(_frame(rows))) == MIN_GAMES_FOR_REAL_TEAM


def test_repeated_rows_for_one_game_do_not_inflate_the_count():
    """Counts distinct games, not rows -- a roster is many rows per game."""

    rows = [{"team_abbreviation": "STARS", "game_id": 1} for _ in range(200)]
    assert _drop_exhibitions(_frame(rows)).empty


def test_a_missing_column_degrades_to_collecting_everything():
    """Better too much than a silent empty collection on a schema change."""

    frame = _frame([{"team_abbreviation": "DAL"}])
    assert len(_drop_exhibitions(frame)) == 1


def test_nba_is_registered():
    assert has_collector("NBA")


@needs_network
def test_seasons_are_discovered_from_the_live_feed():
    from dfs.ingest.stats.nba import available_seasons

    seasons = available_seasons(count=2)
    assert len(seasons) == 2
    assert seasons == sorted(seasons)


@needs_network
def test_a_real_season_loads_and_excludes_exhibitions(tmp_path):
    from dfs.ingest.stats.nba import available_seasons

    database = Database(tmp_path / "nba.db")
    latest = available_seasons(count=1)
    summary = load_history("NBA", database, seasons=latest)

    assert summary["logs"] > 20_000
    assert log_counts(database)["NBA"] == summary["logs"]

    teams = {
        row["team"]
        for row in database.connection.execute(
            "SELECT DISTINCT team FROM game_logs WHERE sport='NBA'"
        )
    }
    assert len(teams) == 30
    assert not teams & {"STARS", "STRIPES", "WORLD", "EAST", "WEST"}
