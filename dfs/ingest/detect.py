"""Work out which sport, site and date a salary file belongs to.

Uploading five files and then telling the app what each one is, five
times, is the kind of step that stops a tool being used. The files
already say: the site is legible from the header row, and the sport
from the positions inside.

Detection is deliberately allowed to fail. Guessing wrong here is
expensive -- an NBA file read as NFL parses cleanly, produces a pool
eligible for no roster slot, and yields an empty build that looks like
a solver problem -- so a file that does not clearly match one sport is
reported as undetermined rather than assigned to the closest thing.
"""

from __future__ import annotations

import csv
import dataclasses
import io
import re
from typing import Any

from dfs.sports import CONFIGS, SITES, SPORTS

# Header columns unique to each site's export. DraftKings writes a
# season average as `AvgPointsPerGame`; FanDuel calls the same number
# `FPPG` and splits the name into three columns.
DK_COLUMNS = frozenset({"teamabbrev", "avgpointspergame", "game info", "name + id"})
FD_COLUMNS = frozenset({"fppg", "nickname", "injury indicator", "first name"})

# A share of the file's players this low means the vocabulary does not
# describe the file, whatever the ranking says.
MIN_COVERAGE = 0.80

# How far ahead of the runner-up the winner must be. Sports share
# position letters -- C is a centre, a catcher and a center -- so a
# close call is a real ambiguity rather than a narrow win.
MIN_MARGIN = 0.15


@dataclasses.dataclass(frozen=True)
class Detection:
    """What a file appears to be, and how sure that is."""

    sport: str | None
    site: str | None
    slate_date: str | None
    coverage: float = 0.0
    reason: str = ""

    @property
    def complete(self) -> bool:
        return bool(self.sport and self.site)


def _rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def detect_site(text: str) -> str | None:
    """DK or FD, from the header row alone."""

    reader = csv.reader(io.StringIO(text))
    try:
        header = {column.strip().lower() for column in next(reader)}
    except StopIteration:
        return None

    if header & FD_COLUMNS:
        return "FD"
    if header & DK_COLUMNS:
        return "DK"
    return None


def _positions(rows: list[dict[str, str]]) -> list[set[str]]:
    listed = []
    for row in rows:
        raw = row.get("Position") or row.get("position") or ""
        parts = {p.strip().upper() for p in re.split(r"[/,]", str(raw)) if p.strip()}
        if parts:
            listed.append(parts)
    return listed


def sport_coverage(text: str) -> dict[str, float]:
    """The share of players each sport's vocabulary recognises."""

    listed = _positions(_rows(text))

    if not listed:
        return {}

    scores = {}
    for sport in SPORTS:
        vocabulary = {
            position
            for key, config in CONFIGS.items()
            if config.sport == sport
            for slot in config.roster
            for position in slot.eligible
        }
        matched = sum(1 for positions in listed if positions & vocabulary)
        scores[sport] = matched / len(listed)

    return scores


def detect_sport(text: str) -> tuple[str | None, float, str]:
    """The sport whose positions describe this file, if one clearly does."""

    scores = sport_coverage(text)

    if not scores:
        return None, 0.0, "The file has no Position column, or no rows."

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    (best, best_score), (runner_up, runner_score) = ranked[0], ranked[1]

    if best_score < MIN_COVERAGE:
        return None, best_score, (
            f"No sport recognises these positions: the closest is {best} at "
            f"{best_score:.0%} of players."
        )

    if best_score - runner_score < MIN_MARGIN:
        return None, best_score, (
            f"Ambiguous: {best} ({best_score:.0%}) and {runner_up} "
            f"({runner_score:.0%}) both fit."
        )

    return best, best_score, ""


# DraftKings writes the matchup and kickoff together: "BAL@KC 09/05/2026
# 08:20PM ET". FanDuel's equivalent column carries no date at all.
_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


def detect_date(text: str) -> str | None:
    """The slate's date, when the file records one.

    The earliest date found, because a slate spanning a Sunday and a
    Monday night game is named for when it starts.
    """

    found = []
    for row in _rows(text):
        raw = row.get("Game Info") or row.get("Game") or ""
        match = _DATE.search(str(raw))
        if match:
            month, day, year = match.groups()
            found.append(f"{year}-{int(month):02d}-{int(day):02d}")

    return min(found) if found else None


def detect(text: str) -> Detection:
    """Everything that can be read off one salary file."""

    site = detect_site(text)
    sport, coverage, reason = detect_sport(text)

    if site is None:
        reason = (reason + " The header matches neither site's export.").strip()

    return Detection(
        sport=sport,
        site=site,
        slate_date=detect_date(text),
        coverage=coverage,
        reason=reason,
    )
