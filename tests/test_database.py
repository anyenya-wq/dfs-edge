"""Storage: slate pools, the lock-then-resolve contract, thread safety."""

from __future__ import annotations

import threading

import pytest

from dfs.db.database import Database


def _players(prefix: str, count: int, sport: str = "NBA"):
    return [
        {
            "player_id": f"{prefix}{index}",
            "name": f"{prefix.upper()}{index}",
            "sport": sport,
            "team": "AAA",
            "positions": ["PG"],
            "roster_positions": ["PG"],
            "salary": 5_000 + index,
        }
        for index in range(count)
    ]


def test_slate_upsert_is_idempotent(database):
    first = database.upsert_slate("NBA", "DK", "2026-01-15")
    second = database.upsert_slate("NBA", "DK", "2026-01-15")
    assert first == second


def test_slates_are_separated_by_site(database):
    dk = database.upsert_slate("NBA", "DK", "2026-01-15")
    fd = database.upsert_slate("NBA", "FD", "2026-01-15")
    assert dk != fd


def test_reuploading_replaces_rather_than_merges(database):
    """A salary file is the pool, not an addition to it."""

    slate = database.upsert_slate("NFL", "DK", "2026-09-07")
    database.save_salaries(slate, _players("nba:", 120))
    database.save_salaries(slate, _players("nfl:", 160, sport="NFL"))

    pool = database.player_pool(slate)
    assert len(pool) == 160
    assert not any(player["player_id"].startswith("nba:") for player in pool)


def test_game_logs_can_be_cut_off_before_a_date(database):
    """The guard that stops a backtest seeing the game it is projecting."""

    database.upsert_player(
        {"player_id": "p1", "name": "P", "sport": "NBA", "positions": ["PG"]}
    )
    for day in range(1, 6):
        database.save_game_log(
            {
                "player_id": "p1",
                "sport": "NBA",
                "game_date": f"2026-01-{day:02d}",
                "opportunity": 30,
                "stats": {"pts": 20},
            }
        )

    assert len(database.game_logs("p1")) == 5
    assert len(database.game_logs("p1", before="2026-01-03")) == 2


def test_only_locked_projections_are_scored(database):
    """An unlocked projection is a draft and must not reach calibration."""

    slate = database.upsert_slate("NBA", "DK", "2026-01-15")
    database.save_salaries(slate, _players("p", 1))
    database.save_projection(slate, {"player_id": "p0", "projected_points": 30.0})
    database.save_actual(slate, {"player_id": "p0", "actual_points": 28.0})

    assert database.resolved_projections() == []

    assert database.lock_slate(slate) == 1
    assert len(database.resolved_projections()) == 1


def test_locking_twice_does_not_relock(database):
    slate = database.upsert_slate("NBA", "DK", "2026-01-15")
    database.save_salaries(slate, _players("p", 2))
    for index in range(2):
        database.save_projection(slate, {"player_id": f"p{index}", "projected_points": 10.0})

    assert database.lock_slate(slate) == 2
    assert database.lock_slate(slate) == 0


def test_projection_is_never_overwritten_by_the_result(database):
    """Resolution writes to a separate table; the forecast stands."""

    slate = database.upsert_slate("NBA", "DK", "2026-01-15")
    database.save_salaries(slate, _players("p", 1))
    database.save_projection(slate, {"player_id": "p0", "projected_points": 30.0})
    database.lock_slate(slate)
    database.save_actual(slate, {"player_id": "p0", "actual_points": 5.0})

    row = database.resolved_projections()[0]
    assert row["projected_points"] == 30.0
    assert row["actual_points"] == 5.0


def test_briefs_round_trip(database):
    slate = database.upsert_slate("NBA", "DK", "2026-01-15")
    database.save_salaries(slate, _players("p", 1))
    database.save_brief(
        slate,
        {"player_id": "p0", "status": "Questionable", "situation": "s", "multiplier": None},
    )

    stored = database.briefs(slate)
    assert len(stored) == 1
    # A gated multiplier stores the brief with estimated=0, keeping the
    # written read while recording that no number survived.
    assert stored[0]["estimated"] == 0
    assert stored[0]["multiplier"] is None


def test_lineups_round_trip(database):
    slate = database.upsert_slate("NBA", "DK", "2026-01-15")
    database.save_lineup(
        slate,
        {"mode": "gpp", "total_salary": 49_900, "total_projection": 250.0,
         "players": [{"player_id": "p0", "slot": "PG"}]},
    )
    stored = database.lineups(slate)
    assert stored[0]["players"][0]["slot"] == "PG"


def test_writes_are_safe_across_threads(database):
    """Streamlit runs each script pass on a new thread with a cached handle."""

    errors: list[str] = []

    def work(index: int) -> None:
        try:
            slate = database.upsert_slate("NBA", "DK", f"2026-02-{index:02d}")
            database.save_salaries(slate, _players(f"t{index}-", 20))
        except Exception as error:  # noqa: BLE001 - the test is what it raises
            errors.append(f"{type(error).__name__}: {error}")

    threads = [threading.Thread(target=work, args=(index,)) for index in range(1, 13)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    count = database.connection.execute("SELECT COUNT(*) FROM salaries").fetchone()[0]
    assert count == 12 * 20


def _projection_ids(database, slate: int) -> set[str]:
    rows = database.connection.execute(
        "SELECT player_id FROM projections WHERE slate_id = ?", (slate,)
    ).fetchall()
    return {row["player_id"] for row in rows}


def test_reuploading_drops_projections_for_players_no_longer_in_the_pool(database):
    """A shrunken re-upload left the old pool's projections behind.

    Seen for real: a 560-player DK file reported 971 projections, the
    extra 411 belonging to an earlier, larger upload of the same slate.
    None of them could ever resolve, because resolution walks the
    current pool.
    """

    slate = database.upsert_slate("MLB", "DK", "2026-04-10")
    database.save_salaries(slate, _players("mlb:", 12, sport="MLB"))
    for player in _players("mlb:", 12, sport="MLB"):
        database.save_projection(
            slate, {"player_id": player["player_id"], "projected_points": 9.0}
        )

    database.save_salaries(slate, _players("mlb:", 5, sport="MLB"))

    assert len(database.player_pool(slate)) == 5
    assert _projection_ids(database, slate) == set()


def test_reuploading_keeps_projections_that_were_locked(database):
    """A locked projection is on the calibration record.

    Deleting one would erase a forecast that was genuinely made before
    the games -- a worse fault than a stale count, so locked rows
    survive a re-upload even when the player leaves the pool.
    """

    slate = database.upsert_slate("MLB", "DK", "2026-04-10")
    database.save_salaries(slate, _players("mlb:", 12, sport="MLB"))
    for player in _players("mlb:", 12, sport="MLB"):
        database.save_projection(
            slate, {"player_id": player["player_id"], "projected_points": 9.0}
        )
    database.lock_slate(slate)

    database.save_salaries(slate, _players("mlb:", 5, sport="MLB"))

    assert len(_projection_ids(database, slate)) == 12
