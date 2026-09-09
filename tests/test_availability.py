"""What the site says about who is playing, read from the salary file.

The export carries more than salaries. DraftKings publishes a batting
order once a lineup is posted, a code on a pitcher once he is
announced, and an injury status -- in the same file already uploaded.
That is the difference between a pool of everyone on the roster and a
pool of everyone who will take the field, and in baseball it is the
whole game: forty pitchers are listed and about a dozen start.
"""

from __future__ import annotations

import pytest

from dfs.availability import announced_starters, benched_hitters
from dfs.ingest.salaries import (
    BENCHED, is_ruled_out, parse_draftkings, parse_fanduel, parse_starting,
)

HEADER = (
    "Position,Name + ID,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,"
    "AvgPointsPerGame,Status,Starting"
)


def _file(rows) -> str:
    lines = [HEADER]
    for index, (position, name, status, starting) in enumerate(rows):
        lines.append(
            f"{position},{name} ({index}),{name},{index},{position},5000,"
            f"AAA@BBB 09/08/2026 07:40PM ET,AAA,9.5,{status},{starting}"
        )
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Reading the column
# ----------------------------------------------------------------------


def test_a_batting_order_is_read_as_a_number():
    assert parse_starting("3") == ("3", 3)
    assert parse_starting("9") == ("9", 9)


def test_a_pitcher_code_is_kept_but_is_not_a_batting_order():
    """Both facts share one column because they are the same fact."""

    assert parse_starting("SP") == ("SP", None)
    assert parse_starting("P") == ("P", None)


def test_blank_means_not_yet_announced_rather_than_out():
    assert parse_starting("") == (None, None)
    assert parse_starting(None) == (None, None)


def test_a_number_outside_a_batting_order_is_not_one():
    assert parse_starting("12") == ("12", None)
    assert parse_starting("0") == ("0", None)


# ----------------------------------------------------------------------
# Ruled out, versus merely doubtful
# ----------------------------------------------------------------------


@pytest.mark.parametrize("status", ["IL", "OUT", "O", "il", " OUT "])
def test_these_statuses_mean_the_player_will_not_appear(status):
    assert is_ruled_out(status)


@pytest.mark.parametrize("status", ["DTD", "Q", "GTD", "", None, "probable"])
def test_a_doubt_is_not_an_absence(status):
    """Removing a probable starter can leave no legal lineup at all,
    which is a worse error than carrying a small risk."""

    assert not is_ruled_out(status)


# ----------------------------------------------------------------------
# Through the parser
# ----------------------------------------------------------------------


def test_the_pool_carries_what_the_file_said():
    pool = parse_draftkings(_file([
        ("SP", "Announced Starter", "", "SP"),
        ("SP", "Not Announced", "", ""),
        ("OF", "Leads Off", "", "1"),
        ("OF", "On The Injured List", "IL", ""),
        ("C", "Day To Day", "DTD", "4"),
    ]), "MLB")

    by_name = {player["name"]: player for player in pool}

    assert by_name["Announced Starter"]["starting"] == "SP"
    assert by_name["Announced Starter"]["batting_order"] is None
    assert by_name["Not Announced"]["starting"] is None
    assert by_name["Leads Off"]["batting_order"] == 1
    assert by_name["On The Injured List"]["injury_status"] == "IL"
    assert by_name["Day To Day"]["batting_order"] == 4


def test_a_file_without_the_columns_still_parses():
    """Older exports, and every FanDuel export, have neither column."""

    older = (
        "Position,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,AvgPointsPerGame\n"
        "SP,Someone,1,P,9000,AAA@BBB 09/08/2026 07:40PM ET,AAA,20.1\n"
    )

    pool = parse_draftkings(older, "MLB")

    assert pool[0]["starting"] is None
    assert pool[0]["batting_order"] is None
    assert pool[0]["injury_status"] is None


# ----------------------------------------------------------------------
# FanDuel, which says outright what DraftKings leaves to be inferred
# ----------------------------------------------------------------------


FD_HEADER = (
    "Id,Position,First Name,Nickname,Last Name,FPPG,Played,Salary,Game,Team,"
    "Opponent,Injury Indicator,Injury Details,Tier,Probable Pitcher,"
    "Batting Order,Roster Position"
)


def _fd_file(rows) -> str:
    lines = [FD_HEADER]
    for index, (position, name, injury, probable, order) in enumerate(rows):
        lines.append(
            f"9-{index},{position},First,{name},Last,9.5,20,5000,CHC@MIL,MIL,"
            f"CHC,{injury},,,{probable},{order},{position}"
        )
    return "\n".join(lines)


def test_fanduel_names_its_probable_pitchers_in_a_column_of_their_own():
    pool = parse_fanduel(_fd_file([
        ("P", "Probable", "", "Yes", "0"),
        ("P", "Not Probable", "", "", "0"),
    ]), "MLB")

    by_name = {player["name"]: player for player in pool}

    assert by_name["Probable"]["starting"] == "SP"
    assert by_name["Not Probable"]["starting"] is None


def test_a_pitchers_batting_order_of_zero_is_not_a_bench_marking():
    """He was never going to bat. His availability is the other column."""

    pool = parse_fanduel(_fd_file([("P", "Someone", "", "Yes", "0")]), "MLB")

    assert pool[0]["starting"] == "SP"
    assert pool[0]["batting_order"] is None


def test_zero_means_the_card_is_out_and_he_is_not_on_it():
    """The distinction DraftKings cannot express, and FanDuel can."""

    pool = parse_fanduel(_fd_file([
        ("OF", "Batting Third", "", "", "3"),
        ("OF", "Benched", "", "", "0"),
        ("OF", "Card Not Out", "", "", ""),
    ]), "MLB")

    by_name = {player["name"]: player for player in pool}

    assert by_name["Batting Third"]["batting_order"] == 3
    assert by_name["Benched"]["starting"] == BENCHED
    assert by_name["Benched"]["batting_order"] is None
    assert by_name["Card Not Out"]["starting"] is None


def test_an_explicitly_benched_hitter_is_benched_whatever_his_team_did():
    """No inference needed, so none is used."""

    pool = parse_fanduel(_fd_file([("OF", "Benched", "", "", "0")]), "MLB")

    assert benched_hitters(pool, ("P", "SP", "RP")) == {pool[0]["player_id"]}


def test_a_blank_order_on_a_team_that_has_not_posted_is_not_benched():
    pool = parse_fanduel(_fd_file([
        ("OF", "Unknown", "", "", ""),
        ("OF", "Also Unknown", "", "", ""),
    ]), "MLB")

    assert benched_hitters(pool, ("P", "SP", "RP")) == set()


def test_a_benched_hitter_is_never_counted_as_an_announced_starter():
    """`starting` carries both facts, so the marker has to be excluded."""

    pool = parse_fanduel(_fd_file([("OF", "Benched", "", "", "0")]), "MLB")

    assert announced_starters(pool, ("P", "SP", "RP")) == set()


def test_fanduels_injury_column_is_read():
    pool = parse_fanduel(_fd_file([
        ("OF", "On IL", "IL", "", "0"),
        ("OF", "Day To Day", "DTD", "", "3"),
    ]), "MLB")

    by_name = {player["name"]: player for player in pool}

    assert is_ruled_out(by_name["On IL"]["injury_status"])
    assert not is_ruled_out(by_name["Day To Day"]["injury_status"])
