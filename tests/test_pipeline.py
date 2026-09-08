"""The end-to-end flow, including the mistakes users actually make."""

from __future__ import annotations

import csv
import io
import random

import pytest

from dfs.db.database import Database
from dfs.pipeline import build_slate, resolve_slate, validate_pool
from dfs.projections.calibration import score_projections
from dfs.sports import get_config


def _salary_csv(sport="NBA", teams=(("DAL", "PHX"), ("DEN", "LAL"), ("BOS", "MIA"), ("GSW", "LAC"))):
    positions = {
        "NBA": ["PG", "SG", "SF", "PF", "C"] * 3,
        "NFL": ["QB", "QB", "RB", "RB", "RB", "RB", "WR", "WR", "WR", "WR",
                "WR", "WR", "TE", "TE", "TE", "DST"],
    }[sport]

    generator = random.Random(9)
    rows = []
    for away, home in teams:
        for team in (away, home):
            for index, position in enumerate(positions):
                salary = generator.randrange(3_000, 11_000, 100)
                rows.append({
                    "Position": position,
                    "Name + ID": f"{team} {position}{index} ({len(rows)})",
                    "Name": f"{team} {position}{index}",
                    "ID": str(len(rows)),
                    "Roster Position": position,
                    "Salary": salary,
                    "Game Info": f"{away}@{home} 01/15/2026 08:00PM ET",
                    "TeamAbbrev": team,
                    "AvgPointsPerGame": round(salary / 1_000 * 3.0, 2),
                })

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def test_a_salary_file_becomes_a_lineup(database):
    result = build_slate(database, _salary_csv(), "NBA", "DK", "2026-01-15")

    assert len(result.lineups) == 1
    assert len(result.lineups[0].players) == result.config.roster_size
    assert result.lineups[0].total_salary <= result.config.salary_cap


def test_projections_are_persisted(database):
    result = build_slate(database, _salary_csv(), "NBA", "DK", "2026-01-15")
    stored = database.connection.execute(
        "SELECT COUNT(*) FROM projections WHERE slate_id = ?", (result.slate_id,)
    ).fetchone()[0]
    assert stored == result.projected_count


def test_lineups_are_persisted(database):
    result = build_slate(
        database, _salary_csv(), "NBA", "DK", "2026-01-15", mode="gpp", lineup_count=3
    )
    assert len(database.lineups(result.slate_id)) == len(result.lineups)


def test_a_players_without_history_fallback_is_reported(database):
    result = build_slate(database, _salary_csv(), "NBA", "DK", "2026-01-15")
    assert any("season average" in warning for warning in result.warnings)


def test_a_gpp_build_produces_distinct_lineups(database):
    result = build_slate(
        database, _salary_csv(), "NBA", "DK", "2026-01-15", mode="gpp", lineup_count=6
    )
    assert len({frozenset(l.player_ids()) for l in result.lineups}) == len(result.lineups)


def test_an_empty_file_is_rejected_clearly(database):
    with pytest.raises(ValueError, match="No players parsed"):
        build_slate(database, "Position,Name,Salary\n", "NBA", "DK", "2026-01-15")


def test_the_wrong_sport_is_named_rather_than_silently_failing(database):
    """The easiest mistake to make and the hardest to diagnose blind."""

    result = build_slate(database, _salary_csv("NBA"), "NFL", "DK", "2026-01-15")

    assert result.lineups == []
    assert any("sport selector" in warning for warning in result.warnings)
    assert any("No legal lineup exists" in warning for warning in result.warnings)


def test_validate_pool_is_quiet_when_the_sport_matches():
    pool = [{"player_id": f"p{i}", "positions": ["PG"]} for i in range(30)]
    assert validate_pool(pool, get_config("NBA", "DK")) == []


def test_validate_pool_reports_an_empty_file():
    assert validate_pool([], get_config("NBA", "DK")) == [
        "The salary file parsed to zero players."
    ]


def test_resolution_scores_box_lines_under_site_rules(database):
    """The same box score must resolve DK and FD differently."""

    result = build_slate(database, _salary_csv(), "NBA", "DK", "2026-01-15")
    player = result.pool[0]["player_id"]
    line = {"pts": 30, "reb": 12, "ast": 11, "stl": 2, "blk": 1, "tov": 3, "fg3m": 4}

    resolve_slate(
        database, result.slate_id,
        [{"player_id": player, "positions": ["PG"], "stats": line, "opportunity": 36}],
        get_config("NBA", "DK"),
    )
    database.lock_slate(result.slate_id)

    resolved = database.resolved_projections()
    assert len(resolved) == 1
    # Triple-double: DraftKings pays a three-point bonus FanDuel does not.
    assert resolved[0]["actual_points"] > 70


def test_a_full_lock_and_resolve_cycle_produces_a_calibration_row(database):
    result = build_slate(database, _salary_csv(), "NBA", "DK", "2026-01-15")

    box = [
        {
            "player_id": player["player_id"],
            "positions": player["positions"],
            "stats": {"pts": 20, "reb": 5, "ast": 5, "stl": 1, "blk": 0, "tov": 2, "fg3m": 2},
            "opportunity": 30,
        }
        for player in result.pool[:40]
    ]

    assert resolve_slate(database, result.slate_id, box, result.config) == 40
    database.lock_slate(result.slate_id)

    scored = score_projections(database.resolved_projections())
    assert scored is not None
    assert scored.count == 40
    assert scored.skill is not None
