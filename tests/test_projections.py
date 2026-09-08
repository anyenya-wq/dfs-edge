"""The projection model: opportunity split, shrinkage, and dispersion."""

from __future__ import annotations

import pytest

from dfs.projections.baseline import (
    DEFAULT_HALF_LIFE,
    clamp_multiplier,
    ewma,
    project_player,
    weighted_stdev,
)
from dfs.sports import get_config
from tests.fixtures import make_game_logs

CONFIG = get_config("NBA", "DK")
PLAYER = {"player_id": "nba:test", "name": "Test", "positions": ["PG"]}

STARTER_LINE = {"pts": 28, "reb": 8, "ast": 8, "stl": 1, "blk": 0, "tov": 4, "fg3m": 3}
BENCH_LINE = {"pts": 6, "reb": 2, "ast": 2, "stl": 0, "blk": 0, "tov": 1, "fg3m": 1}


def _logs(count=10, opportunity=34.0, stats=None):
    return [
        {"opportunity": opportunity, "opponent": "OPP", "stats": stats or STARTER_LINE}
        for _ in range(count)
    ]


def test_ewma_weights_recent_observations_more():
    """Values are most-recent-first, so a rising series pulls upward."""

    assert ewma([30, 10, 10, 10]) > sum([30, 10, 10, 10]) / 4


def test_ewma_of_a_constant_series_is_that_constant():
    assert ewma([12.0] * 8) == pytest.approx(12.0)


def test_ewma_of_nothing_is_zero():
    assert ewma([]) == 0.0


def test_weighted_stdev_of_a_constant_series_is_zero():
    assert weighted_stdev([5.0] * 6, 5.0) == pytest.approx(0.0)


def test_projection_scales_with_opportunity():
    """The core claim: minutes and rate are separable."""

    projection = project_player(PLAYER, _logs(), CONFIG)
    assert projection.projected_opportunity == pytest.approx(34.0, abs=0.1)
    assert projection.projected_points > 40


def test_research_can_override_opportunity_without_touching_the_rate():
    """The late-news scenario the whole split exists to serve."""

    logs = _logs(opportunity=12.0, stats=BENCH_LINE)
    normal = project_player(PLAYER, logs, CONFIG)
    promoted = project_player(PLAYER, logs, CONFIG, opportunity_override=34.0)

    assert promoted.per_opportunity_rate == pytest.approx(normal.per_opportunity_rate)
    assert promoted.projected_points == pytest.approx(
        normal.projected_points * 34.0 / 12.0, rel=0.02
    )
    assert "research" in promoted.notes.lower()


def test_games_not_played_do_not_drag_the_rate_down():
    """A DNP says nothing about how well someone plays."""

    healthy = project_player(PLAYER, _logs(), CONFIG)
    with_dnps = project_player(
        PLAYER,
        [{"opportunity": 0, "opponent": "OPP", "stats": {}}] * 3 + _logs(),
        CONFIG,
    )
    assert with_dnps.per_opportunity_rate == pytest.approx(healthy.per_opportunity_rate)


def test_games_not_played_do_lower_expected_opportunity():
    """They say a great deal about whether someone will play."""

    healthy = project_player(PLAYER, _logs(), CONFIG)
    with_dnps = project_player(
        PLAYER,
        [{"opportunity": 0, "opponent": "OPP", "stats": {}}] * 3 + _logs(),
        CONFIG,
    )
    assert with_dnps.projected_opportunity < healthy.projected_opportunity


def test_thin_history_is_shrunk_toward_the_positional_prior():
    """One huge game must not become the projection."""

    monster = {"pts": 45, "reb": 12, "ast": 12, "stl": 3, "blk": 2, "tov": 1, "fg3m": 7}
    thin = project_player(
        PLAYER,
        [{"opportunity": 30, "opponent": "OPP", "stats": monster}],
        CONFIG,
        pool_rates={"PG": [0.5, 0.55, 0.6, 0.45]},
    )

    assert thin.games_used == 1
    assert "thin history" in thin.notes.lower()
    # The raw rate would be well above 2.0; the prior drags it far down.
    assert thin.per_opportunity_rate < 1.0


def test_long_history_is_barely_shrunk():
    rich = project_player(PLAYER, _logs(count=40), CONFIG, pool_rates={"PG": [0.1]})
    assert rich.notes == ""
    assert rich.per_opportunity_rate > 1.0


def test_no_history_leans_entirely_on_the_prior():
    projection = project_player(PLAYER, [], CONFIG, pool_rates={"PG": [0.6]})
    assert projection.games_used == 0
    assert "no usable history" in projection.notes.lower()


def test_ceiling_and_floor_bracket_the_projection():
    projection = project_player(PLAYER, _logs(), CONFIG)
    assert projection.floor <= projection.projected_points <= projection.ceiling


def test_floor_never_goes_negative():
    volatile = make_game_logs("p", games=10, opportunity=20, stats=BENCH_LINE)
    projection = project_player(PLAYER, volatile, CONFIG)
    assert projection.floor >= 0.0


def test_a_short_history_is_not_credited_with_low_variance():
    """Consistency needs evidence; the CV floor supplies the default."""

    identical = _logs(count=3)
    projection = project_player(PLAYER, identical, CONFIG)
    assert projection.stdev == pytest.approx(projection.projected_points * 0.25, rel=0.01)


def test_site_scoring_changes_the_projection():
    dk = project_player(PLAYER, _logs(), get_config("NBA", "DK"))
    fd = project_player(PLAYER, _logs(), get_config("NBA", "FD"))
    assert dk.projected_points != fd.projected_points


def test_matchup_multipliers_are_clamped():
    assert clamp_multiplier(3.0) == 1.25
    assert clamp_multiplier(0.0) == 0.75
    assert clamp_multiplier(1.1) == pytest.approx(1.1)
