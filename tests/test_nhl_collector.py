"""The NHL collector: skater and goalie tables, TOI, and saves parsing.

Like the MLB collector, this one was written without being able to reach
its own data source — api-web.nhle.com is unreachable from the
environment it was built in — so these fixtures carry more weight than
usual and the gated live test is what confirms the shape.
"""

from __future__ import annotations

import os

import pytest

from dfs.db.database import Database
from dfs.ingest.stats import has_collector
from dfs.ingest.stats.nhl import (
    _goalie_record,
    _normalise_position,
    _player_name,
    _side_records,
    _skater_record,
    fetch_schedule,
    game_dates,
    saves_from_shots_against,
    time_on_ice_to_minutes,
)
from dfs.sports import get_config, score_stat_line

needs_network = pytest.mark.skipif(
    not os.environ.get("DFS_NETWORK_TESTS"),
    reason="set DFS_NETWORK_TESTS=1 to run tests that call api-web.nhle.com",
)

CONTEXT = {"team": "BOS", "opponent": "TOR", "home": True, "game_date": "2026-01-15"}


# --- time on ice ------------------------------------------------------

def test_time_on_ice_reads_minutes_and_seconds():
    """"18:24" is 18.4 minutes, not 18.24."""

    assert time_on_ice_to_minutes("18:24") == pytest.approx(18.4, abs=1e-3)
    assert time_on_ice_to_minutes("20:00") == 20.0
    assert time_on_ice_to_minutes("00:30") == pytest.approx(0.5, abs=1e-3)


def test_time_on_ice_handles_missing_and_malformed_values():
    assert time_on_ice_to_minutes(None) == 0.0
    assert time_on_ice_to_minutes("") == 0.0
    assert time_on_ice_to_minutes("nonsense") == 0.0
    assert time_on_ice_to_minutes("17") == 17.0


# --- goalie saves -----------------------------------------------------

def test_saves_are_read_from_the_shots_against_pair():
    """The feed writes "28/30" and expects the reader to split it."""

    assert saves_from_shots_against("28/30") == (28.0, 30.0)
    assert saves_from_shots_against("0/0") == (0.0, 0.0)


def test_saves_handle_missing_and_malformed_values():
    assert saves_from_shots_against(None) == (0.0, 0.0)
    assert saves_from_shots_against("") == (0.0, 0.0)
    assert saves_from_shots_against("junk") == (0.0, 0.0)


def test_a_goalie_score_collapses_if_saves_are_misread():
    """Why the pair matters: saves are nearly the whole goalie score."""

    config = get_config("NHL", "DK")
    saves, _ = saves_from_shots_against("28/30")
    with_saves = score_stat_line({"save": saves, "win": 1, "goal_against": 2}, ["G"], config)
    without = score_stat_line({"win": 1, "goal_against": 2}, ["G"], config)

    assert with_saves - without == pytest.approx(28 * 0.7)


# --- skaters ----------------------------------------------------------

def _skater(**overrides):
    entry = {
        "name": {"default": "Test Skater"}, "position": "C", "toi": "18:24",
        "goals": 1, "assists": 2, "sog": 5, "blockedShots": 2,
    }
    entry.update(overrides)
    return _skater_record(entry, CONTEXT)


def test_skater_columns_map_onto_the_scoring_keys():
    record = _skater()
    assert record["stats"] == {"goal": 1, "assist": 2, "sog": 5, "blocked_shot": 2}


def test_the_mapped_skater_line_actually_scores():
    record = _skater()
    points = score_stat_line(record["stats"], ["C"], get_config("NHL", "DK"))
    # goal 8.5 + 2 assists at 5 + 5 shots at 1.5 + 2 blocks at 1.3,
    # plus the five-shot bonus DraftKings pays.
    assert points == pytest.approx(8.5 + 10 + 7.5 + 2.6 + 3)


def test_time_on_ice_is_the_skater_s_opportunity():
    assert _skater()["opportunity"] == pytest.approx(18.4, abs=1e-3)


def test_zero_valued_skater_stats_are_dropped():
    record = _skater(goals=0, blockedShots=0)
    assert "goal" not in record["stats"]
    assert "blocked_shot" not in record["stats"]


def test_short_handed_points_are_taken_when_reported():
    assert _skater(shPoints=1)["stats"]["short_handed_point"] == 1


def test_short_handed_points_are_not_invented_when_absent():
    assert "short_handed_point" not in _skater()["stats"]


def test_wings_collapse_to_a_single_position():
    """Both sites list left and right wing as W."""

    for position in ("L", "R", "LW", "RW", "W"):
        assert _skater(position=position)["positions"] == ["W"]


def test_centres_and_defencemen_keep_their_position():
    assert _skater(position="C")["positions"] == ["C"]
    assert _skater(position="D")["positions"] == ["D"]
    assert _normalise_position("D") == "D"


def test_a_skater_without_a_name_is_skipped():
    assert _skater_record({"position": "C", "toi": "10:00"}, CONTEXT) is None


# --- goalies ----------------------------------------------------------

def _goalie(**overrides):
    entry = {
        "name": {"default": "Test Goalie"}, "toi": "60:00",
        "saveShotsAgainst": "28/30", "goalsAgainst": 2, "decision": "W",
    }
    entry.update(overrides)
    return _goalie_record(entry, CONTEXT)


def test_goalie_columns_map_onto_the_alternate_table():
    record = _goalie()
    assert record["stats"]["save"] == 28
    assert record["stats"]["goal_against"] == 2
    assert record["stats"]["win"] == 1.0
    assert record["positions"] == ["G"]


def test_a_goalie_is_scored_on_the_goalie_table():
    """The same numbers must not be read as a skater line."""

    record = _goalie()
    config = get_config("NHL", "DK")
    assert score_stat_line(record["stats"], ["G"], config) > 15
    assert score_stat_line(record["stats"], ["C"], config) == 0.0


def test_a_shutout_is_a_win_with_nothing_conceded():
    record = _goalie(goalsAgainst=0, saveShotsAgainst="31/31")
    assert record["stats"]["shutout"] == 1.0


def test_a_win_conceding_goals_is_not_a_shutout():
    assert "shutout" not in _goalie()["stats"]


def test_an_unused_goalie_cannot_be_credited_with_a_shutout():
    """A backup on the bench concedes nothing, which is not a shutout."""

    record = _goalie_record(
        {"name": {"default": "Backup"}, "toi": "00:00", "saveShotsAgainst": "0/0",
         "goalsAgainst": 0, "decision": ""},
        CONTEXT,
    )
    assert record is None


def test_an_overtime_loss_is_recorded_but_a_regulation_loss_is_not():
    """DraftKings pays the first and not the second."""

    assert _goalie(decision="O")["stats"]["ot_loss"] == 1.0
    assert "ot_loss" not in _goalie(decision="L")["stats"]
    assert "win" not in _goalie(decision="L")["stats"]


def test_time_on_ice_is_the_goalie_s_opportunity():
    assert _goalie()["opportunity"] == 60.0


# --- names and assembly ----------------------------------------------

def test_names_are_read_from_the_localised_object():
    assert _player_name({"name": {"default": "Auston Matthews"}}) == "Auston Matthews"


def test_names_fall_back_to_a_plain_string():
    assert _player_name({"name": "Auston Matthews"}) == "Auston Matthews"


def test_names_fall_back_to_first_and_last_parts():
    entry = {"firstName": {"default": "Auston"}, "lastName": {"default": "Matthews"}}
    assert _player_name(entry) == "Auston Matthews"


def test_player_id_matches_the_salary_parser():
    from dfs.ingest.salaries import parse_salaries

    record = _skater(name={"default": "Tim Stützle"})
    csv = (
        "Position,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,AvgPointsPerGame\n"
        "C,Tim Stutzle,1,C,6400,OTT@BOS 01/15/2026 07:00PM ET,OTT,13.2\n"
    )
    assert parse_salaries(csv, "NHL", "DK")[0]["player_id"] == record["player_id"]


def test_both_skater_groups_and_goalies_are_read():
    side = {
        "forwards": [{"name": {"default": "Fwd"}, "position": "C", "toi": "18:00", "goals": 1}],
        "defense": [{"name": {"default": "Def"}, "position": "D", "toi": "22:00", "blockedShots": 4}],
        "goalies": [{"name": {"default": "Gee"}, "toi": "60:00",
                     "saveShotsAgainst": "30/32", "goalsAgainst": 2, "decision": "W"}],
    }
    records = _side_records(side, CONTEXT)

    assert len(records) == 3
    assert {record["positions"][0] for record in records} == {"C", "D", "G"}


def test_the_alternate_spelling_of_the_defence_group_is_read():
    """Feeds have used both 'defense' and 'defensemen'."""

    side = {"defensemen": [{"name": {"default": "Def"}, "position": "D", "toi": "20:00"}]}
    assert len(_side_records(side, CONTEXT)) == 1


# --- scheduling -------------------------------------------------------

def test_only_finished_games_are_collected(monkeypatch):
    import datetime as dt

    payload = {"gameWeek": [{"date": "2026-01-15", "games": [
        {"id": 1, "gameState": "OFF"},
        {"id": 2, "gameState": "FINAL"},
        {"id": 3, "gameState": "LIVE"},
        {"id": 4, "gameState": "FUT"},
    ]}]}
    monkeypatch.setattr("dfs.ingest.stats.nhl._get", lambda url, timeout=45: payload)

    games = fetch_schedule(dt.date(2026, 1, 15), dt.date(2026, 1, 15))
    assert sorted(game["game_id"] for game in games) == [1, 2]


def test_games_outside_the_window_are_ignored(monkeypatch):
    """The endpoint returns a whole week, which can overrun the window."""

    import datetime as dt

    payload = {"gameWeek": [
        {"date": "2026-01-15", "games": [{"id": 1, "gameState": "OFF"}]},
        {"date": "2026-01-20", "games": [{"id": 2, "gameState": "OFF"}]},
    ]}
    monkeypatch.setattr("dfs.ingest.stats.nhl._get", lambda url, timeout=45: payload)

    games = fetch_schedule(dt.date(2026, 1, 15), dt.date(2026, 1, 16))
    assert [game["game_id"] for game in games] == [1]


def test_a_game_returned_by_two_weeks_is_collected_once(monkeypatch):
    import datetime as dt

    payload = {"gameWeek": [{"date": "2026-01-15", "games": [{"id": 1, "gameState": "OFF"}]}]}
    monkeypatch.setattr("dfs.ingest.stats.nhl._get", lambda url, timeout=45: payload)

    games = fetch_schedule(dt.date(2026, 1, 15), dt.date(2026, 1, 29))
    assert len(games) == 1


def test_a_since_date_shortens_the_window():
    import datetime as dt

    start, end = game_dates(days=90, end=dt.date(2026, 1, 31), since="2026-01-28")
    assert start == dt.date(2026, 1, 28)
    assert end == dt.date(2026, 1, 31)


def test_nhl_is_registered():
    assert has_collector("NHL")


@needs_network
def test_the_live_schedule_returns_finished_games():
    import datetime as dt

    games = fetch_schedule(dt.date(2025, 1, 15), dt.date(2025, 1, 16))
    assert games
    assert all("game_id" in game and "game_date" in game for game in games)


@needs_network
def test_a_live_load_stores_skaters_and_goalies(tmp_path):
    """Targets a fixed mid-season window rather than the last few days.

    Hockey runs October to June, so a window ending today collects
    nothing for a third of the year -- correctly, but a test that reads
    that as failure is testing the calendar rather than the collector.
    This was found by running it in September, when the honest answer
    was zero games.
    """

    import datetime as dt

    from dfs.ingest.stats import load_history

    database = Database(tmp_path / "nhl.db")
    summary = load_history("NHL", database, days=3, end=dt.date(2025, 1, 16))

    assert summary["logs"] > 50

    positions = {
        row["positions"]
        for row in database.connection.execute(
            "SELECT DISTINCT positions FROM players WHERE sport='NHL'"
        )
    }
    assert '["G"]' in positions
    assert positions & {'["C"]', '["W"]', '["D"]'}


@needs_network
def test_an_out_of_season_window_collects_nothing_without_failing():
    """The offseason is a legitimate empty answer, not an error."""

    import datetime as dt

    from dfs.ingest.stats.nhl import fetch_schedule

    # Early August: no NHL of any kind.
    assert fetch_schedule(dt.date(2025, 8, 1), dt.date(2025, 8, 5)) == []


# --- orchestration ----------------------------------------------------

def _boxscore_payload(game_date="2026-01-15"):
    return {
        "awayTeam": {"abbrev": "BOS"},
        "homeTeam": {"abbrev": "TOR"},
        "gameDate": game_date,
        "playerByGameStats": {
            "awayTeam": {
                "forwards": [{"name": {"default": "Away Fwd"}, "position": "C",
                              "toi": "18:00", "goals": 1, "sog": 3}],
                "goalies": [],
            },
            "homeTeam": {
                "forwards": [],
                "goalies": [{"name": {"default": "Home Goalie"}, "toi": "60:00",
                             "saveShotsAgainst": "30/32", "goalsAgainst": 2,
                             "decision": "W"}],
            },
        },
    }


def _stub_api(monkeypatch, payload_date="2026-01-15"):
    def fake_get(url, timeout=45):
        if "/schedule/" in url:
            return {"gameWeek": [{"date": "2026-01-15", "games": [
                {"id": 2026020001, "gameState": "OFF"},
            ]}]}
        return _boxscore_payload(payload_date)

    monkeypatch.setattr("dfs.ingest.stats.nhl._get", fake_get)


def test_a_full_load_writes_skaters_and_goalies(monkeypatch, tmp_path):
    import datetime as dt

    from dfs.ingest.stats import load_history

    _stub_api(monkeypatch)
    database = Database(tmp_path / "nhl.db")
    summary = load_history(
        "NHL", database,
        days=(dt.date.today() - dt.date(2026, 1, 15)).days,
    )

    assert summary["logs"] == 2
    assert summary["failed_games"] == 0


def test_a_timestamped_game_date_is_trimmed_to_a_date(monkeypatch, tmp_path):
    """A stored timestamp would never match a slate date, and a log that
    does not match reads as 'did not play' rather than as an error."""

    import datetime as dt

    from dfs.ingest.stats import load_history

    _stub_api(monkeypatch, payload_date="2026-01-15T19:00:00Z")
    database = Database(tmp_path / "nhl.db")
    load_history("NHL", database, days=(dt.date.today() - dt.date(2026, 1, 15)).days)

    dates = {
        row["game_date"]
        for row in database.connection.execute("SELECT DISTINCT game_date FROM game_logs")
    }
    assert dates == {"2026-01-15"}


def test_a_window_with_no_hockey_is_not_an_error(monkeypatch, tmp_path):
    from dfs.ingest.stats import load_history

    monkeypatch.setattr("dfs.ingest.stats.nhl._get", lambda url, timeout=45: {"gameWeek": []})
    database = Database(tmp_path / "nhl.db")
    summary = load_history("NHL", database, days=3)

    assert summary["logs"] == 0
    assert summary["games"] == 0


def test_a_wholly_failed_run_raises_rather_than_reporting_nothing(monkeypatch, tmp_path):
    """A dead endpoint must not look like an off day in the schedule."""

    from dfs.ingest.stats import CollectorError
    from dfs.ingest.stats.nhl import load_nhl_history

    def fake_get(url, timeout=45):
        if "/schedule/" in url:
            return {"gameWeek": [{"date": "2026-01-15", "games": [
                {"id": 1, "gameState": "OFF"},
            ]}]}
        raise CollectorError("boom")

    monkeypatch.setattr("dfs.ingest.stats.nhl._get", fake_get)

    import datetime as dt

    with pytest.raises(CollectorError, match="api-web.nhle.com"):
        load_nhl_history(
            Database(tmp_path / "nhl.db"),
            days=(dt.date.today() - dt.date(2026, 1, 15)).days,
        )


def test_a_load_records_a_collector_run(monkeypatch, tmp_path):
    import datetime as dt

    from dfs.ingest.stats import is_stale, load_history

    _stub_api(monkeypatch)
    database = Database(tmp_path / "nhl.db")
    assert is_stale(database, "NHL")

    load_history("NHL", database, days=(dt.date.today() - dt.date(2026, 1, 15)).days)
    assert not is_stale(database, "NHL")
