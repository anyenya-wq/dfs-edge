"""The MLB StatsAPI collector: two scoring tables, and innings notation.

Parsing is tested against fixture responses shaped like StatsAPI's, so
the suite is fast and does not depend on MLB being reachable. A live
test is gated behind DFS_NETWORK_TESTS.

Worth knowing when reading these: unlike the NFL and NBA collectors,
this one could not be exercised against the real endpoint while it was
written, because statsapi.mlb.com is unreachable from the environment it
was built in. The fixtures below are therefore doing more work than
usual, and the gated live test is the one that confirms the shape is
right.
"""

from __future__ import annotations

import os

import pytest

from dfs.db.database import Database
from dfs.ingest.stats import has_collector
from dfs.ingest.stats.mlb import (
    _normalise_position,
    _record_for,
    _side_records,
    fetch_schedule,
    game_dates,
    innings_to_float,
)
from dfs.sports import get_config, score_stat_line

needs_network = pytest.mark.skipif(
    not os.environ.get("DFS_NETWORK_TESTS"),
    reason="set DFS_NETWORK_TESTS=1 to run tests that call statsapi.mlb.com",
)


# --- innings notation -------------------------------------------------

def test_innings_thirds_are_real_thirds():
    """6.1 is six innings and one out, not six and a tenth."""

    assert innings_to_float("6.0") == 6.0
    assert innings_to_float("6.1") == pytest.approx(6.3333, abs=1e-4)
    assert innings_to_float("6.2") == pytest.approx(6.6667, abs=1e-4)
    assert innings_to_float("7.0") == 7.0


def test_innings_handles_whole_numbers_and_junk():
    assert innings_to_float("5") == 5.0
    assert innings_to_float(None) == 0.0
    assert innings_to_float("") == 0.0
    assert innings_to_float("nonsense") == 0.0


def test_a_third_of_an_inning_is_worth_more_than_a_tenth():
    """The bug this guards: scoring pays per inning, so 0.23 innings of
    DraftKings credit would go missing on every start."""

    config = get_config("MLB", "DK")
    naive = score_stat_line({"ip": 6.1}, ["P"], config)
    correct = score_stat_line({"ip": innings_to_float("6.1")}, ["P"], config)
    assert correct > naive


# --- hitters ----------------------------------------------------------

def _batting(**overrides):
    stats = {
        "plateAppearances": 5, "hits": 3, "doubles": 1, "triples": 0,
        "homeRuns": 1, "rbi": 4, "runs": 2, "baseOnBalls": 0,
        "hitByPitch": 0, "stolenBases": 1,
    }
    stats.update(overrides)
    return stats


def _hitter(position="LF", **overrides):
    return _record_for(
        "Test Hitter", position, _batting(**overrides), {},
        "BOS", "NYY", "2026-07-04", True, {},
    )


def test_singles_are_derived_from_hits(record=None):
    """The feed reports total hits and the extra-base types separately."""

    record = _hitter()
    assert record["stats"]["single"] == 1  # 3 hits - 1 double - 1 home run


def test_no_singles_key_when_every_hit_was_extra_base():
    record = _hitter(hits=2, doubles=1, homeRuns=1)
    assert "single" not in record["stats"]


def test_batting_columns_map_onto_the_scoring_keys():
    record = _hitter()
    assert record["stats"]["hr"] == 1
    assert record["stats"]["rbi"] == 4
    assert record["stats"]["run"] == 2
    assert record["stats"]["sb"] == 1
    assert record["stats"]["double"] == 1


def test_the_mapped_hitting_line_actually_scores():
    record = _hitter()
    points = score_stat_line(record["stats"], ["OF"], get_config("MLB", "DK"))
    # single 3 + double 5 + hr 10 + rbi 4x2 + runs 2x2 + sb 5
    assert points == pytest.approx(3 + 5 + 10 + 8 + 4 + 5)


def test_plate_appearances_are_the_hitter_s_opportunity():
    assert _hitter()["opportunity"] == 5


def test_outfield_positions_collapse_to_of():
    """Both sites list all three outfield spots as OF."""

    for position in ("LF", "CF", "RF", "OF"):
        assert _hitter(position=position)["positions"] == ["OF"]


def test_designated_and_pinch_hitters_become_util():
    for position in ("DH", "PH", "PR"):
        assert _normalise_position(position, is_pitcher=False) == "UTIL"


def test_infield_positions_are_left_alone():
    for position in ("C", "1B", "2B", "3B", "SS"):
        assert _normalise_position(position, is_pitcher=False) == position


# --- pitchers ---------------------------------------------------------

def _pitching(**overrides):
    stats = {
        "inningsPitched": "6.1", "strikeOuts": 9, "earnedRuns": 1,
        "hits": 4, "baseOnBalls": 2, "hitBatsmen": 0,
    }
    stats.update(overrides)
    return stats


def _pitcher(entry=None, **overrides):
    return _record_for(
        "Test Pitcher", "P", {}, _pitching(**overrides),
        "BOS", "NYY", "2026-07-04", True, entry or {},
    )


def test_pitching_columns_map_onto_the_alternate_table():
    record = _pitcher()
    assert record["stats"]["k"] == 9
    assert record["stats"]["er"] == 1
    assert record["stats"]["hit_allowed"] == 4
    assert record["stats"]["bb_allowed"] == 2
    # Stored rounded to three decimals, as every stat is.
    assert record["stats"]["ip"] == pytest.approx(6.333, abs=1e-3)


def test_innings_pitched_are_the_pitcher_s_opportunity():
    assert _pitcher()["opportunity"] == pytest.approx(6.333, abs=1e-3)


def test_a_pitcher_is_scored_on_the_pitching_table():
    """The same numbers must not be read as a hitting line."""

    record = _pitcher()
    config = get_config("MLB", "DK")
    as_pitcher = score_stat_line(record["stats"], ["P"], config)
    as_hitter = score_stat_line(record["stats"], ["OF"], config)

    assert as_pitcher > 20
    assert as_hitter == 0.0


def test_a_win_is_read_from_the_counter():
    assert _pitcher(wins=1)["stats"]["win"] == 1.0


def test_a_win_is_read_from_the_decision_note():
    """The feed records the decision either way depending on endpoint."""

    record = _pitcher(entry={"note": "(W, 12-4)"})
    assert record["stats"]["win"] == 1.0


def test_a_loss_note_is_not_read_as_a_win():
    record = _pitcher(entry={"note": "(L, 3-9)"})
    assert "win" not in record["stats"]


def test_a_complete_game_shutout_records_both_bonuses():
    record = _pitcher(completeGames=1, shutouts=1)
    assert record["stats"]["complete_game"] == 1.0
    assert record["stats"]["complete_game_shutout"] == 1.0


def test_a_complete_game_without_a_shutout_records_only_one():
    record = _pitcher(completeGames=1, shutouts=0)
    assert record["stats"]["complete_game"] == 1.0
    assert "complete_game_shutout" not in record["stats"]


def test_a_pitcher_who_did_not_appear_has_no_record():
    assert _record_for("Bench Arm", "P", {}, {}, "BOS", "NYY", "2026-07-04", True, {}) is None


# --- box score assembly ----------------------------------------------

def _boxscore_side(abbreviation, players):
    return {"team": {"abbreviation": abbreviation}, "players": players}


def test_both_teams_are_read_with_the_right_opponent():
    away = _boxscore_side("BOS", {
        "ID1": {"person": {"fullName": "Away Hitter"}, "position": {"abbreviation": "1B"},
                "stats": {"batting": _batting()}},
    })
    home = _boxscore_side("NYY", {
        "ID2": {"person": {"fullName": "Home Pitcher"}, "position": {"abbreviation": "P"},
                "stats": {"pitching": _pitching()}},
    })

    away_records = _side_records(away, home, "2026-07-04", home=False)
    home_records = _side_records(home, away, "2026-07-04", home=True)

    assert away_records[0]["team"] == "BOS"
    assert away_records[0]["opponent"] == "NYY"
    assert away_records[0]["home"] is False
    assert home_records[0]["team"] == "NYY"
    assert home_records[0]["opponent"] == "BOS"
    assert home_records[0]["home"] is True


def test_players_without_a_name_are_skipped():
    side = _boxscore_side("BOS", {
        "ID1": {"person": {}, "position": {"abbreviation": "1B"},
                "stats": {"batting": _batting()}},
    })
    assert _side_records(side, side, "2026-07-04", home=True) == []


def test_player_id_matches_the_salary_parser():
    from dfs.ingest.salaries import parse_salaries

    record = _record_for(
        "Ronald Acuña Jr.", "RF", _batting(), {}, "ATL", "NYM", "2026-07-04", True, {},
    )
    csv = (
        "Position,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,AvgPointsPerGame\n"
        "OF,Ronald Acuna Jr.,1,OF,5200,ATL@NYM 07/04/2026 07:10PM ET,ATL,9.8\n"
    )
    assert parse_salaries(csv, "MLB", "DK")[0]["player_id"] == record["player_id"]


# --- scheduling and windows ------------------------------------------

def test_only_finished_games_are_collected(monkeypatch):
    """An in-progress game would be stored as a completed night."""

    payload = {"dates": [{"date": "2026-07-04", "games": [
        {"gamePk": 1, "status": {"abstractGameState": "Final"}},
        {"gamePk": 2, "status": {"abstractGameState": "Live"}},
        {"gamePk": 3, "status": {"abstractGameState": "Preview"}},
    ]}]}
    monkeypatch.setattr("dfs.ingest.stats.mlb._get", lambda url, timeout=45: payload)

    games = fetch_schedule("2026-07-04", "2026-07-04")
    assert [game["game_pk"] for game in games] == [1]


def test_the_window_covers_the_requested_days():
    import datetime as dt

    start, end = game_dates(days=30, end=dt.date(2026, 7, 31))
    assert start == "2026-07-01"
    assert end == "2026-07-31"


def test_a_since_date_shortens_the_window():
    """What makes a refresh cost sixteen requests instead of thousands."""

    import datetime as dt

    start, _ = game_dates(days=90, end=dt.date(2026, 7, 31), since="2026-07-28")
    assert start == "2026-07-28"


def test_a_since_date_before_the_window_does_not_widen_it():
    import datetime as dt

    start, _ = game_dates(days=10, end=dt.date(2026, 7, 31), since="2026-01-01")
    assert start == "2026-07-21"


def test_mlb_is_registered():
    assert has_collector("MLB")


@needs_network
def test_the_live_schedule_returns_finished_games():
    games = fetch_schedule("2025-07-04", "2025-07-05")
    assert games
    assert all("game_pk" in game and "game_date" in game for game in games)


@needs_network
def test_a_live_load_stores_hitters_and_pitchers(tmp_path):
    from dfs.ingest.stats import load_history

    import datetime as dt

    # A fixed in-season window. Baseball runs March to October, so a
    # window ending today collects nothing for months of the year --
    # correctly, but a test reading that as failure measures the
    # calendar rather than the collector.
    database = Database(tmp_path / "mlb.db")
    summary = load_history("MLB", database, days=2, end=dt.date(2025, 7, 5))

    assert summary["logs"] > 100

    positions = {
        row["positions"]
        for row in database.connection.execute("SELECT DISTINCT positions FROM players WHERE sport='MLB'")
    }
    assert '["P"]' in positions


# --- orchestration ----------------------------------------------------

def _stub_api(monkeypatch, days=2, games_per_day=2):
    """Stand in for StatsAPI with schedule and box-score responses."""

    import datetime as dt

    def fake_get(url, timeout=45):
        if "schedule" in url:
            start = url.split("startDate=")[1].split("&")[0]
            end = url.split("endDate=")[1]
            first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
            dates, game_pk = [], 100
            day = last - dt.timedelta(days=days - 1)
            while day <= last:
                if day >= first:
                    dates.append({
                        "date": day.isoformat(),
                        "games": [
                            {"gamePk": game_pk + index, "status": {"abstractGameState": "Final"}}
                            for index in range(games_per_day)
                        ],
                    })
                    game_pk += games_per_day
                day += dt.timedelta(days=1)
            return {"dates": dates}

        return {"teams": {
            "away": _boxscore_side("BOS", {
                "ID1": {"person": {"fullName": "Away Hitter"},
                        "position": {"abbreviation": "1B"},
                        "stats": {"batting": _batting()}},
            }),
            "home": _boxscore_side("NYY", {
                "ID2": {"person": {"fullName": "Home Pitcher"},
                        "position": {"abbreviation": "P"},
                        "stats": {"pitching": _pitching()}},
            }),
        }}

    monkeypatch.setattr("dfs.ingest.stats.mlb._get", fake_get)


def test_a_full_load_writes_hitters_and_pitchers(monkeypatch, tmp_path):
    from dfs.ingest.stats import load_history

    _stub_api(monkeypatch, days=3, games_per_day=2)
    database = Database(tmp_path / "mlb.db")
    summary = load_history("MLB", database, days=3)

    assert summary["sport"] == "MLB"
    assert summary["games"] == 6
    assert summary["logs"] == 12  # one hitter and one pitcher per game
    assert summary["failed_games"] == 0


def test_a_load_records_a_collector_run(monkeypatch, tmp_path):
    """Without this the refresh cannot tell fresh history from stale."""

    from dfs.ingest.stats import is_stale, load_history

    _stub_api(monkeypatch)
    database = Database(tmp_path / "mlb.db")
    assert is_stale(database, "MLB")

    load_history("MLB", database, days=2)
    assert not is_stale(database, "MLB")


def test_a_day_with_no_baseball_is_not_an_error(monkeypatch, tmp_path):
    from dfs.ingest.stats import load_history

    monkeypatch.setattr("dfs.ingest.stats.mlb._get", lambda url, timeout=45: {"dates": []})
    database = Database(tmp_path / "mlb.db")
    summary = load_history("MLB", database, days=2)

    assert summary["logs"] == 0
    assert summary["games"] == 0


def test_a_wholly_failed_run_raises_rather_than_reporting_nothing(monkeypatch, tmp_path):
    """A dead endpoint must not look like a quiet day with no games."""

    from dfs.ingest.stats import CollectorError
    from dfs.ingest.stats.mlb import load_mlb_history

    def fake_get(url, timeout=45):
        if "schedule" in url:
            return {"dates": [{"date": "2026-07-04", "games": [
                {"gamePk": 1, "status": {"abstractGameState": "Final"}},
            ]}]}
        raise CollectorError("boom")

    monkeypatch.setattr("dfs.ingest.stats.mlb._get", fake_get)

    with pytest.raises(CollectorError, match="statsapi.mlb.com"):
        load_mlb_history(Database(tmp_path / "mlb.db"), days=1)


def test_one_bad_game_does_not_lose_the_rest(monkeypatch, tmp_path):
    from dfs.ingest.stats import CollectorError
    from dfs.ingest.stats.mlb import load_mlb_history

    def fake_get(url, timeout=45):
        if "schedule" in url:
            return {"dates": [{"date": "2026-07-04", "games": [
                {"gamePk": 1, "status": {"abstractGameState": "Final"}},
                {"gamePk": 2, "status": {"abstractGameState": "Final"}},
            ]}]}
        if "/game/2/" in url:
            raise CollectorError("one bad game")
        return {"teams": {
            "away": _boxscore_side("BOS", {
                "ID1": {"person": {"fullName": "Away Hitter"},
                        "position": {"abbreviation": "1B"},
                        "stats": {"batting": _batting()}},
            }),
            "home": _boxscore_side("NYY", {}),
        }}

    monkeypatch.setattr("dfs.ingest.stats.mlb._get", fake_get)
    summary = load_mlb_history(Database(tmp_path / "mlb.db"), days=1)

    assert summary["logs"] == 1
    assert summary["failed_games"] == 1
