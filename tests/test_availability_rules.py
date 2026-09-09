"""Who is in today's game, and why the three rules are not symmetric.

Treating every gap as an absence would delete most of a slate for most
of the afternoon. Treating none of them as an absence rosters players
who are confirmed to be sitting. The distinctions here are where the
difference lives.
"""

from __future__ import annotations

from dfs.availability import (
    announced_starters, benched_hitters, is_starter_position, ruled_out,
    sidelined_pitchers, teams_with_posted_lineups,
)

PITCHERS = ("P", "SP", "RP")


def _player(player_id, team, position="OF", **extra):
    return {
        "player_id": player_id, "name": player_id, "team": team,
        "positions": [position], **extra,
    }


# ----------------------------------------------------------------------
# Ruled out
# ----------------------------------------------------------------------


def test_the_injured_list_is_an_absence():
    pool = [
        _player("a", "MIL", injury_status="IL"),
        _player("b", "MIL", injury_status="DTD"),
        _player("c", "MIL"),
    ]

    assert ruled_out(pool) == {"a"}


# ----------------------------------------------------------------------
# Hitters: only a posted lineup settles anything
# ----------------------------------------------------------------------


def test_a_hitter_left_out_of_a_posted_lineup_is_benched():
    """The strongest signal available: the lineup is published and he
    is not in it, so he is worth zero."""

    pool = [
        _player("starting", "MIL", batting_order=1),
        _player("also-starting", "MIL", batting_order=2),
        _player("benched", "MIL"),
    ]

    assert benched_hitters(pool, PITCHERS) == {"benched"}


def test_a_team_that_has_not_posted_keeps_every_hitter():
    """For a hitter, "not yet listed" usually means "will play".
    Excluding them would delete most of the slate all afternoon."""

    pool = [_player("a", "SEA"), _player("b", "SEA"), _player("c", "SEA")]

    assert benched_hitters(pool, PITCHERS) == set()


def test_one_team_posting_does_not_bench_another_team():
    pool = [
        _player("posted", "MIL", batting_order=3),
        _player("mil-bench", "MIL"),
        _player("unposted", "SEA"),
    ]

    assert benched_hitters(pool, PITCHERS) == {"mil-bench"}
    assert teams_with_posted_lineups(pool) == {"MIL"}


def test_a_pitcher_is_not_benched_by_a_posted_batting_order():
    """He was never going to be in it. Pitchers are settled separately."""

    pool = [
        _player("hitter", "MIL", batting_order=1),
        _player("pitcher", "MIL", "SP"),
    ]

    assert benched_hitters(pool, PITCHERS) == set()


# ----------------------------------------------------------------------
# Pitchers: an announcement settles the whole staff
# ----------------------------------------------------------------------


def test_naming_a_starter_sidelines_his_team_mates():
    pool = [
        _player("ace", "MIL", "SP", starting="SP"),
        _player("reliever", "MIL", "RP"),
        _player("other-reliever", "MIL", "RP"),
    ]

    assert sidelined_pitchers(pool, {"ace"}, PITCHERS) == {"reliever", "other-reliever"}


def test_an_unannounced_team_is_excluded_too_unless_asked_otherwise():
    """An earlier version kept them, on the reasoning that excluding a
    game's starter made him unrosterable. That was wrong: a pitcher
    nobody has named is not known to be the starter, so keeping him only
    exposes the pool to his bullpen."""

    pool = [
        _player("ace", "MIL", "SP", starting="SP"),
        _player("mil-reliever", "MIL", "RP"),
        _player("sea-starter", "SEA", "SP"),
        _player("sea-reliever", "SEA", "RP"),
    ]

    assert sidelined_pitchers(pool, {"ace"}, PITCHERS) == {
        "mil-reliever", "sea-starter", "sea-reliever",
    }
    assert sidelined_pitchers(pool, {"ace"}, PITCHERS, whole_slate=False) == {
        "mil-reliever",
    }


def test_hitters_are_never_sidelined_by_the_pitcher_rule():
    pool = [
        _player("ace", "MIL", "SP", starting="SP"),
        _player("hitter", "MIL"),
    ]

    assert sidelined_pitchers(pool, {"ace"}, PITCHERS) == set()


def test_announced_starters_are_read_from_the_pool():
    pool = [
        _player("ace", "MIL", "SP", starting="SP"),
        _player("unannounced", "SEA", "SP"),
        _player("hitter", "MIL", batting_order=1, starting="1"),
    ]

    assert announced_starters(pool, PITCHERS) == {"ace"}


def test_the_starter_positions_come_from_the_sport():
    """Pitchers in baseball, goalies in hockey -- the same shape."""

    assert is_starter_position({"positions": ["G"]}, ("G",))
    assert not is_starter_position({"positions": ["D"]}, ("G",))
    assert is_starter_position({"positions": ["SP"]}, PITCHERS)


def test_a_pool_with_nothing_announced_excludes_nobody():
    """Every morning, before any of this is known."""

    pool = [_player("a", "MIL"), _player("b", "SEA", "SP")]

    assert ruled_out(pool) == set()
    assert benched_hitters(pool, PITCHERS) == set()
    assert sidelined_pitchers(pool, set(), PITCHERS) == set()


def test_once_anyone_is_announced_unannounced_pitchers_go_too():
    """A pitcher nobody has named is not known to be starting, and one
    who is not starting throws an inning in relief at most -- on a rate
    per inning good enough to win the optimizer outright."""

    pool = [
        _player("ace", "MIL", "SP", starting="SP"),
        _player("mil-pen", "MIL", "RP"),
        _player("tor-sp", "TOR", "SP"),
        _player("tor-pen", "TOR", "RP"),
        _player("hitter", "TOR"),
    ]

    assert sidelined_pitchers(pool, {"ace"}, PITCHERS) == {
        "mil-pen", "tor-sp", "tor-pen",
    }


def test_nothing_is_excluded_before_anything_is_announced():
    """First thing in the morning, nobody knows anything yet."""

    pool = [_player("a", "MIL", "SP"), _player("b", "TOR", "RP")]

    assert sidelined_pitchers(pool, set(), PITCHERS) == set()


def test_the_narrow_rule_keeps_unannounced_teams_whole():
    """For when you know a starter the file does not."""

    pool = [
        _player("ace", "MIL", "SP", starting="SP"),
        _player("mil-pen", "MIL", "RP"),
        _player("tor-sp", "TOR", "SP"),
    ]

    assert sidelined_pitchers(pool, {"ace"}, PITCHERS, whole_slate=False) == {"mil-pen"}


def test_naming_a_starter_yourself_keeps_him_and_drops_his_team_mates():
    """The better way to handle a starter the file has not caught up to."""

    pool = [
        _player("ace", "MIL", "SP", starting="SP"),
        _player("tor-sp", "TOR", "SP"),
        _player("tor-pen", "TOR", "RP"),
    ]

    assert sidelined_pitchers(pool, {"ace", "tor-sp"}, PITCHERS) == {"tor-pen"}
