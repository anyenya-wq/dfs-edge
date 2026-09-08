"""Ownership projection, leverage, and the simulated optimal rate."""

from __future__ import annotations

import pytest

from dfs.ingest.salaries import expand_roster_eligibility
from dfs.optimizer.rules import cash_settings
from dfs.ownership.leverage import (
    MAX_OWNERSHIP,
    apply_leverage,
    gpp_score,
    leverage_board,
    project_ownership,
    value_per_thousand,
)
from dfs.ownership.simulation import apply_simulated_leverage, simulate_optimal_rates
from dfs.sports import get_config
from tests.fixtures import make_pool

CONFIG = get_config("NBA", "DK")


def _pool(**kwargs):
    pool = expand_roster_eligibility(make_pool(CONFIG, **kwargs), CONFIG)
    for player in pool:
        player["projected_ownership"] = None
    return pool


def test_ownership_sums_to_the_contest_budget():
    """Every entry rosters roster_size players, so this total is fixed."""

    ownership = project_ownership(_pool(), CONFIG.roster_size)
    assert sum(ownership.values()) == pytest.approx(CONFIG.roster_size * 100, rel=0.01)


def test_a_pool_of_equal_value_yields_flat_ownership():
    """The failure a z-score formulation has: noise amplified into spread."""

    pool = [
        {"player_id": f"p{index}", "salary": 5_000, "projected_points": 15.0}
        for index in range(100)
    ]
    ownership = project_ownership(pool, 8)

    assert max(ownership.values()) == pytest.approx(min(ownership.values()))
    assert sum(ownership.values()) == pytest.approx(800, rel=0.01)


def test_better_value_draws_more_ownership():
    # Padded to a realistic size: a two-player pool cannot absorb eight
    # roster spots' worth of ownership, so both entries clip at the
    # ceiling and the comparison becomes meaningless.
    pool = [
        {"player_id": "cheap_good", "salary": 4_000, "projected_points": 30.0},
        {"player_id": "pricey_bad", "salary": 10_000, "projected_points": 20.0},
    ]
    pool += [
        {"player_id": f"filler{index}", "salary": 6_000, "projected_points": 18.0}
        for index in range(60)
    ]

    ownership = project_ownership(pool, 8)
    assert ownership["cheap_good"] > ownership["pricey_bad"]


def test_a_pool_too_small_to_absorb_the_budget_caps_everyone():
    """Degenerate but well-defined: two players cannot carry eight spots."""

    pool = [
        {"player_id": "a", "salary": 4_000, "projected_points": 30.0},
        {"player_id": "b", "salary": 10_000, "projected_points": 20.0},
    ]
    ownership = project_ownership(pool, 8)
    assert set(ownership.values()) == {MAX_OWNERSHIP}


def test_the_ownership_ceiling_redistributes_rather_than_discards():
    """Clipping without redistribution loses part of the contest budget."""

    pool = [{"player_id": "star", "salary": 3_000, "projected_points": 60.0}]
    pool += [
        {"player_id": f"p{index}", "salary": 9_000, "projected_points": 5.0}
        for index in range(40)
    ]

    ownership = project_ownership(pool, 8)
    assert ownership["star"] <= MAX_OWNERSHIP
    assert sum(ownership.values()) == pytest.approx(800, rel=0.02)


def test_supplied_ownership_is_not_overwritten():
    """Real data beats the model and must survive it."""

    pool = _pool()
    pool[0]["projected_ownership"] = 42.0
    apply_leverage(pool, CONFIG.roster_size)
    assert pool[0]["projected_ownership"] == 42.0


def test_value_is_points_per_thousand_dollars():
    assert value_per_thousand({"salary": 5_000, "projected_points": 25.0}) == 5.0
    assert value_per_thousand({"salary": 0, "projected_points": 25.0}) == 0.0


def test_leverage_runs_between_minus_one_and_one():
    pool = apply_leverage(_pool(), CONFIG.roster_size)
    for player in pool:
        assert -1.0 <= player["leverage"] <= 1.0


def test_leverage_board_is_sorted_best_first():
    board = leverage_board(apply_leverage(_pool(), CONFIG.roster_size))
    assert board == sorted(board, key=lambda row: -row["leverage"])


def test_gpp_score_penalises_ownership():
    chalk = {"projected_points": 30.0, "ceiling": 45.0, "projected_ownership": 40.0}
    quiet = {"projected_points": 30.0, "ceiling": 45.0, "projected_ownership": 2.0}
    assert gpp_score(quiet) > gpp_score(chalk)


def test_optimal_rates_sum_to_the_roster_budget():
    """Each simulated slate contributes exactly roster_size appearances."""

    pool = apply_leverage(_pool(games=3), CONFIG.roster_size)
    rates = simulate_optimal_rates(pool, CONFIG, cash_settings(CONFIG), iterations=25, seed=3)
    assert sum(rates.values()) == pytest.approx(CONFIG.roster_size * 100, rel=0.02)


def test_simulated_leverage_compares_like_with_like():
    """Optimal rate and ownership are both percentages, so subtraction works."""

    pool = apply_leverage(_pool(games=3), CONFIG.roster_size)
    apply_simulated_leverage(pool, CONFIG, cash_settings(CONFIG), iterations=25, seed=3)

    for player in pool:
        assert player["optimal_rate"] >= 0.0
        assert player["sim_leverage"] == pytest.approx(
            player["optimal_rate"] - player["projected_ownership"]
        )


def test_simulation_is_reproducible_given_a_seed():
    pool = apply_leverage(_pool(games=3), CONFIG.roster_size)
    first = simulate_optimal_rates(pool, CONFIG, cash_settings(CONFIG), iterations=15, seed=11)
    second = simulate_optimal_rates(pool, CONFIG, cash_settings(CONFIG), iterations=15, seed=11)
    assert first == second
