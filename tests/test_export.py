"""Writing lineups back out in each site's bulk-upload format."""

from __future__ import annotations

import csv
import io

import pytest

from dfs.export import (
    ExportError,
    readable_filename,
    slot_columns,
    to_readable_csv,
    to_upload_csv,
    upload_filename,
)
from dfs.optimizer.lineup import Lineup, LineupPlayer
from dfs.sports import get_config

MLB = get_config("MLB", "DK")
NFL_FD = get_config("NFL", "FD")


def _player(slot, name, dk_id="1", fd_id="2", positions=("OF",)):
    return LineupPlayer(
        player_id=f"mlb:{name.lower()}", name=name, slot=slot,
        positions=tuple(positions), team="AAA", opponent="BBB",
        salary=5_000, projection=10.0, ceiling=15.0, ownership=8.0,
        dk_id=dk_id, fd_id=fd_id,
    )


def _mlb_lineup(**overrides):
    slots = ["P", "P", "C", "1B", "2B", "3B", "SS", "OF", "OF", "OF"]
    players = [
        _player(slot, f"Player{index}", dk_id=str(100 + index))
        for index, slot in enumerate(slots)
    ]
    for key, value in overrides.items():
        setattr(players[0], key, value)
    return Lineup(
        players=players, total_salary=49_000, total_projection=120.0,
        total_ceiling=180.0, total_ownership=90.0, mode="gpp",
    )


# --- columns ----------------------------------------------------------

def test_columns_repeat_for_multi_slot_positions():
    """A DraftKings MLB file really does have two P and three OF columns."""

    assert slot_columns(MLB) == ["P", "P", "C", "1B", "2B", "3B", "SS", "OF", "OF", "OF"]


def test_columns_match_the_roster_size():
    for config in (MLB, NFL_FD):
        assert len(slot_columns(config)) == config.roster_size


def test_columns_follow_the_site_s_roster_order():
    assert slot_columns(NFL_FD) == ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DEF"]


# --- the upload file --------------------------------------------------

def test_the_upload_file_is_a_header_and_one_row_per_lineup():
    rows = list(csv.reader(io.StringIO(to_upload_csv([_mlb_lineup(), _mlb_lineup()], MLB))))
    assert rows[0] == slot_columns(MLB)
    assert len(rows) == 3


def test_cells_are_site_ids_not_names():
    """Two players share a name often enough that names cannot be trusted."""

    rows = list(csv.reader(io.StringIO(to_upload_csv([_mlb_lineup()], MLB))))
    assert rows[1] == [str(100 + index) for index in range(10)]


def test_every_column_gets_a_player_for_that_slot():
    """Whatever order the lineup arrives in, each cell fills its column.

    Not an exact id sequence: the two P columns are interchangeable, as
    are the three OF, so which of the pair lands first is arbitrary and
    asserting it would test the sort rather than the mapping.
    """

    lineup = _mlb_lineup()
    lineup.players.reverse()

    rows = list(csv.reader(io.StringIO(to_upload_csv([lineup], MLB))))
    slot_of = {player.dk_id: player.slot for player in lineup.players}

    assert [slot_of[cell] for cell in rows[1]] == slot_columns(MLB)
    assert sorted(rows[1]) == sorted(str(100 + index) for index in range(10))


def test_fanduel_uses_its_own_ids():
    lineup = Lineup(
        players=[
            _player(slot, f"P{index}", dk_id="dk", fd_id=f"fd{index}")
            for index, slot in enumerate(slot_columns(NFL_FD))
        ],
        total_salary=59_000, total_projection=1.0, total_ceiling=1.0,
        total_ownership=1.0, mode="cash",
    )
    rows = list(csv.reader(io.StringIO(to_upload_csv([lineup], NFL_FD))))
    assert rows[1] == [f"fd{index}" for index in range(NFL_FD.roster_size)]


def test_a_missing_site_id_fails_loudly():
    """Writing a name and letting the site guess could enter the wrong player."""

    with pytest.raises(ExportError, match="no DK player id"):
        to_upload_csv([_mlb_lineup(dk_id=None)], MLB)


def test_a_lineup_for_another_sport_is_refused():
    with pytest.raises(ExportError, match="does not match this sport"):
        to_upload_csv([_mlb_lineup()], NFL_FD)


def test_exporting_nothing_says_so():
    with pytest.raises(ExportError, match="no lineups"):
        to_upload_csv([], MLB)


# --- the readable file ------------------------------------------------

def test_the_readable_file_names_players_and_totals():
    rows = list(csv.reader(io.StringIO(to_readable_csv([_mlb_lineup()], MLB))))
    assert rows[0][:3] == ["Lineup", "Slot", "Player"]
    assert rows[1][2] == "Player0"
    assert rows[-1][1] == "TOTAL"


def test_the_readable_file_separates_each_lineup():
    rows = list(csv.reader(io.StringIO(to_readable_csv([_mlb_lineup(), _mlb_lineup()], MLB))))
    # Ten players plus a total, twice, plus the header.
    assert len(rows) == 1 + 2 * 11
    assert {row[0] for row in rows[1:]} == {"1", "2"}


# --- filenames --------------------------------------------------------

def test_filenames_name_the_site_sport_and_date():
    assert upload_filename(MLB, "2026-09-02") == "draftkings-mlb-2026-09-02-upload.csv"
    assert upload_filename(NFL_FD, "2026-09-07") == "fanduel-nfl-2026-09-07-upload.csv"
    assert readable_filename(MLB, "2026-09-02") == "mlb-2026-09-02-lineups.csv"


# --- end to end -------------------------------------------------------

def test_a_built_lineup_exports_with_ids_from_the_salary_file(tmp_path):
    """The ids must survive parsing, storage, and lineup construction."""

    import random

    from dfs.db.database import Database
    from dfs.pipeline import build_slate

    generator = random.Random(1)
    rows = []
    count = 0
    for away, home in (("AAA", "BBB"), ("CCC", "DDD")):
        for team in (away, home):
            for position, many in [("SP", 2), ("C", 2), ("1B", 2), ("2B", 2),
                                   ("3B", 2), ("SS", 2), ("OF", 5)]:
                for index in range(many):
                    count += 1
                    rows.append({
                        "Position": position, "Name": f"{team} {position}{index}",
                        "ID": f"9{count:04d}", "Roster Position": position,
                        "Salary": generator.randrange(2_500, 9_000, 100),
                        "Game Info": f"{away}@{home} 09/02/2026 07:10PM ET",
                        "TeamAbbrev": team,
                        "AvgPointsPerGame": round(generator.uniform(5, 15), 2),
                    })

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)

    result = build_slate(
        Database(tmp_path / "e.db"), buffer.getvalue(), "MLB", "DK", "2026-09-02"
    )

    exported = list(csv.reader(io.StringIO(to_upload_csv(result.lineups, result.config))))
    assert exported[0] == slot_columns(MLB)
    assert all(cell.startswith("9") for cell in exported[1])
