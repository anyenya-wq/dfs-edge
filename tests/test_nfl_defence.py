"""Team defences: identity, aggregation, and scoring.

A defence is the one roster slot with no player behind it. It has no
name the two sites agree on and no row in the stats feed, so both its
identity and its stat line have to be constructed -- and until they
were, every defence fell back to the site's own average projection,
which is the number the entire field is already looking at.
"""

from __future__ import annotations

import math

import pytest

from dfs.ingest.salaries import parse_draftkings, parse_fanduel
from dfs.ingest.stats.nfl_defence import (
    defence_records, parse_scoreboard,
)
from dfs.ingest.teams import defence_id, is_defence, normalise_team
from dfs.sports import get_config


def _week_date(season, week):
    return f"{season}-09-{week:02d}"


SCOREBOARD = """game_id,season,week,away_team,away_score,home_team,home_score
2024_01_BAL_KC,2024,1,BAL,20,KC,27
2024_02_LA_ARI,2024,2,LAR,41,ARI,10
2024_03_NYJ_NE,2024,3,NYJ,,NE,
"""


def _row(**kwargs):
    row = {
        "team": "BAL", "opponent_team": "KC", "week": "1",
        "game_id": "2024_01_BAL_KC", "position": "LB",
        "def_interceptions": "0", "def_sacks": "0", "fumble_recovery_opp": "0",
        "def_safeties": "0", "def_tds": "0", "special_teams_tds": "0",
        "def_punt_blocks": "0", "def_fg_blocks": "0", "def_pat_blocks": "0",
        "sacks_suffered": "0",
    }
    row.update({key: str(value) for key, value in kwargs.items()})
    return row


# ----------------------------------------------------------------------
# Identity
# ----------------------------------------------------------------------


def test_the_two_sites_name_a_defence_differently_and_still_agree():
    """The whole reason a defence is keyed by team rather than by name."""

    dk = parse_draftkings(
        "Position,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,AvgPointsPerGame\n"
        "DST,Ravens,1,DST,3400,BAL@KC 09/05/2026 08:20PM ET,BAL,8.1\n",
        "NFL",
    )
    fd = parse_fanduel(
        "Id,Position,First Name,Last Name,Nickname,FPPG,Salary,Game,Team,Opponent,"
        "Injury Indicator,Roster Position\n"
        "9-1,D,Baltimore,Ravens,Baltimore Ravens,8.4,4200,BAL@KC,BAL,KC,,DEF\n",
        "NFL",
    )

    assert dk[0]["name"] != fd[0]["name"]
    assert dk[0]["player_id"] == fd[0]["player_id"] == "nfl:dst:bal"


def test_a_relocated_or_respelled_team_resolves_to_one_franchise():
    """nflverse writes the Rams LA, DraftKings writes them LAR."""

    assert normalise_team("LAR") == normalise_team("LA") == "LA"
    assert normalise_team("JAC") == normalise_team("JAX") == "JAX"
    assert normalise_team("OAK") == "LV"
    assert normalise_team("SD") == "LAC"


def test_an_unknown_team_code_produces_no_id_rather_than_a_wrong_one():
    """A guess here joins a defence to another team's history."""

    assert normalise_team("ZZZ") is None
    assert defence_id("ZZZ") is None


def test_a_skill_player_is_still_keyed_by_name():
    pool = parse_draftkings(
        "Position,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,AvgPointsPerGame\n"
        "WR,Zay Flowers,2,WR/FLEX,6200,BAL@KC 09/05/2026 08:20PM ET,BAL,14.2\n",
        "NFL",
    )

    assert pool[0]["player_id"] == "nfl:zay-flowers"


def test_defence_positions_cover_both_sites_spellings():
    assert is_defence(["DST"]) and is_defence(["DEF"]) and is_defence(["D"])
    assert not is_defence(["WR"])


# ----------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------


@pytest.mark.parametrize("site", ["DK", "FD"])
@pytest.mark.parametrize(
    "allowed,points",
    [(0, 10.0), (6, 7.0), (7, 4.0), (13, 4.0), (14, 1.0), (20, 1.0),
     (21, 0.0), (27, 0.0), (28, -1.0), (34, -1.0), (35, -4.0), (52, -4.0)],
)
def test_points_allowed_bands(site, allowed, points):
    config = get_config("NFL", site)
    assert config.score_stat_line({"points_allowed": allowed}, ["DST"]) == points


def test_a_defence_can_score_below_zero():
    """No skill player can, which changes how a defence is optimised."""

    config = get_config("NFL", "DK")
    assert config.score_stat_line({"points_allowed": 45, "sack": 1}, ["DST"]) == -3.0


def test_a_skill_player_never_collects_the_shutout_bonus():
    """The failure a value-based tier would have caused.

    A receiver's line has no `points_allowed` key at all. Reading that
    absence as zero would pay every one of them ten points.
    """

    config = get_config("NFL", "DK")
    assert config.score_stat_line({"rec": 5, "rec_yd": 60}, ["WR"]) == 11.0


def test_a_defence_is_scored_on_its_own_table_not_the_skill_one():
    config = get_config("NFL", "DK")
    line = {"sack": 4, "def_int": 2, "fumble_recovery": 1, "def_td": 1,
            "blocked_kick": 1, "safety": 1, "points_allowed": 3}

    # 4 sacks + 2x2 int + 2 fumble + 6 TD + 2 block + 2 safety + 7 band
    assert config.score_stat_line(line, ["DST"]) == 27.0


# ----------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------


def test_a_defence_is_the_sum_of_its_players():
    rows = [
        _row(position="LB", def_interceptions=1, def_tds=1),
        _row(position="CB", def_interceptions=1),
        _row(position="DE", fumble_recovery_opp=1, def_fg_blocks=1),
    ]

    records = defence_records(rows, 2024, parse_scoreboard(SCOREBOARD), _week_date)

    assert len(records) == 1
    stats = records[0]["stats"]
    assert stats["def_int"] == 2
    assert stats["def_td"] == 1
    assert stats["fumble_recovery"] == 1
    assert stats["blocked_kick"] == 1


def test_sacks_come_from_the_offence_that_suffered_them():
    """Ten team-games in 2024 had a sack no defender was credited with."""

    rows = [
        _row(team="BAL", opponent_team="KC", def_sacks=2),
        _row(team="KC", opponent_team="BAL", position="QB", sacks_suffered=3),
    ]

    records = {r["team"]: r for r in
               defence_records(rows, 2024, parse_scoreboard(SCOREBOARD), _week_date)}

    assert records["BAL"]["stats"]["sack"] == 3


def test_sacks_fall_back_to_credited_when_the_opponent_has_no_rows():
    rows = [_row(team="BAL", opponent_team="KC", def_sacks=2)]

    records = defence_records(rows, 2024, parse_scoreboard(SCOREBOARD), _week_date)

    assert records[0]["stats"]["sack"] == 2


def test_points_allowed_is_what_the_other_side_scored():
    rows = [
        _row(team="BAL", opponent_team="KC"),
        _row(team="KC", opponent_team="BAL"),
    ]

    records = {r["team"]: r for r in
               defence_records(rows, 2024, parse_scoreboard(SCOREBOARD), _week_date)}

    assert records["BAL"]["stats"]["points_allowed"] == 27
    assert records["KC"]["stats"]["points_allowed"] == 20


def test_a_game_with_no_score_yet_is_not_a_double_shutout():
    """The failure worth guarding: a fixture read as 0-0 pays both
    defences ten points for a game nobody has played."""

    rows = [
        _row(team="NYJ", opponent_team="NE", week=3, game_id="2024_03_NYJ_NE"),
        _row(team="NE", opponent_team="NYJ", week=3, game_id="2024_03_NYJ_NE"),
    ]

    assert defence_records(rows, 2024, parse_scoreboard(SCOREBOARD), _week_date) == []


def test_the_scoreboard_normalises_its_own_team_codes():
    """games.csv writes the Rams LAR where the stats file writes LA."""

    scores = parse_scoreboard(SCOREBOARD)

    assert scores["2024_02_LA_ARI"] == {"LA": 10.0, "ARI": 41.0}


def test_a_defence_carries_the_same_date_as_its_players():
    """A defence dated apart from its offence is invisible to every join."""

    rows = [_row(week=5)]
    records = defence_records(rows, 2024, parse_scoreboard(SCOREBOARD), _week_date)

    assert records == [] or records[0]["game_date"] == _week_date(2024, 5)


def test_every_defence_game_is_one_opportunity():
    """A defence plays its whole game, so there is no playing time to
    model and the rate is simply points per game."""

    rows = [_row()]
    records = defence_records(rows, 2024, parse_scoreboard(SCOREBOARD), _week_date)

    assert records[0]["opportunity"] == 1.0


def test_an_unrecognised_team_is_dropped_rather_than_invented():
    rows = [_row(team="ZZZ")]

    assert defence_records(rows, 2024, parse_scoreboard(SCOREBOARD), _week_date) == []


def test_the_bands_partition_every_possible_score():
    """No gap and no overlap, including at each boundary."""

    config = get_config("NFL", "DK")
    tier = config.tiers[0]

    assert tier.bands[-1][0] == math.inf
    scores = [tier.points_for(value) for value in range(0, 80)]
    assert all(score is not None for score in scores)
    assert scores[0] == 10.0 and scores[-1] == -4.0


# ----------------------------------------------------------------------
# Projection: the matchup, which is most of the signal
# ----------------------------------------------------------------------


from dfs.projections.defence import (
    MIN_OPPONENT_GAMES, OWN_FORM_WEIGHT, blend_with_matchup, concession_levels,
)


def _conceded(opponent, points_allowed, count):
    return [
        {"opponent": opponent, "stats": {"points_allowed": points_allowed}}
        for _ in range(count)
    ]


def test_an_offence_that_concedes_more_reads_as_a_softer_draw():
    config = get_config("NFL", "DK")
    logs = _conceded("NYG", 3, 5) + _conceded("BUF", 38, 5)

    levels, league = concession_levels(logs, config)

    assert levels["NYG"] > league > levels["BUF"]


def test_an_opponent_with_too_little_history_gets_no_level():
    """Two soft games would otherwise set an offence's price for a season."""

    config = get_config("NFL", "DK")
    logs = _conceded("NYG", 3, MIN_OPPONENT_GAMES - 1)

    levels, _ = concession_levels(logs, config)

    assert "NYG" not in levels


def test_a_defence_without_a_matchup_read_says_so():
    """Silence here would imply an adjustment that never happened."""

    blended, note = blend_with_matchup(9.0, "ZZZ", {"BUF": 2.0}, 6.0)

    assert blended == 9.0
    assert "own form only" in note


def test_the_blend_moves_a_projection_toward_its_matchup():
    soft, _ = blend_with_matchup(6.0, "NYG", {"NYG": 12.0}, 6.0)
    tough, _ = blend_with_matchup(6.0, "BUF", {"BUF": 2.0}, 6.0)

    assert soft > 6.0 > tough
    assert soft == OWN_FORM_WEIGHT * 6.0 + (1 - OWN_FORM_WEIGHT) * 12.0


def test_the_note_names_the_direction_of_the_draw():
    _, soft = blend_with_matchup(6.0, "NYG", {"NYG": 12.0}, 6.0)
    _, tough = blend_with_matchup(6.0, "BUF", {"BUF": 2.0}, 6.0)

    assert "soft draw" in soft and "tough draw" in tough


def test_a_skill_player_is_never_blended(database):
    """The blend applies to defences and nothing else."""

    from dfs.pipeline import project_slate

    slate = database.upsert_slate("NFL", "DK", "2026-09-20")
    database.save_salaries(slate, [{
        "player_id": "nfl:zay-flowers", "name": "Zay Flowers", "sport": "NFL",
        "team": "BAL", "opponent": "KC", "positions": ["WR"],
        "roster_positions": ["WR"], "salary": 6200,
    }])
    for day in range(10, 16):
        database.save_game_log({
            "player_id": "nfl:zay-flowers", "sport": "NFL",
            "game_date": f"2026-09-{day:02d}", "opportunity": 8,
            "stats": {"rec": 5, "rec_yd": 60},
        })

    projected, _ = project_slate(
        database, slate, get_config("NFL", "DK"),
        database.player_pool(slate), "2026-09-20", store=False,
    )

    assert projected[0]["projection_source"] == "model"


def test_a_defence_in_a_pool_is_projected_from_its_matchup(database):
    """The whole point, end to end.

    Before this existed a defence had no game logs at all and fell back
    to the site's own season average -- the number every entrant in the
    contest is already looking at.
    """

    from dfs.pipeline import project_slate

    # Two defences with identical form, facing offences that have
    # conceded very differently.
    for team, opponent in (("BAL", "NYG"), ("KC", "BUF")):
        database.upsert_player({
            "player_id": f"nfl:dst:{team.lower()}", "name": f"{team} DST",
            "sport": "NFL", "positions": ["DST"],
        })
        for day in range(10, 16):
            database.save_game_log({
                "player_id": f"nfl:dst:{team.lower()}", "sport": "NFL",
                "game_date": f"2026-09-{day:02d}", "opportunity": 1.0,
                "opponent": "CHI",
                "stats": {"points_allowed": 20, "sack": 3},
            })

    # The history that prices each offence.
    for opponent, allowed in (("NYG", 0), ("BUF", 45)):
        for index, day in enumerate(range(10, 16)):
            database.upsert_player({
                "player_id": f"nfl:dst:x{opponent}{index}", "name": f"{opponent}{index}",
                "sport": "NFL", "positions": ["DST"],
            })
            database.save_game_log({
                "player_id": f"nfl:dst:x{opponent}{index}", "sport": "NFL",
                "game_date": f"2026-09-{day:02d}", "opportunity": 1.0,
                "opponent": opponent,
                "stats": {"points_allowed": allowed},
            })

    slate = database.upsert_slate("NFL", "DK", "2026-09-20")
    database.save_salaries(slate, [
        {
            "player_id": f"nfl:dst:{team.lower()}", "name": f"{team} DST",
            "sport": "NFL", "team": team, "opponent": opponent,
            "positions": ["DST"], "roster_positions": ["DST"],
            "salary": 3000, "site_avg_points": 7.0,
        }
        for team, opponent in (("BAL", "NYG"), ("KC", "BUF"))
    ])

    projected, _ = project_slate(
        database, slate, get_config("NFL", "DK"),
        database.player_pool(slate), "2026-09-20", store=False,
    )
    by_team = {row["team"]: row for row in projected}

    # Neither fell back to the site average.
    assert all(row["projection_source"] == "model+matchup" for row in projected)

    # Both defences have the same form: 20 points allowed is the +1 band,
    # three sacks is +3, so each projects 4.0 on its own history. The
    # matchup is the whole difference between them -- NYG has been shut
    # out (a defence facing them has scored 10), BUF has hung 45 (-4).
    assert by_team["BAL"]["projected_points"] == 0.5 * 4.0 + 0.5 * 10.0
    assert by_team["KC"]["projected_points"] == 0.5 * 4.0 + 0.5 * -4.0
