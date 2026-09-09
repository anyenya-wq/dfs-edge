"""Salary export parsing and roster eligibility."""

from __future__ import annotations

import pytest

from dfs.ingest.salaries import (
    expand_roster_eligibility,
    parse_salaries,
    player_id,
    slugify_name,
)
from dfs.sports import get_config

DK_CSV = """Position,Name + ID,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,AvgPointsPerGame
PG,Luka Doncic (123),Luka Dončić,123,PG/SG/G/UTIL,11200,DAL@PHX 01/15/2026 08:00PM ET,DAL,52.3
C,Nikola Jokic (456),Nikola Jokic,456,C/UTIL,11800,DEN@LAL 01/15/2026 10:00PM ET,LAL,58.1
"""

FD_CSV = """Id,Position,First Name,Nickname,Last Name,FPPG,Played,Salary,Game,Team,Opponent,Injury Indicator,Injury Details,Tier,Roster Position
1-2,PG,Luka,Luka Doncic,Doncic,52.3,40,11200,DAL@PHX,DAL,PHX,,,,PG
3-4,C,Nikola,Nikola Jokic,Jokic,58.1,41,11800,DEN@LAL,LAL,DEN,Q,knee,,C
"""


def test_slug_normalises_accents_and_suffixes():
    assert slugify_name("Luka Dončić") == "luka-doncic"
    assert slugify_name("Michael Pittman Jr.") == slugify_name("Michael Pittman")
    assert slugify_name("D'Angelo Russell") == "d-angelo-russell"


def test_the_same_player_joins_across_sites():
    """The whole point of the slug: one id from two different feeds."""

    dk = parse_salaries(DK_CSV, "NBA", "DK")
    fd = parse_salaries(FD_CSV, "NBA", "FD")
    assert dk[0]["player_id"] == fd[0]["player_id"]


def test_draftkings_columns_are_read():
    pool = parse_salaries(DK_CSV, "NBA", "DK")
    luka = pool[0]

    assert luka["name"] == "Luka Dončić"
    assert luka["salary"] == 11200
    assert luka["team"] == "DAL"
    assert luka["roster_positions"] == ["PG", "SG", "G", "UTIL"]
    assert luka["site_avg_points"] == 52.3


def test_home_and_away_come_from_the_matchup_string():
    """The away team is written first, which is the only home/away signal."""

    pool = parse_salaries(DK_CSV, "NBA", "DK")
    away, home = pool[0], pool[1]

    assert away["team"] == "DAL" and away["home"] is False and away["opponent"] == "PHX"
    assert home["team"] == "LAL" and home["home"] is True and home["opponent"] == "DEN"


def test_fanduel_injury_indicator_is_captured():
    pool = parse_salaries(FD_CSV, "NBA", "FD")
    assert pool[1]["injury_status"] == "Q"
    assert pool[0]["injury_status"] is None


def test_flex_eligibility_is_expanded():
    """A player listed only as PG must still reach the G and UTIL slots."""

    pool = [{"name": "Bare", "positions": ["PG"], "roster_positions": []}]
    expand_roster_eligibility(pool, get_config("NBA", "DK"))
    assert set(pool[0]["roster_positions"]) == {"PG", "G", "UTIL"}


def test_expansion_respects_a_site_without_flex_slots():
    pool = [{"name": "Bare", "positions": ["PG"], "roster_positions": []}]
    expand_roster_eligibility(pool, get_config("NBA", "FD"))
    assert pool[0]["roster_positions"] == ["PG"]


def test_rows_without_a_salary_are_skipped():
    csv = DK_CSV + "SG,Broken (9),Broken Row,9,SG,,DAL@PHX 01/15/2026,DAL,0\n"
    assert len(parse_salaries(csv, "NBA", "DK")) == 2


def test_unknown_site_is_rejected():
    with pytest.raises(ValueError, match="Unknown site"):
        parse_salaries(DK_CSV, "NBA", "YAHOO")


def test_player_id_is_namespaced_by_sport():
    assert player_id("Josh Allen", "NFL") != player_id("Josh Allen", "NBA")


# ----------------------------------------------------------------------
# Eligibility belongs to the slate, not to the player
# ----------------------------------------------------------------------


def test_published_eligibility_is_not_widened_by_a_players_other_positions():
    """The bug DraftKings rejected nine lineups over.

    Positions are stored once per player and overwritten by whichever
    file was uploaded last, so a FanDuel MLB file rewrote what
    DraftKings had said about the same man. Expansion then read those
    positions and added slots this slate never offered — 116 of 942
    players in one real pool — and the upload came back with "not in a
    valid roster position".

    Here DraftKings lists him at second base only, while the player row
    remembers an outfield eligibility from somewhere else. Second base
    is the answer.
    """

    pool = [{
        "player_id": "mlb:oneil-cruz",
        "name": "Oneil Cruz",
        "roster_positions": ["2B"],
        "positions": ["2B", "OF", "SS"],
    }]

    expand_roster_eligibility(pool, get_config("MLB", "DK"))

    assert pool[0]["roster_positions"] == ["2B"]


def test_eligibility_is_still_expanded_when_the_site_publishes_none():
    """The reason expansion exists, which must survive the fix.

    A file that lists a position and no roster eligibility still needs
    the combined slots filling in, or the optimizer never considers
    them.
    """

    pool = [{
        "player_id": "nba:guard",
        "name": "A Guard",
        "roster_positions": [],
        "positions": ["PG"],
    }]

    expand_roster_eligibility(pool, get_config("NBA", "DK"))

    slots = set(pool[0]["roster_positions"])
    assert "PG" in slots
    assert {"G", "UTIL"} & slots, "combined slots should still be inferred"


def test_a_multi_position_player_keeps_every_slot_the_site_published():
    pool = [{
        "player_id": "mlb:abrams",
        "name": "CJ Abrams",
        "roster_positions": ["2B", "SS"],
        "positions": ["2B", "SS"],
    }]

    expand_roster_eligibility(pool, get_config("MLB", "DK"))

    assert pool[0]["roster_positions"] == ["2B", "SS"]
