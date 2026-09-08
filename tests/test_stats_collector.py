"""The nflverse collector: column mapping, opportunity, and date derivation.

Parsing is tested against synthetic rows rather than the live feed, so
the suite stays fast and does not fail when GitHub is unreachable. One
test does hit the network and is skipped unless DFS_NETWORK_TESTS is
set.
"""

from __future__ import annotations

import os

import pytest

from dfs.db.database import Database
from dfs.ingest.stats import CollectorError, PLANNED, has_collector, load_history, log_counts
from dfs.ingest.stats.nfl import (
    RELEASE_TAGS, _normalise_row, season_urls, week_date,
)
from dfs.sports import get_config, score_stat_line

needs_network = pytest.mark.skipif(
    not os.environ.get("DFS_NETWORK_TESTS"),
    reason="set DFS_NETWORK_TESTS=1 to run tests that download from nflverse",
)


def _row(**overrides):
    row = {
        "player_display_name": "Test Player",
        "position": "WR",
        "week": "3",
        "team": "buf",
        "opponent_team": "kc",
        "receptions": "7",
        "receiving_yards": "104",
        "receiving_tds": "1",
        "targets": "10",
        "carries": "1",
        "rushing_yards": "8",
    }
    row.update(overrides)
    return row


def test_columns_map_onto_the_scoring_keys():
    record = _normalise_row(_row(), 2024)
    assert record["stats"]["rec"] == 7
    assert record["stats"]["rec_yd"] == 104
    assert record["stats"]["rec_td"] == 1
    assert record["stats"]["rush_yd"] == 8


def test_the_mapped_stats_actually_score():
    """The mapping is only correct if the scoring table recognises it."""

    record = _normalise_row(_row(), 2024)
    points = score_stat_line(record["stats"], ["WR"], get_config("NFL", "DK"))
    # 7 receptions + 104 receiving yards + a touchdown + 8 rushing yards,
    # plus the 100-yard receiving bonus.
    assert points == pytest.approx(7 + 10.4 + 6 + 0.8 + 3)


def test_zero_valued_stats_are_dropped():
    """Keeps the stored JSON small; absent and zero score identically."""

    record = _normalise_row(_row(receiving_tds="0"), 2024)
    assert "rec_td" not in record["stats"]


def test_fumbles_are_summed_across_phases():
    record = _normalise_row(
        _row(sack_fumbles_lost="1", rushing_fumbles_lost="1", receiving_fumbles_lost="0"),
        2024,
    )
    assert record["stats"]["fumble_lost"] == 2


def test_two_point_conversions_are_summed_across_phases():
    record = _normalise_row(
        _row(passing_2pt_conversions="1", rushing_2pt_conversions="1"), 2024
    )
    assert record["stats"]["two_point_conv"] == 2


def test_opportunity_for_a_skill_player_is_carries_plus_targets():
    record = _normalise_row(_row(carries="4", targets="9"), 2024)
    assert record["opportunity"] == 13


def test_opportunity_for_a_quarterback_is_attempts_plus_carries():
    record = _normalise_row(
        _row(position="QB", attempts="38", carries="5", targets="0"), 2024
    )
    assert record["opportunity"] == 43


def test_non_fantasy_positions_are_dropped():
    for position in ("C", "OT", "CB", "LB", "K"):
        assert _normalise_row(_row(position=position), 2024) is None


def test_backfield_labels_normalise_onto_rb():
    """Both salary files call these RB, so the join needs one label."""

    for position in ("FB", "HB"):
        assert _normalise_row(_row(position=position), 2024)["positions"] == ["RB"]


def test_rows_without_a_name_or_week_are_dropped():
    assert _normalise_row(_row(player_display_name=""), 2024) is None
    assert _normalise_row(_row(week="0"), 2024) is None


def test_missing_and_na_values_are_treated_as_zero():
    record = _normalise_row(_row(receiving_yards="NA", receptions=""), 2024)
    assert "rec_yd" not in record["stats"]
    assert "rec" not in record["stats"]


def test_teams_are_upper_cased_for_the_salary_join():
    record = _normalise_row(_row(), 2024)
    assert record["team"] == "BUF"
    assert record["opponent"] == "KC"


def test_player_id_matches_the_salary_parser():
    """The join that makes any of this work."""

    from dfs.ingest.salaries import parse_salaries

    record = _normalise_row(_row(player_display_name="Ja'Marr Chase"), 2024)
    csv = (
        "Position,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,AvgPointsPerGame\n"
        "WR,Ja'Marr Chase,1,WR,8800,CIN@BAL 09/07/2026 01:00PM ET,CIN,19.4\n"
    )
    assert parse_salaries(csv, "NFL", "DK")[0]["player_id"] == record["player_id"]


def test_week_one_lands_on_the_opening_sunday():
    # The 2024 season opened Thursday 5 September; the first Sunday was the 8th.
    assert week_date(2024, 1) == "2024-09-08"


def test_weeks_advance_by_seven_days():
    assert week_date(2024, 2) == "2024-09-15"
    assert week_date(2024, 10) == "2024-11-10"


def test_late_weeks_roll_into_the_next_calendar_year():
    """A January game must sort after a December one, not before it."""

    assert week_date(2024, 18) > week_date(2024, 17) > "2024-12-01"
    assert week_date(2024, 18).startswith("2025")


def test_both_nflverse_release_names_are_tried():
    """nflverse renamed the release; only the new one carries 2025 on."""

    urls = season_urls(2025)
    assert len(urls) == len(RELEASE_TAGS) == 2
    assert "/stats_player/" in urls[0]
    assert "/player_stats/" in urls[1]
    assert all(url.endswith("stats_player_week_2025.csv") for url in urls)


def test_the_newer_release_name_is_preferred():
    """Ordering matters: the old release stops at 2024 and would win ties."""

    assert RELEASE_TAGS[0] == "stats_player"


def test_the_registry_knows_which_sports_are_built():
    from tests.fixtures import unbuilt_sport

    assert has_collector("NFL")
    assert has_collector("nfl")
    assert not has_collector(unbuilt_sport())


def test_an_unbuilt_sport_names_its_planned_source(tmp_path):
    # Derived from the registry, not hardcoded. Naming a built sport
    # here would make this download real seasons instead of raising --
    # a false failure, and minutes added to the suite.
    from dfs.ingest.stats import PLANNED
    from tests.fixtures import unbuilt_sport

    sport = unbuilt_sport()
    database = Database(tmp_path / "t.db")

    with pytest.raises(CollectorError) as raised:
        load_history(sport, database)

    assert PLANNED[sport] in str(raised.value)


def test_every_unbuilt_sport_has_a_planned_source_recorded():
    from dfs.sports import SPORTS
    from dfs.ingest.stats import COLLECTORS

    for sport in SPORTS:
        assert sport in COLLECTORS or sport in PLANNED


def test_log_counts_reports_per_sport(tmp_path):
    database = Database(tmp_path / "t.db")
    assert log_counts(database) == {}

    database.upsert_player(
        {"player_id": "nfl:x", "name": "X", "sport": "NFL", "positions": ["WR"]}
    )
    database.save_game_log(
        {"player_id": "nfl:x", "sport": "NFL", "game_date": "2024-09-08",
         "opportunity": 8, "stats": {"rec": 5}}
    )
    assert log_counts(database) == {"NFL": 1}


@needs_network
def test_seasons_are_discovered_from_the_live_feed():
    from dfs.ingest.stats.nfl import available_seasons

    seasons = available_seasons(count=2)
    assert len(seasons) == 2
    assert seasons == sorted(seasons)


@needs_network
def test_a_season_only_the_new_release_carries_is_found():
    """The regression that let the collector sit a year out of date."""

    from dfs.ingest.stats.nfl import resolve_season_url

    url = resolve_season_url(2025)
    assert url is not None
    assert "/stats_player/" in url


@needs_network
def test_a_real_season_loads_into_the_database(tmp_path):
    database = Database(tmp_path / "live.db")
    summary = load_history("NFL", database, count=1)

    assert summary["logs"] > 3_000
    assert summary["players"] > 300
    assert log_counts(database)["NFL"] == summary["logs"]
