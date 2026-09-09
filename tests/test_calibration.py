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


def _records(
    count, model_noise, baseline_noise, bias=0.0, position="PG", seed=3, slates=8,
):
    """A believable resolved record.

    Spread across several slate dates by default, because that is what
    a record worth judging looks like. A few hundred projections from a
    single evening is one night of results wearing a large n, and the
    verdict now says so rather than reading the skill number.
    """

    generator = random.Random(seed)
    rows = []
    for index in range(count):
        true = generator.uniform(5, 45)
        rows.append(
            {
                "positions": [position],
                "sport": "NBA",
                "site": "DK",
                "slate_date": f"2026-01-{(index % slates) + 1:02d}",
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


# ----------------------------------------------------------------------
# What a skill number is not saying
# ----------------------------------------------------------------------
#
# Written from a real MLB record, where the headline read +0.089 and
# the two strongest position groups were players who never scored.


def _zero_group(count, projection, site_average, position="SP"):
    """A group where everyone finished on nothing."""

    return [
        {
            "positions": [position],
            "sport": "MLB",
            "site": "DK",
            "slate_date": "2026-09-09",
            "projected_points": projection,
            "site_avg_points": site_average,
            "actual_points": 0.0,
        }
        for _ in range(count)
    ]


def test_a_group_that_all_scored_zero_is_marked_degenerate():
    """The signature, taken from the real record: bias equals MAE
    exactly and correlation is exactly zero. Both follow from every
    actual being identical, and neither is visible as a problem in a
    table of ten position rows."""

    result = score_projections(_zero_group(62, 7.251, 9.106))

    assert result.degenerate
    assert result.zeros == result.count == 62
    assert result.bias == result.mae
    assert result.correlation == 0.0
    # And the "skill" is nothing but the ratio of the two means.
    assert result.skill == pytest.approx(1 - 7.251 / 9.106, abs=5e-5)


def test_a_group_with_real_outcomes_is_not_marked_degenerate():
    result = score_projections(_records(60, 3.0, 8.0))

    assert not result.degenerate
    assert result.correlation != 0.0


def test_the_scored_only_record_excludes_players_who_managed_nothing():
    records = _records(100, 3.0, 8.0) + _zero_group(50, 7.0, 9.0)

    report = calibration_report(records)
    overall, played = report["overall"], report["played"]

    # Not 100: a hitter who goes hitless scores zero too, and the
    # fixture clamps at zero for the same reason the sites do. What is
    # asserted is the relationship, since nothing here can tell a real
    # zero from a player who never took the field.
    assert overall["count"] == 150
    assert overall["zeros"] >= 50
    assert played["count"] == overall["count"] - overall["zeros"]
    assert played["zeros"] == 0


def test_a_record_carried_by_zero_scorers_says_so():
    """The warning that matters. A third of these scored nothing, and
    the margin on them is arithmetic rather than an edge."""

    records = _records(100, 6.0, 6.0, slates=8) + _zero_group(50, 7.0, 9.0)

    report = calibration_report(records)
    warning = report["sample_warning"]
    share = report["overall"]["zeros"] / report["overall"]["count"]

    assert "scored" in warning and "nothing" in warning
    assert f"{share:.0%}" in warning
    assert "projecting lower" in warning
    # And it says what the record looks like without them.
    assert f"{report['played']['skill']:+.3f}" in warning


def test_a_record_from_one_slate_is_not_read_as_an_edge():
    """826 projections from a single evening is not 826 observations."""

    report = calibration_report(_records(826, 3.0, 8.0, slates=1))

    assert "1 slate" in report["verdict"]
    assert "too few days" in report["verdict"].lower()
    assert "meaningfully better" not in report["verdict"].lower()


def test_a_forward_record_from_one_slate_says_the_loop_works_not_that_it_wins():
    rows = _records(826, 3.0, 8.0, slates=1)
    for row in rows:
        # The flag the database computes, not a timestamp: a projection
        # is forward when it was written on or before its slate date.
        row["forward"] = 1

    verdict = calibration_report(rows)["forward_verdict"]

    assert "loop works" in verdict
    assert "not enough to show an edge" in verdict


def test_slates_are_counted_across_sites_and_sports_not_just_dates():
    """Two sites on the same evening are two slates, not one day.

    They price and score differently, so a projection for each is a
    separate test of the model even though the games are the same.
    """

    rows = _records(40, 3.0, 8.0, slates=1)
    for index, row in enumerate(rows):
        row["site"] = "DK" if index % 2 else "FD"

    assert score_projections(rows).slates == 2


def test_the_comparison_table_can_span_what_the_headline_excludes():
    """The board filters the headline to one sport and site and still
    wants the table underneath to compare across them."""

    dk = _records(30, 3.0, 8.0)
    fd = _records(30, 3.0, 8.0, seed=9)
    for row in fd:
        row["site"] = "FD"

    report = calibration_report(dk, comparison=dk + fd)

    assert report["overall"]["count"] == 30
    assert {row["scope"] for row in report["by_sport"]} == {"NBA:DK", "NBA:FD"}
