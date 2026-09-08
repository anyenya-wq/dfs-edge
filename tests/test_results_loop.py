"""Locking, resolving from collected logs, and the forward/backtest split.

The loop these close: a projection is written, stamped final before its
games, then compared to the box scores that the collectors were already
fetching to train it. No second data source is involved.
"""

from __future__ import annotations

import pytest

from dfs.db.database import Database
from dfs.pipeline import resolve_slate_from_logs
from dfs.projections.calibration import calibration_report, split_forward
from dfs.sports import get_config, score_stat_line


SLATE_DATE = "2026-01-15"
# A double-double, so the DraftKings bonus is exercised and the two
# sites genuinely disagree about what this line is worth.
LINE = {"pts": 30, "reb": 11, "ast": 6, "stl": 2, "blk": 1, "tov": 3, "fg3m": 4}


def _slate_with_pool(database, players=3, sport="NBA", site="DK", slate_date=SLATE_DATE):
    slate_id = database.upsert_slate(sport, site, slate_date)
    database.save_salaries(slate_id, [
        {
            "player_id": f"nba:p{index}",
            "name": f"Player {index}",
            "sport": sport,
            "team": "DAL",
            "opponent": "PHX",
            "positions": ["PG"],
            "roster_positions": ["PG"],
            "salary": 8_000,
        }
        for index in range(players)
    ])
    return slate_id


def _log(database, player_id, game_date=SLATE_DATE, stats=None, opportunity=34.0):
    database.save_game_log({
        "player_id": player_id,
        "sport": "NBA",
        "game_date": game_date,
        "opportunity": opportunity,
        "stats": stats if stats is not None else LINE,
    })


def test_resolution_scores_from_collected_logs(database):
    slate_id = _slate_with_pool(database, players=2)
    for index in range(2):
        _log(database, f"nba:p{index}")

    summary = resolve_slate_from_logs(database, slate_id, refresh=False)

    assert summary["played"] == 2
    assert summary["did_not_play"] == 0
    assert summary["resolved"] == 2


def test_a_player_who_did_not_play_scores_zero_rather_than_vanishing(database):
    """Dropping them would hide the model's worst misses from its own report."""

    slate_id = _slate_with_pool(database, players=3)
    _log(database, "nba:p0")  # only one of the three played

    summary = resolve_slate_from_logs(database, slate_id, refresh=False)

    assert summary["played"] == 1
    assert summary["did_not_play"] == 2
    assert summary["resolved"] == 3

    absent = database.connection.execute(
        "SELECT actual_points FROM actuals WHERE player_id = 'nba:p1'"
    ).fetchone()
    assert absent["actual_points"] == 0.0


def test_logs_from_another_date_are_not_used(database):
    slate_id = _slate_with_pool(database, players=1)
    _log(database, "nba:p0", game_date="2026-01-14")

    summary = resolve_slate_from_logs(database, slate_id, refresh=False)
    assert summary["played"] == 0
    assert summary["did_not_play"] == 1


def test_resolution_scores_under_the_slate_s_site_rules(database):
    """The same box line must resolve differently on DraftKings and FanDuel."""

    dk = _slate_with_pool(database, players=1, site="DK")
    fd = _slate_with_pool(database, players=1, site="FD")
    _log(database, "nba:p0")

    resolve_slate_from_logs(database, dk, refresh=False)
    resolve_slate_from_logs(database, fd, refresh=False)

    dk_points = database.connection.execute(
        "SELECT actual_points FROM actuals WHERE slate_id = ?", (dk,)
    ).fetchone()["actual_points"]
    fd_points = database.connection.execute(
        "SELECT actual_points FROM actuals WHERE slate_id = ?", (fd,)
    ).fetchone()["actual_points"]

    assert dk_points != fd_points
    # Compared against the scorer rather than a hand-computed total, so
    # the test checks that resolution used the site's rules rather than
    # re-deriving them (and getting the bonus boundary wrong).
    assert dk_points == score_stat_line(LINE, ["PG"], get_config("NBA", "DK"))
    assert fd_points == score_stat_line(LINE, ["PG"], get_config("NBA", "FD"))

    # DraftKings alone pays the double-double bonus this line earns.
    raw = get_config("NBA", "DK").score_stat_line(LINE, ["PG"])
    assert dk_points == raw + 1.5


def test_an_unknown_slate_is_rejected(database):
    with pytest.raises(ValueError, match="No slate"):
        resolve_slate_from_logs(database, 999, refresh=False)


def test_resolution_is_repeatable(database):
    slate_id = _slate_with_pool(database, players=2)
    for index in range(2):
        _log(database, f"nba:p{index}")

    first = resolve_slate_from_logs(database, slate_id, refresh=False)
    second = resolve_slate_from_logs(database, slate_id, refresh=False)

    assert first == second
    stored = database.connection.execute(
        "SELECT COUNT(*) FROM actuals WHERE slate_id = ?", (slate_id,)
    ).fetchone()[0]
    assert stored == 2


def _resolved_record(database, slate_id, created_at):
    """Write a locked projection with a controlled creation time."""

    database.save_projection(slate_id, {
        "player_id": "nba:p0", "projected_points": 40.0,
    })
    database.connection.execute(
        "UPDATE projections SET created_at = ?, locked_at = CURRENT_TIMESTAMP "
        "WHERE slate_id = ?",
        (created_at, slate_id),
    )
    database.connection.commit()


def test_a_projection_made_before_the_games_is_forward(database):
    slate_id = _slate_with_pool(database, players=1)
    _log(database, "nba:p0")
    _resolved_record(database, slate_id, f"{SLATE_DATE} 17:30:00")
    resolve_slate_from_logs(database, slate_id, refresh=False)

    record = database.resolved_projections()[0]
    assert record["forward"] == 1


def test_a_projection_reconstructed_afterwards_is_not_forward(database):
    slate_id = _slate_with_pool(database, players=1)
    _log(database, "nba:p0")
    _resolved_record(database, slate_id, "2026-02-01 12:00:00")
    resolve_slate_from_logs(database, slate_id, refresh=False)

    record = database.resolved_projections()[0]
    assert record["forward"] == 0


def test_the_split_separates_the_two_records():
    records = [
        {"forward": 1, "projected_points": 10, "actual_points": 12},
        {"forward": 0, "projected_points": 10, "actual_points": 8},
        {"forward": 0, "projected_points": 10, "actual_points": 9},
    ]
    forward, backtest = split_forward(records)
    assert len(forward) == 1
    assert len(backtest) == 2


def test_a_pure_backtest_says_so_rather_than_claiming_a_record():
    """The number looks like a result; the verdict must say it is not one."""

    records = [
        {"forward": 0, "projected_points": 20, "actual_points": 21,
         "site_avg_points": 25, "positions": ["PG"]}
        for _ in range(50)
    ]
    report = calibration_report(records)

    assert report["forward"] is None
    assert report["backtest"]["count"] == 50
    assert "reconstructed after the games" in report["forward_verdict"]


def test_a_thin_forward_record_is_not_read_as_a_result():
    records = [
        {"forward": 1, "projected_points": 20, "actual_points": 21,
         "site_avg_points": 25, "positions": ["PG"]}
        for _ in range(20)
    ]
    assert "too few to read" in calibration_report(records)["forward_verdict"]


def test_a_substantial_forward_record_is_compared_to_the_backtest():
    """The comparison that answers whether pre-lock research pays."""

    import random

    generator = random.Random(5)
    records = []
    for index in range(600):
        true = generator.uniform(10, 40)
        forward = index < 300
        # Forward projections are deliberately the more accurate half.
        noise = 2.0 if forward else 5.0
        records.append({
            "forward": 1 if forward else 0,
            "positions": ["PG"],
            "projected_points": true + generator.gauss(0, noise),
            "site_avg_points": true + generator.gauss(0, 9),
            "actual_points": max(true + generator.gauss(0, 6), 0),
        })

    report = calibration_report(records)
    assert report["forward"]["skill"] > report["backtest"]["skill"]
    assert "above the backtest" in report["forward_verdict"]


def test_an_empty_record_produces_no_forward_verdict():
    assert calibration_report([])["forward_verdict"] == ""


def test_a_past_slate_is_scorable():
    from dfs.pipeline import slate_is_scorable

    assert slate_is_scorable("2026-01-15", today="2026-09-01")


def test_todays_slate_is_scorable_the_same_evening():
    """Requiring tomorrow would leave the record permanently a day behind."""

    from dfs.pipeline import slate_is_scorable

    assert slate_is_scorable("2026-09-01", today="2026-09-01")


def test_a_future_slate_is_not_scorable():
    from dfs.pipeline import slate_is_scorable

    assert not slate_is_scorable("2026-09-08", today="2026-09-01")


# ----------------------------------------------------------------------
# Unattended resolution: what a scheduled run must and must not do
# ----------------------------------------------------------------------


def _project(database, slate_id, players=2):
    for index in range(players):
        database.save_projection(
            slate_id, {"player_id": f"nba:p{index}", "projected_points": 30.0}
        )


def test_an_unlocked_slate_is_not_awaiting_resolution(database):
    """An unlocked projection is a draft, not a forecast on the record."""

    slate_id = _slate_with_pool(database, players=2)
    _project(database, slate_id)

    assert database.slates_awaiting_resolution() == []


def test_a_locked_unscored_slate_is_awaiting_resolution(database):
    slate_id = _slate_with_pool(database, players=2)
    _project(database, slate_id)
    database.lock_slate(slate_id)

    due = database.slates_awaiting_resolution()

    assert [slate["id"] for slate in due] == [slate_id]


def test_a_slate_already_scored_is_not_offered_again(database):
    slate_id = _slate_with_pool(database, players=2)
    _project(database, slate_id)
    database.lock_slate(slate_id)
    for index in range(2):
        _log(database, f"nba:p{index}")
    resolve_slate_from_logs(database, slate_id, refresh=False)

    assert database.slates_awaiting_resolution() == []


def test_a_backlog_is_offered_oldest_first(database):
    """Worked in the order it accumulated, so the record fills in order."""

    later = _slate_with_pool(database, players=1, slate_date="2026-01-20")
    earlier = _slate_with_pool(database, players=1, slate_date="2026-01-10")
    for slate_id in (later, earlier):
        _project(database, slate_id, players=1)
        database.lock_slate(slate_id)

    due = database.slates_awaiting_resolution()

    assert [slate["id"] for slate in due] == [earlier, later]


def test_no_logs_for_a_date_is_distinguishable_from_nobody_playing(database):
    """The check that stops a scheduled run recording a slate of zeros.

    Resolution cannot tell the two apart -- a player with no log scores
    zero either way -- so the caller has to ask before scoring.
    """

    slate_id = _slate_with_pool(database, players=2)

    assert database.game_log_count("NBA", SLATE_DATE) == 0

    _log(database, "nba:p0")

    assert database.game_log_count("NBA", SLATE_DATE) == 1
    assert database.game_log_count("NBA", "2026-01-16") == 0
    assert database.game_log_count("NHL", SLATE_DATE) == 0
    assert slate_id  # the pool exists; only the logs are in question


def _run_due(database, monkeypatch, tmp_path):
    """Drive `dfs resolve --due` against an already-open database."""

    import argparse

    from dfs import cli

    monkeypatch.setattr(cli, "Database", lambda _path: database)
    monkeypatch.setattr(database, "close", lambda: None)
    # The collectors are the network; the point here is the decision the
    # command makes about what to score, not where logs come from.
    monkeypatch.setattr(cli, "refresh_history", lambda *a, **k: None)

    return cli._resolve_due(argparse.Namespace(db=tmp_path / "unused.db"))


def test_a_due_run_does_not_score_a_slate_whose_box_scores_are_missing(
    database, monkeypatch, tmp_path, capsys
):
    """The failure this guard exists for.

    Scoring early records every player as a zero, and the slate then has
    results, so nothing ever revisits it: one premature run turns a real
    forecast into a permanent row saying the model predicted 30 points
    for players who scored nothing.
    """

    import datetime as dt

    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    slate_id = _slate_with_pool(database, players=2, slate_date=yesterday)
    _project(database, slate_id)
    database.lock_slate(slate_id)

    exit_code = _run_due(database, monkeypatch, tmp_path)

    # Waiting a night for box scores is ordinary, so the run passes.
    assert exit_code == 0
    assert "leaving it for the next run" in capsys.readouterr().out
    # Still due, and still unscored.
    assert [slate["id"] for slate in database.slates_awaiting_resolution()] == [slate_id]


def test_a_slate_still_unscorable_long_after_its_games_is_a_failure(
    database, monkeypatch, tmp_path, capsys
):
    """A week without box scores is a broken collector, not a wait."""

    slate_id = _slate_with_pool(database, players=2, slate_date="2026-01-10")
    _project(database, slate_id)
    database.lock_slate(slate_id)

    exit_code = _run_due(database, monkeypatch, tmp_path)

    assert exit_code == 1
    assert "days after the slate" in capsys.readouterr().out
    assert [slate["id"] for slate in database.slates_awaiting_resolution()] == [slate_id]


def test_a_due_run_scores_a_slate_once_its_box_scores_arrive(
    database, monkeypatch, tmp_path, capsys
):
    slate_id = _slate_with_pool(database, players=2, slate_date="2026-01-10")
    _project(database, slate_id)
    database.lock_slate(slate_id)
    for index in range(2):
        _log(database, f"nba:p{index}", game_date="2026-01-10")

    exit_code = _run_due(database, monkeypatch, tmp_path)

    assert exit_code == 0
    assert "resolved 2 of 2" in capsys.readouterr().out
    assert database.slates_awaiting_resolution() == []


def test_a_due_run_leaves_a_slate_whose_games_have_not_been_played(
    database, monkeypatch, tmp_path, capsys
):
    slate_id = _slate_with_pool(database, players=2, slate_date="2099-01-01")
    _project(database, slate_id)
    database.lock_slate(slate_id)

    exit_code = _run_due(database, monkeypatch, tmp_path)

    assert exit_code == 0
    assert "still in play" in capsys.readouterr().out
    assert [slate["id"] for slate in database.slates_awaiting_resolution()] == [slate_id]
