"""Reading a salary file's sport, site and date off the file itself.

Uploading several files and then telling the app what each one is, once
per file, is the step that stops a multi-sport tool being used. The
files already say.

The bias throughout is toward refusing. Guessing wrong is expensive: an
NBA export read as NFL parses perfectly, produces a pool eligible for
no roster slot, and yields an empty build that looks like a solver
problem rather than a mislabelled file.
"""

from __future__ import annotations

import pytest

from dfs.ingest.detect import detect, detect_date, detect_site, detect_sport, sport_coverage

DK_HEADER = (
    "Position,Name + ID,Name,ID,Roster Position,Salary,Game Info,"
    "TeamAbbrev,AvgPointsPerGame"
)
FD_HEADER = (
    "Id,Position,First Name,Last Name,Nickname,FPPG,Salary,Game,Team,"
    "Opponent,Injury Indicator,Roster Position"
)

POSITIONS = {
    "NFL": ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "DST"],
    "NBA": ["PG", "SG", "SF", "PF", "C", "PG/SG", "SF/PF"],
    "MLB": ["P", "P", "C", "1B", "2B", "3B", "SS", "OF", "OF", "OF"],
    "NHL": ["C", "C", "LW", "RW", "W", "D", "D", "G"],
    "EPL": ["GK", "DF", "DF", "MF", "MF", "FW", "ST"],
}


def make(site: str, positions, game: str | None = None) -> str:
    rows = [DK_HEADER if site == "DK" else FD_HEADER]
    for index, position in enumerate(positions):
        if site == "DK":
            info = game if game is not None else "AAA@BBB 09/14/2026 01:00PM ET"
            rows.append(
                f"{position},P{index} ({index}),P{index},{index},{position},"
                f"5000,{info},AAA,9.5"
            )
        else:
            rows.append(
                f"9-{index},{position},First,Last{index},P{index},9.5,5000,"
                f"AAA@BBB,AAA,BBB,,{position}"
            )
    return "\n".join(rows)


# ----------------------------------------------------------------------
# Site, from the header row
# ----------------------------------------------------------------------


def test_the_two_sites_are_told_apart_by_their_headers():
    assert detect_site(make("DK", ["QB"])) == "DK"
    assert detect_site(make("FD", ["QB"])) == "FD"


def test_a_header_from_neither_site_is_not_forced_into_one():
    assert detect_site("Player,Cost\nSomeone,100") is None


def test_an_empty_file_is_not_a_site():
    assert detect_site("") is None


# ----------------------------------------------------------------------
# Sport, from the positions inside
# ----------------------------------------------------------------------


@pytest.mark.parametrize("sport", sorted(POSITIONS))
@pytest.mark.parametrize("site", ["DK", "FD"])
def test_every_sport_and_site_pair_is_identified(sport, site):
    found = detect(make(site, POSITIONS[sport]))

    assert found.sport == sport
    assert found.site == site
    assert found.complete


def test_a_file_of_only_centres_is_ambiguous_rather_than_guessed():
    """C is a centre, a catcher and a center. Three sports claim it."""

    sport, _, reason = detect_sport(make("DK", ["C"] * 6))

    assert sport is None
    assert "Ambiguous" in reason


def test_defenders_and_goalkeepers_do_not_settle_hockey_against_soccer():
    sport, _, reason = detect_sport(make("DK", ["D", "G", "D", "G"]))

    assert sport is None
    assert "Ambiguous" in reason


def test_the_full_hockey_vocabulary_does_settle_it():
    """Wingers are the letters soccer does not have."""

    sport, _, _ = detect_sport(make("DK", ["C", "LW", "RW", "D", "D", "G"]))

    assert sport == "NHL"


def test_positions_no_sport_uses_are_reported_not_ranked():
    sport, _, reason = detect_sport(make("DK", ["ZZ", "YY", "XX"]))

    assert sport is None
    assert "No sport recognises" in reason


def test_a_file_with_no_rows_says_so():
    sport, _, reason = detect_sport(DK_HEADER)

    assert sport is None
    assert "no rows" in reason or "no Position" in reason


def test_coverage_is_the_share_of_players_a_vocabulary_recognises():
    scores = sport_coverage(make("DK", POSITIONS["NFL"]))

    assert scores["NFL"] == 1.0
    assert scores["NBA"] < 0.5


# ----------------------------------------------------------------------
# Date
# ----------------------------------------------------------------------


def test_draftkings_writes_the_date_into_every_row():
    assert detect_date(make("DK", ["QB"])) == "2026-09-14"


def test_the_earliest_date_names_a_slate_that_spans_days():
    """A Sunday slate with a Monday night game is a Sunday slate."""

    text = "\n".join([
        DK_HEADER,
        "QB,A (1),A,1,QB,5000,AAA@BBB 09/14/2026 01:00PM ET,AAA,9.5",
        "QB,B (2),B,2,QB,5000,CCC@DDD 09/15/2026 08:15PM ET,CCC,9.5",
    ])

    assert detect_date(text) == "2026-09-14"


def test_fanduel_carries_no_date_and_does_not_invent_one():
    """The caller substitutes the chosen date; guessing here would put a
    slate on the wrong day and quietly make it unresolvable."""

    found = detect(make("FD", POSITIONS["NFL"]))

    assert found.slate_date is None
    assert found.complete


def test_a_file_without_a_date_column_is_not_an_error():
    assert detect_date(make("DK", ["QB"], game="AAA@BBB")) is None


# ----------------------------------------------------------------------
# The whole answer
# ----------------------------------------------------------------------


def test_an_unidentifiable_file_carries_a_reason_a_person_can_act_on():
    found = detect("Player,Cost\nSomeone,100")

    assert not found.complete
    assert found.reason


def test_a_sport_without_a_site_is_incomplete():
    """Both halves are needed: the scoring table is keyed on the pair."""

    text = "Position\nQB\nRB\nWR\nTE"
    found = detect(text)

    assert found.sport == "NFL"
    assert found.site is None
    assert not found.complete
