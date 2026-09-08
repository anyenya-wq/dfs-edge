"""Staleness detection, incremental refresh, and batched writes.

The bug these cover is quiet rather than loud: history that loads once
and is never updated does not fail, it just stops being recent, and the
projections built on it get worse without any signal.
"""

from __future__ import annotations

import pytest

from dfs.db.database import Database
from dfs.ingest.stats import is_stale, refresh_history
from dfs.ingest.stats.common import REFRESH_LOOKBACK_DAYS, lookback_from, write_records
from tests.fixtures import unbuilt_sport


def _records(count, sport="NBA", start_day=1):
    return [
        {
            "player_id": f"{sport.lower()}:p{index}",
            "name": f"Player {index}",
            "sport": sport,
            "positions": ["PG"],
            "team": "DAL",
            "opponent": "PHX",
            "game_date": f"2026-01-{start_day + (index % 20):02d}",
            "opportunity": 30.0,
            "stats": {"pts": 20},
        }
        for index in range(count)
    ]


def test_batched_write_stores_players_and_logs(database):
    summary = write_records(database, _records(50), "NBA")
    assert summary["logs"] == 50
    assert summary["players"] == 50


def test_batched_write_is_idempotent(database):
    records = _records(30)
    write_records(database, records, "NBA")
    write_records(database, records, "NBA")

    stored = database.connection.execute("SELECT COUNT(*) FROM game_logs").fetchone()[0]
    assert stored == 30


def test_batched_write_updates_a_corrected_stat_line(database):
    """Box scores get corrected, so a re-read must overwrite."""

    records = _records(1)
    write_records(database, records, "NBA")

    records[0]["stats"] = {"pts": 44}
    records[0]["opportunity"] = 41.0
    write_records(database, records, "NBA")

    log = database.game_logs("nba:p0")[0]
    assert log["stats"]["pts"] == 44
    assert log["opportunity"] == 41.0


def test_writing_nothing_is_harmless(database):
    assert write_records(database, [], "NBA") == {"logs": 0, "players": 0}


def test_latest_game_date_tracks_the_newest_game(database):
    write_records(database, _records(40), "NBA")
    assert database.latest_game_date("NBA") == "2026-01-20"
    assert database.latest_game_date("NFL") is None


def test_a_sport_never_collected_is_stale(database):
    assert is_stale(database, "NBA")


def test_a_sport_just_collected_is_not_stale(database):
    write_records(database, _records(10), "NBA")
    database.record_collector_run("NBA", 10, "2026")
    assert not is_stale(database, "NBA")


def test_staleness_respects_the_age_threshold(database):
    database.record_collector_run("NBA", 10, "2026")
    # A zero-hour threshold makes even a run from this instant stale.
    assert is_stale(database, "NBA", max_age_hours=0)


def test_a_sport_without_a_collector_is_never_stale(database):
    """Nothing can refresh it, so calling it stale would be noise."""

    assert not is_stale(database, unbuilt_sport())


def test_refresh_is_skipped_when_history_is_fresh(database):
    write_records(database, _records(10), "NBA")
    database.record_collector_run("NBA", 10, "2026")
    assert refresh_history(database, "NBA") is None


def test_refresh_of_a_sport_without_a_collector_returns_nothing(database):
    assert refresh_history(database, unbuilt_sport()) is None


def test_the_lookback_starts_before_the_newest_stored_game():
    """Re-reads a few days so corrections and late games are not missed."""

    assert lookback_from("2026-03-10") == "2026-03-07"
    assert REFRESH_LOOKBACK_DAYS == 3


def test_the_lookback_handles_a_missing_or_malformed_date():
    assert lookback_from(None) is None
    assert lookback_from("") is None
    assert lookback_from("not-a-date") is None


def test_the_lookback_crosses_a_month_boundary():
    assert lookback_from("2026-03-02") == "2026-02-27"


def test_recording_a_run_makes_history_fresh(database):
    assert database.hours_since_collection("NBA") is None
    database.record_collector_run("NBA", 100, "2026")
    assert database.hours_since_collection("NBA") == pytest.approx(0, abs=0.01)


def test_collector_runs_table_is_added_to_an_existing_database(tmp_path):
    """Opening an older database must migrate it, not fail."""

    path = tmp_path / "old.db"
    first = Database(path)
    first.connection.execute("DROP TABLE collector_runs")
    first.connection.commit()
    first.close()

    reopened = Database(path)
    assert reopened.hours_since_collection("NBA") is None
    reopened.record_collector_run("NBA", 1, "2026")
    assert reopened.hours_since_collection("NBA") is not None
