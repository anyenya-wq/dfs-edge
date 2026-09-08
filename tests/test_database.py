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


def test_a_slate_can_be_found_without_being_created(database):
    """`upsert_slate` would create one, which hides the question asked."""

    assert database.find_slate("NBA", "DK", "2026-01-15") is None

    slate = database.upsert_slate("NBA", "DK", "2026-01-15")

    assert database.find_slate("NBA", "DK", "2026-01-15") == slate


def test_only_slates_with_a_pool_are_offered(database):
    """A slate row with no salaries has nothing to show."""

    empty = database.upsert_slate("NBA", "DK", "2026-01-15")
    filled = database.upsert_slate("NFL", "DK", "2026-09-14")
    database.save_salaries(filled, _players("nfl:", 3, sport="NFL"))

    offered = database.slates_with_players()

    assert [row["id"] for row in offered] == [filled]
    assert offered[0]["players"] == 3
    assert empty not in {row["id"] for row in offered}


def test_slates_from_several_sports_and_sites_coexist(database):
    """The whole point of uploading more than one file.

    Each sport-site-date is its own slate, so a Sunday of NFL on both
    sites and an MLB slate the same evening are three pools that do not
    overwrite one another.
    """

    for sport, site, date in (
        ("NFL", "DK", "2026-09-14"),
        ("NFL", "FD", "2026-09-14"),
        ("MLB", "DK", "2026-09-14"),
    ):
        slate = database.upsert_slate(sport, site, date)
        database.save_salaries(slate, _players(f"{sport.lower()}:", 4, sport=sport))

    offered = database.slates_with_players()

    assert len(offered) == 3
    assert {(row["sport"], row["site"]) for row in offered} == {
        ("NFL", "DK"), ("NFL", "FD"), ("MLB", "DK"),
    }
    assert all(row["players"] == 4 for row in offered)


def test_a_slate_reports_what_it_is(database):
    slate = database.upsert_slate("MLB", "FD", "2026-04-10")

    stored = database.slate(slate)

    assert (stored["sport"], stored["site"], stored["slate_date"]) == (
        "MLB", "FD", "2026-04-10",
    )
    assert database.slate(9_999) is None


def test_a_reset_reports_what_it_would_destroy(database):
    """Counted before deleting, because the caller cannot see inside."""

    slate = database.upsert_slate("MLB", "DK", "2026-09-08")
    database.save_salaries(slate, _players("mlb:", 4, sport="MLB"))

    counts = database.row_counts()

    assert counts["salaries"] == 4
    assert counts["slates"] == 1
    assert counts["players"] == 4
    # Reporting must not itself change anything.
    assert database.row_counts() == counts


def test_a_reset_empties_every_table_and_says_what_it_removed(database):
    slate = database.upsert_slate("MLB", "DK", "2026-09-08")
    database.save_salaries(slate, _players("mlb:", 4, sport="MLB"))
    database.save_projection(slate, {"player_id": "mlb:0", "projected_points": 9.0})

    removed = database.reset()

    assert removed["salaries"] == 4
    assert removed["projections"] == 1
    assert set(database.row_counts().values()) == {0}


def test_the_database_still_works_after_a_reset(database):
    """Rows go, tables stay. A dropped schema would need recreating."""

    slate = database.upsert_slate("NFL", "DK", "2026-09-14")
    database.save_salaries(slate, _players("nfl:", 2, sport="NFL"))
    database.reset()

    fresh = database.upsert_slate("NFL", "DK", "2026-09-21")
    database.save_salaries(fresh, _players("nfl:", 3, sport="NFL"))

    assert len(database.player_pool(fresh)) == 3


def test_resetting_an_empty_database_is_not_an_error(database):
    assert set(database.reset().values()) == {0}


def test_batched_logs_match_fetching_them_one_at_a_time(database):
    """The batched read must be a pure speed-up, not a different answer.

    It replaced a per-player fetch that cost one network round trip per
    player -- 942 of them for a real MLB pool. Same rows, same order,
    same cutoff, or the speed is worthless.
    """

    ids = [f"mlb:p{index}" for index in range(6)]
    for player_id in ids:
        database.upsert_player({
            "player_id": player_id, "name": player_id,
            "sport": "MLB", "positions": ["OF"],
        })
        for day in range(1, 13):
            database.save_game_log({
                "player_id": player_id, "sport": "MLB",
                "game_date": f"2026-05-{day:02d}", "opportunity": 4,
                "stats": {"single": day},
            })

    for before in (None, "2026-05-07"):
        batched = database.game_logs_for(ids, before=before, limit=5)
        for player_id in ids:
            one_at_a_time = database.game_logs(player_id, before=before, limit=5)
            assert batched[player_id] == one_at_a_time, (player_id, before)


def test_the_per_player_limit_is_applied_per_player(database):
    """Not a limit on the whole result, which would starve later players."""

    for index in range(3):
        player_id = f"nba:p{index}"
        database.upsert_player({
            "player_id": player_id, "name": player_id,
            "sport": "NBA", "positions": ["PG"],
        })
        for day in range(1, 11):
            database.save_game_log({
                "player_id": player_id, "sport": "NBA",
                "game_date": f"2026-03-{day:02d}", "opportunity": 30,
                "stats": {"pts": day},
            })

    logs = database.game_logs_for([f"nba:p{i}" for i in range(3)], limit=4)

    assert [len(rows) for rows in logs.values()] == [4, 4, 4]


def test_a_player_with_no_logs_still_gets_an_entry(database):
    """The caller indexes by id; a missing key would be a KeyError on a
    debut, which is exactly when it must not crash."""

    logs = database.game_logs_for(["nfl:never-played"])

    assert logs == {"nfl:never-played": []}


def test_more_players_than_fit_in_one_statement(database):
    """SQLite caps bound parameters, so the ids are sent in batches."""

    ids = [f"mlb:b{index}" for index in range(Database.ID_BATCH + 25)]
    for player_id in ids[:3]:
        database.upsert_player({
            "player_id": player_id, "name": player_id,
            "sport": "MLB", "positions": ["OF"],
        })
        database.save_game_log({
            "player_id": player_id, "sport": "MLB",
            "game_date": "2026-05-01", "opportunity": 4, "stats": {"single": 1},
        })

    logs = database.game_logs_for(ids)

    assert len(logs) == len(ids)
    assert all(len(logs[player_id]) == 1 for player_id in ids[:3])
    assert logs[ids[400]] == []


def test_a_slate_can_be_moved_to_the_date_its_games_are_played(database):
    """FanDuel's export carries no date, so one has to be supplied.

    A wrong one is not cosmetic. Results are matched to a slate by date,
    so a slate filed under a day its games were not played can never be
    scored -- it sits on the calibration record for ever, and nothing
    errors to say so.
    """

    slate = database.upsert_slate("NFL", "FD", "2026-09-08")
    database.save_salaries(slate, _players("nfl:", 3, sport="NFL"))

    database.set_slate_date(slate, "2026-09-13")

    assert database.slate(slate)["slate_date"] == "2026-09-13"
    # The pool moves with it rather than being orphaned.
    assert len(database.player_pool(slate)) == 3


def test_moving_a_slate_onto_an_occupied_date_is_refused(database):
    """Merging two pools silently would destroy one of them."""

    monday = database.upsert_slate("NFL", "FD", "2026-09-08")
    sunday = database.upsert_slate("NFL", "FD", "2026-09-13")
    database.save_salaries(sunday, _players("nfl:", 2, sport="NFL"))

    with pytest.raises(ValueError, match="already exists"):
        database.set_slate_date(monday, "2026-09-13")

    assert database.slate(monday)["slate_date"] == "2026-09-08"
    assert len(database.player_pool(sunday)) == 2


def test_moving_a_slate_to_the_date_it_already_has_does_nothing(database):
    slate = database.upsert_slate("NFL", "FD", "2026-09-13")

    database.set_slate_date(slate, "2026-09-13")

    assert database.slate(slate)["slate_date"] == "2026-09-13"


def test_moving_a_slate_that_does_not_exist_is_an_error(database):
    with pytest.raises(ValueError, match="No slate"):
        database.set_slate_date(9_999, "2026-09-13")


def test_a_moved_slate_keeps_its_projections(database):
    """The point of moving rather than re-uploading."""

    slate = database.upsert_slate("NFL", "FD", "2026-09-08")
    database.save_salaries(slate, _players("nfl:", 2, sport="NFL"))
    database.save_projections(slate, [
        {"player_id": "nfl:0", "projected_points": 14.0},
        {"player_id": "nfl:1", "projected_points": 11.0},
    ])

    database.set_slate_date(slate, "2026-09-13")

    rows = database.connection.execute(
        "SELECT COUNT(*) AS n FROM projections WHERE slate_id = ?", (slate,)
    ).fetchone()
    assert int(rows["n"]) == 2


def test_a_database_created_before_a_column_existed_gains_it(database):
    """A deployed database holds the collected history and every stored
    slate. Recreating it to pick up a column would throw away the record
    this project exists to build, so it is migrated instead.
    """

    slate = database.upsert_slate("MLB", "DK", "2026-09-08")
    database.save_salaries(slate, _players("mlb:", 2, sport="MLB"))

    # Drop back to the older shape, then re-run the migration.
    for column in ("starting", "batting_order"):
        database.connection.execute(f"ALTER TABLE salaries DROP COLUMN {column}")
    database.connection.commit()
    assert "starting" not in database._columns("salaries")

    database._add_missing_columns("salaries", Database.ADDED_COLUMNS["salaries"])

    assert {"starting", "batting_order"} <= database._columns("salaries")
    # And the rows that were there are still there.
    assert len(database.player_pool(slate)) == 2


def test_migrating_twice_changes_nothing(database):
    before = database._columns("salaries")

    database._add_missing_columns("salaries", Database.ADDED_COLUMNS["salaries"])
    database._add_missing_columns("salaries", Database.ADDED_COLUMNS["salaries"])

    assert database._columns("salaries") == before


def test_the_pool_returns_what_the_site_said_about_availability(database):
    slate = database.upsert_slate("MLB", "DK", "2026-09-08")
    database.save_salaries(slate, [
        {
            "player_id": "mlb:sp", "name": "Announced", "sport": "MLB",
            "positions": ["SP"], "roster_positions": ["P"], "salary": 11_000,
            "starting": "SP", "batting_order": None, "injury_status": None,
        },
        {
            "player_id": "mlb:bat", "name": "Leads Off", "sport": "MLB",
            "positions": ["OF"], "roster_positions": ["OF"], "salary": 5_000,
            "starting": "1", "batting_order": 1, "injury_status": None,
        },
    ])

    by_name = {row["name"]: row for row in database.player_pool(slate)}

    assert by_name["Announced"]["starting"] == "SP"
    assert by_name["Announced"]["batting_order"] is None
    assert by_name["Leads Off"]["batting_order"] == 1
