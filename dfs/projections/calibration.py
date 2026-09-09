"""Scoring projections against what actually happened.

The central question this answers is not "how accurate are my
projections" but "are they better than the number the site already
prints on the screen". Both DraftKings and FanDuel publish a season
average next to every player, and the field is anchored to it. A model
that cannot beat that average has no edge at all, however respectable
its absolute error looks -- fantasy scoring is noisy enough that a
mediocre model still posts a plausible-sounding mean absolute error.

So the headline number here is skill relative to that baseline, not
error. A skill score of zero means you have reproduced the site's
average and have no reason to enter. Above about 0.05 is a real edge.

The comparison is only honest because of how the data was collected:
projections carry a `locked_at` stamp and results land in a separate
table, so nothing can be revised after the fact. Unlocked projections
are drafts and are excluded from scoring entirely. That was the design
and for a while it was not the behaviour -- the write guard is in
`Database._PROJECTION_SQL`, added after a locked slate was found being
re-projected in place.

Two things a skill score does not say on its own, both of which flatter
a record and neither of which is visible in the count. It can be many
projections from a single slate, where the outcomes move together and
the effective sample is one evening. And it can be earned on players
who scored nothing, where beating the site average means projecting
lower rather than picking better. `_sample_warning` reports both.

Bias is reported alongside error because the two fail differently. A
model that is accurate on average but systematically over-projects
starters and under-projects bench players will look fine in aggregate
and lose money consistently, since the optimizer selects precisely on
the direction of the error. Per-position breakdowns exist to catch that.
"""

from __future__ import annotations

import dataclasses
import math
import statistics
from collections.abc import Mapping, Sequence
from typing import Any


@dataclasses.dataclass
class CalibrationResult:
    scope: str
    count: int
    mae: float
    rmse: float
    bias: float
    correlation: float
    baseline_mae: float | None = None
    skill: float | None = None
    # How many of the scored players finished on nothing, and how many
    # separate slates the rows came from. Both change what the skill
    # number above is worth, and neither was visible.
    zeros: int = 0
    slates: int = 1

    def as_row(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @property
    def degenerate(self) -> bool:
        """Every player in this group scored the same, so nothing is measured.

        A group where all the actuals are identical has no ordering to
        get right and no variance to explain. Its "skill" collapses to
        whether the model's mean sits nearer that one number than the
        site average does -- which for a group of players who all
        scored zero means nothing more than projecting lower. The two
        strongest skill scores in a real MLB record were exactly this,
        and read as the model's best positions.
        """

        return self.count > 1 and self.correlation == 0.0 and (
            abs(self.mae - abs(self.bias)) < 1e-9
        )


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Pearson correlation, returning 0.0 when undefined."""

    if len(xs) < 2:
        return 0.0

    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)

    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    variance_x = sum((x - mean_x) ** 2 for x in xs)
    variance_y = sum((y - mean_y) ** 2 for y in ys)

    if variance_x <= 0 or variance_y <= 0:
        return 0.0

    return covariance / math.sqrt(variance_x * variance_y)


def _rank(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)

    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        average = (position + end) / 2.0 + 1.0
        for index in range(position, end + 1):
            ranks[order[index]] = average
        position = end + 1

    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Rank correlation.

    Reported alongside Pearson because a lineup optimizer consumes the
    *ordering* of projections, not their levels. A model that ranks
    players perfectly but is uniformly ten points high builds exactly
    the same lineups as a perfect one, and Spearman is the metric that
    says so.
    """

    if len(xs) < 2:
        return 0.0
    return _pearson(_rank(xs), _rank(ys))


def score_projections(
    records: Sequence[Mapping[str, Any]],
    scope: str = "all",
) -> CalibrationResult | None:
    """Error, bias, and skill for a set of resolved projections.

    Each record needs `projected_points` and `actual_points`, and
    optionally `site_avg_points` for the baseline comparison.
    """

    usable = [
        record
        for record in records
        if record.get("projected_points") is not None
        and record.get("actual_points") is not None
    ]

    if not usable:
        return None

    projected = [float(record["projected_points"]) for record in usable]
    actual = [float(record["actual_points"]) for record in usable]
    errors = [p - a for p, a in zip(projected, actual)]

    mae = statistics.fmean(abs(error) for error in errors)
    rmse = math.sqrt(statistics.fmean(error**2 for error in errors))
    bias = statistics.fmean(errors)

    baseline_mae = None
    skill = None

    with_baseline = [
        (float(record["site_avg_points"]), float(record["actual_points"]))
        for record in usable
        if record.get("site_avg_points") is not None
    ]

    # Skill is only meaningful when the baseline is measured on the same
    # rows as the model. Comparing a model's error on every player to a
    # baseline's error on the subset that happened to have an average
    # would flatter whichever had the easier rows.
    if len(with_baseline) == len(usable) and with_baseline:
        baseline_mae = statistics.fmean(
            abs(average - actual_points) for average, actual_points in with_baseline
        )
        if baseline_mae > 0:
            skill = 1.0 - mae / baseline_mae

    slates = {
        (record.get("sport"), record.get("site"), record.get("slate_date"))
        for record in usable
    }

    return CalibrationResult(
        scope=scope,
        count=len(usable),
        mae=round(mae, 3),
        rmse=round(rmse, 3),
        bias=round(bias, 3),
        correlation=round(spearman(projected, actual), 4),
        baseline_mae=round(baseline_mae, 3) if baseline_mae is not None else None,
        skill=round(skill, 4) if skill is not None else None,
        zeros=sum(1 for value in actual if value == 0.0),
        slates=len(slates),
    )


def score_by_position(records: Sequence[Mapping[str, Any]]) -> list[CalibrationResult]:
    """Break calibration out by position.

    The breakdown that matters most. Aggregate error hides the failure
    that actually costs money -- being systematically high on one
    position -- because the optimizer picks on exactly that direction
    and will happily fill a roster with whichever group you over-project.
    """

    grouped: dict[str, list[Mapping[str, Any]]] = {}

    for record in records:
        positions = record.get("positions") or []
        if isinstance(positions, str):
            positions = [positions]
        for position in positions or ["UNKNOWN"]:
            grouped.setdefault(str(position), []).append(record)

    results = []
    for position, rows in sorted(grouped.items()):
        result = score_projections(rows, scope=position)
        if result:
            results.append(result)

    return results


def score_by_sport(records: Sequence[Mapping[str, Any]]) -> list[CalibrationResult]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        key = f"{record.get('sport', '?')}:{record.get('site', '?')}"
        grouped.setdefault(key, []).append(record)

    results = []
    for key, rows in sorted(grouped.items()):
        result = score_projections(rows, scope=key)
        if result:
            results.append(result)

    return results


def ownership_calibration(records: Sequence[Mapping[str, Any]]) -> CalibrationResult | None:
    """How well projected ownership matched what the field actually did.

    Only scorable once real ownership is available, which both sites
    publish after contests settle. Worth tracking separately because
    ownership errors and projection errors cost differently: a
    projection error costs points, an ownership error costs equity in
    the lineups that do hit.
    """

    usable = [
        {
            "projected_points": record["projected_ownership"],
            "actual_points": record["actual_ownership"],
        }
        for record in records
        if record.get("projected_ownership") is not None
        and record.get("actual_ownership") is not None
    ]

    if not usable:
        return None

    result = score_projections(usable, scope="ownership")
    return result


def split_forward(
    records: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Separate genuine forecasts from reconstructed ones.

    A projection written before its slate was played is a forecast made
    under real uncertainty. One written afterwards is a backtest: the
    engine still cannot see the game it is projecting, because game logs
    are read with a cutoff, so the number is honest -- but it was
    produced without the pressure of a lock time, and only the forward
    record can say whether the research layer helps, since a backtest
    has no injury news in it.

    Reported apart for that reason. Averaged together, a large backtest
    would drown out the small forward record that is the one actually
    worth watching.
    """

    forward = [record for record in records if record.get("forward")]
    backtest = [record for record in records if not record.get("forward")]
    return forward, backtest


def scored_something(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The rows where the player actually put points on the board.

    A player who finished on nothing is a real result and belongs in
    the headline, but he is not where an edge comes from: beating the
    site average on a man you would never roster is arithmetic, not
    skill. Reported separately so the two cannot be confused.

    Nothing here can tell a genuine zero from a player who never took
    the field -- the resolved record carries points and not minutes --
    and for this purpose they are the same thing.
    """

    return [
        record for record in records
        if record.get("actual_points") is not None
        and float(record["actual_points"]) != 0.0
    ]


def calibration_report(
    records: Sequence[Mapping[str, Any]],
    comparison: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Everything the dashboard shows about model quality.

    `comparison` is what the by-sport-and-site table is built from, and
    defaults to the same rows. The board passes every sport and site
    there while filtering `records` to the pair on screen, so the
    headline describes what the selectors say and the table still
    compares across them.
    """

    overall = score_projections(records)
    forward_records, backtest_records = split_forward(records)
    forward = score_projections(forward_records, scope="forward")
    backtest = score_projections(backtest_records, scope="backtest")
    played = score_projections(scored_something(records), scope="scored")
    forward_played = score_projections(
        scored_something(forward_records), scope="forward scored"
    )

    return {
        "overall": overall.as_row() if overall else None,
        "forward": forward.as_row() if forward else None,
        "backtest": backtest.as_row() if backtest else None,
        "played": played.as_row() if played else None,
        "forward_played": forward_played.as_row() if forward_played else None,
        "by_sport": [
            result.as_row()
            for result in score_by_sport(records if comparison is None else comparison)
        ],
        "by_position": [result.as_row() for result in score_by_position(records)],
        "ownership": (
            ownership_calibration(records).as_row()
            if ownership_calibration(records)
            else None
        ),
        "verdict": _verdict(overall),
        "forward_verdict": _forward_verdict(forward, backtest),
        "sample_warning": _sample_warning(forward or overall, played),
    }


# Distinct slates below which a record is one day of results wearing a
# large n. Fantasy outcomes are correlated within a slate -- the same
# parks, the same weather, the same handful of games -- so a thousand
# projections from one evening is closer to one observation than to a
# thousand.
MIN_SLATES = 5


def _sample_warning(
    result: CalibrationResult | None,
    played: CalibrationResult | None,
) -> str:
    """What the headline skill number is not telling you.

    Two failures, both of which make a record look stronger than it is
    and neither of which shows up in n. A record can be several hundred
    projections drawn from a single evening, and a record can earn most
    of its margin on players who scored nothing.
    """

    if result is None:
        return ""

    notes = []

    if result.slates < MIN_SLATES:
        notes.append(
            f"These {result.count:,} projections come from "
            f"{result.slates} {'slate' if result.slates == 1 else 'slates'}. "
            "Results within a slate share parks, weather and opponents, so "
            f"this is closer to {result.slates} "
            f"{'observation' if result.slates == 1 else 'observations'} than to "
            f"{result.count:,}. Around {MIN_SLATES} separate slates before the "
            "skill number is worth reading."
        )

    if result.zeros and result.count:
        share = result.zeros / result.count
        if share >= 0.2:
            note = (
                f"{result.zeros:,} of {result.count:,} ({share:.0%}) scored "
                "nothing. Beating the site average on those is a matter of "
                "projecting lower, not of picking better."
            )
            if played is not None and played.skill is not None:
                note += (
                    f" On the {played.count:,} who did score, skill is "
                    f"{played.skill:+.3f}."
                )
            notes.append(note)

    return " ".join(notes)


def _forward_verdict(
    forward: CalibrationResult | None,
    backtest: CalibrationResult | None,
) -> str:
    """What the forward record does and does not yet establish."""

    if forward is None:
        if backtest is None:
            return ""
        return (
            f"All {backtest.count:,} scored projections were reconstructed after "
            "the games. That measures the model, not the workflow — a backtest "
            "contains no injury news, which is where the design expects its edge. "
            "Lock a slate before its games to start a forward record."
        )

    if forward.count < 200:
        return (
            f"{forward.count:,} forward projections so far — too few to read. "
            "Several hundred are needed before a forward skill number means "
            "anything."
        )

    # A count gate alone passes a single slate the moment it resolves,
    # because one MLB pool is most of a thousand projections on its own.
    if forward.slates < MIN_SLATES:
        return (
            f"{forward.count:,} forward projections, but from "
            f"{forward.slates} {'slate' if forward.slates == 1 else 'slates'}. "
            "That is enough to show the loop works and not enough to show an "
            f"edge — lock {MIN_SLATES - forward.slates} more on separate days "
            "before reading the skill number."
        )

    if forward.skill is None:
        return f"{forward.count:,} forward projections, but no site baseline stored to compare against."

    comparison = ""
    if backtest is not None and backtest.skill is not None:
        difference = forward.skill - backtest.skill
        direction = "above" if difference > 0 else "below"
        comparison = (
            f" That is {abs(difference):.3f} {direction} the backtest's "
            f"{backtest.skill:+.3f}, which is the number that says whether "
            "researching players before lock actually pays."
        )

    return f"Forward skill {forward.skill:+.3f} over {forward.count:,} projections.{comparison}"


def _verdict(overall: CalibrationResult | None) -> str:
    """A plain-language read on whether the model is worth entering with."""

    if overall is None:
        return "No resolved projections yet. Lock a slate and record results."

    if overall.count < 100:
        return (
            f"Only {overall.count} resolved projections. Too few to judge -- "
            "fantasy scoring is noisy enough that several hundred are needed "
            "before skill separates from luck."
        )

    if overall.slates < MIN_SLATES:
        return (
            f"{overall.count:,} resolved projections from {overall.slates} "
            f"{'slate' if overall.slates == 1 else 'slates'}. Too few days to "
            "judge, whatever the count says: one slate's results move together, "
            f"so read this again after about {MIN_SLATES} separate days."
        )

    if overall.skill is None:
        return (
            "No site baseline stored, so skill cannot be measured. Keep the "
            "AvgPointsPerGame / FPPG column when importing salaries."
        )

    if overall.skill <= 0:
        return (
            f"Skill {overall.skill:+.3f}: the model is no better than the site's "
            "own season average. There is no edge here yet -- do not enter real "
            "contests on these projections."
        )

    if overall.skill < 0.05:
        return (
            f"Skill {overall.skill:+.3f}: marginally better than the site average. "
            "Real but thin; likely eaten by rake in anything but the softest fields."
        )

    return (
        f"Skill {overall.skill:+.3f}: meaningfully better than the site average. "
        f"Rank correlation {overall.correlation:.3f}. Watch the per-position bias "
        "before scaling up."
    )
