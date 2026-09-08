"""Reading confirmed lineups from MLB's own schedule.

Written without being able to reach statsapi.mlb.com from the machine
it was written on -- the same position the stat collector was written
in. So the offline tests pin the parsing against shapes the API is
documented to return, and the gated live test at the bottom is what
proves those shapes are real. Run it before trusting this:

    DFS_NETWORK_TESTS=1 pytest tests/test_lineups.py -k live
"""

from __future__ import annotations

import os

import pytest

from dfs.ingest.lineups import (
    Availability, LineupError, STARTING_PITCHER, fetch_mlb_lineups, parse_schedule,
)

needs_network = pytest.mark.skipif(
    not os.environ.get("DFS_NETWORK_TESTS"),
    reason="hits statsapi.mlb.com; set DFS_NETWORK_TESTS=1 to run",
)


def _game(away_pitcher=None, home_pitcher=None, home_lineup=(), away_lineup=()):
    return {
        "teams": {
            "away": {
                "team": {"abbreviation": "CHC"},
                **({"probablePitcher": {"fullName": away_pitcher}} if away_pitcher else {}),
            },
            "home": {
                "team": {"abbreviation": "MIL"},
                **({"probablePitcher": {"fullName": home_pitcher}} if home_pitcher else {}),
            },
        },
        "lineups": {
            "homePlayers": [{"fullName": name} for name in home_lineup],
            "awayPlayers": [{"fullName": name} for name in away_lineup],
        },
    }


def _payload(*games):
    return {"dates": [{"games": list(games)}]}


# ----------------------------------------------------------------------
# Announced pitchers
# ----------------------------------------------------------------------


def test_both_probable_pitchers_are_read():
    found = parse_schedule(_payload(_game("Justin Steele", "Jacob Misiorowski")))

    assert {record.name for record in found} == {"Justin Steele", "Jacob Misiorowski"}
    assert all(record.starting == STARTING_PITCHER for record in found)
    assert all(record.batting_order is None for record in found)


def test_a_pitcher_carries_the_id_the_pool_uses():
    """Names are the join, so the id has to be built the same way."""

    found = parse_schedule(_payload(_game("Jacob Misiorowski")))

    assert found[0].player_id == "mlb:jacob-misiorowski"


def test_a_game_with_no_probable_pitcher_yet_contributes_nothing():
    assert parse_schedule(_payload(_game())) == []


# ----------------------------------------------------------------------
# Posted batting orders
# ----------------------------------------------------------------------


def test_a_posted_lineup_becomes_a_batting_order():
    names = [f"Hitter {index}" for index in range(1, 10)]
    found = parse_schedule(_payload(_game(home_lineup=names)))

    orders = {record.name: record.batting_order for record in found}

    assert orders["Hitter 1"] == 1
    assert orders["Hitter 9"] == 9
    assert len(orders) == 9


def test_the_batting_order_matches_the_column_the_file_uses():
    """One vocabulary for both sources, or the app has to know which
    one it is looking at."""

    found = parse_schedule(_payload(_game(home_lineup=["Leads Off"])))

    assert found[0].starting == "1"
    assert found[0].batting_order == 1


def test_more_than_nine_names_is_a_shape_we_do_not_understand():
    """Better to drop the extras than to record a tenth batting slot."""

    names = [f"Hitter {index}" for index in range(1, 13)]
    found = parse_schedule(_payload(_game(home_lineup=names)))

    assert len(found) == 9
    assert max(record.batting_order for record in found) == 9


def test_a_game_posted_on_one_side_only_yields_that_side():
    """The ordinary afternoon state: one team has posted, one has not."""

    found = parse_schedule(_payload(_game(home_lineup=["Posted Hitter"])))

    assert [record.name for record in found] == ["Posted Hitter"]


# ----------------------------------------------------------------------
# Shapes that are not what we expect
# ----------------------------------------------------------------------


@pytest.mark.parametrize("payload", [
    {}, {"dates": []}, {"dates": [{}]}, {"dates": [{"games": []}]},
    {"dates": [{"games": [{}]}]},
    {"dates": [{"games": [{"teams": {}, "lineups": {}}]}]},
    {"dates": [{"games": [{"teams": {"home": {"probablePitcher": {}}}}]}]},
])
def test_an_unfamiliar_shape_yields_nothing_rather_than_something_wrong(payload):
    assert parse_schedule(payload) == []


def test_a_team_without_an_abbreviation_is_still_read():
    """The team is useful, not required: the join is on the player."""

    payload = {"dates": [{"games": [{
        "teams": {"home": {"team": {}, "probablePitcher": {"fullName": "Someone"}}},
        "lineups": {},
    }]}]}

    found = parse_schedule(payload)

    assert found[0].name == "Someone"
    assert found[0].team is None


# ----------------------------------------------------------------------
# Against the live API
# ----------------------------------------------------------------------


@needs_network
def test_live_the_schedule_still_has_the_shape_this_parses():
    """The test that actually proves this works.

    Pinned to a date in the middle of a completed season, so it is about
    the response's shape rather than about whether anyone has announced
    anything today.
    """

    found = fetch_mlb_lineups("2025-07-15")

    assert found, "the schedule returned no starters at all"

    pitchers = [record for record in found if record.batting_order is None]
    hitters = [record for record in found if record.batting_order]

    assert pitchers, "no probable pitchers were found"
    assert hitters, "no posted batting orders were found"
    assert all(1 <= record.batting_order <= 9 for record in hitters)
    assert all(record.player_id.startswith("mlb:") for record in found)


@needs_network
def test_live_a_date_with_no_games_is_not_an_error():
    """The All-Star break, and every February day."""

    assert fetch_mlb_lineups("2025-02-01") == []
