"""Scoring projections against results, and the skill-versus-baseline read."""

from __future__ import annotations

import random

import pytest

from dfs.projections.calibration import (
    calibration_report,
    ownership_calibration,
    score_by_position,
    score_projections,
    spearman,
)


def _records(count, model_noise, baseline_noise, bias=0.0, position="PG", seed=3):
    generator = random.Random(seed)
    rows = []
    for _ in range(count):
        true = generator.uniform(5, 45)
        rows.append(
            {
                "positions": [position],
                "sport": "NBA",
                "site": "DK",
                "projected_points": true + generator.gauss(bias, model_noise),
                "site_avg_points": true + generator.gauss(0, baseline_noise),
                "actual_points": max(true + generator.gauss(0, 7), 0),
            }
        )
    return rows


def test_no_records_scores_to_nothing():
    assert score_projections([]) is None


def test_records_without_results_are_ignored():
    assert score_projections([{"projected_points": 20.0, "actual_points": None}]) is None


def test_a_perfect_model_has_zero_error():
    rows = [{"projected_points": 20.0, "actual_points": 20.0} for _ in range(10)]
    result = score_projections(rows)
    assert result.mae == 0.0
    assert result.bias == 0.0


def test_skill_is_positive_when_the_model_beats_the_site_average():
    result = score_projections(_records(400, model_noise=3.0, baseline_noise=8.0))
    assert result.skill > 0.1
    assert result.baseline_mae > result.mae


def test_skill_is_near_zero_when_it_does_not():
    result = score_projections(_records(400, model_noise=8.0, baseline_noise=8.0))
    assert abs(result.skill) < 0.1


def test_skill_is_negative_when_the_model_is_worse():
    result = score_projections(_records(400, model_noise=14.0, baseline_noise=6.0))
    assert result.skill < 0


def test_skill_is_withheld_without_a_complete_baseline():
    """Comparing different row sets would flatter whichever had easier rows."""

    rows = _records(50, 3.0, 8.0)
    del rows[0]["site_avg_points"]
    assert score_projections(rows).skill is None


def test_bias_carries_a_sign():
    over = score_projections(_records(300, 3.0, 8.0, bias=6.0))
    under = score_projections(_records(300, 3.0, 8.0, bias=-6.0))
    assert over.bias > 4
    assert under.bias < -4


def test_position_breakdown_isolates_a_biased_group():
    """The failure mode that costs money: one position systematically high."""

    rows = _records(300, 3.0, 8.0, bias=6.0, position="PG")
    rows += _records(300, 3.0, 8.0, bias=0.0, position="C", seed=9)

    by_position = {result.scope: result for result in score_by_position(rows)}

    assert by_position["PG"].bias > 4
    assert abs(by_position["C"].bias) < 2


def test_spearman_ignores_a_constant_offset():
    """A uniformly high model builds identical lineups, and rank says so."""

    values = [10, 20, 30, 40]
    assert spearman(values, [value + 10 for value in values]) == pytest.approx(1.0)


def test_spearman_detects_reversal():
    values = [10, 20, 30, 40]
    assert spearman(values, list(reversed(values))) == pytest.approx(-1.0)


def test_spearman_of_a_constant_series_is_zero():
    assert spearman([5, 5, 5], [1, 2, 3]) == 0.0


def test_the_verdict_refuses_to_judge_a_small_sample():
    report = calibration_report(_records(30, 3.0, 8.0))
    assert "too few" in report["verdict"].lower()


def test_the_verdict_warns_when_there_is_no_edge():
    report = calibration_report(_records(400, 14.0, 6.0))
    assert "no edge" in report["verdict"].lower()


def test_the_verdict_reports_a_real_edge():
    report = calibration_report(_records(400, 3.0, 8.0))
    assert "meaningfully better" in report["verdict"].lower()


def test_an_empty_record_set_says_so():
    assert "no resolved" in calibration_report([])["verdict"].lower()


def test_ownership_is_scored_separately():
    rows = [
        {"projected_ownership": 20.0, "actual_ownership": 22.0},
        {"projected_ownership": 5.0, "actual_ownership": 4.0},
    ]
    assert ownership_calibration(rows).count == 2
    assert ownership_calibration([{"projected_ownership": 1.0}]) is None
