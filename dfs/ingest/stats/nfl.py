"""Collect NFL weekly player stats from nflverse into `game_logs`.

nflverse publishes one CSV per season of per-player, per-week box score
lines, free and without a key. It is the best open football data there
is, and it is the difference between projections that mean something and
projections that are just the season average the site already prints.

Two things here are less obvious than they look.

**Seasons are discovered, not assumed.** Which seasons exist changes over
time, and a collector that hardcodes a list either misses the current
season or 404s on one that has not been published. Probing costs four
HEAD requests and means the collector picks up a new season the day it
appears with no code change.

**Opportunity is touches, not snaps.** The projection engine splits
playing time from efficiency, and for football the useful denominator is
the ball in a player's hands: pass attempts plus carries for a
quarterback, carries plus targets for everyone else. Snap counts are a
worse denominator despite sounding more precise, because a receiver who
runs forty routes and is targeted twice did not have forty
opportunities -- he had two, and dividing his output by snaps would
understate his efficiency and overstate the stability of his role.
"""

from __future__ import annotations

import csv
import io
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

from dfs.ingest.salaries import player_id as make_player_id
from dfs.ingest.stats.common import write_records

# nflverse renamed the release holding these files from `player_stats`
# to `stats_player`, and only the new name carries seasons after 2024.
# Both are tried, newest naming first, because the old release still
# holds the earlier seasons and dropping it would lose them.
#
# This is worth a comment because of how it failed: nothing errored. The
# collector kept reporting 2024 as the newest season long after 2025 was
# published, and every NFL projection was quietly built on roles a year
# out of date. A hardcoded upstream path is exactly the kind of
# dependency that breaks silently, which is why `available_seasons`
# probes rather than assumes -- and now probes both spellings.
RELEASE_TAGS = ("stats_player", "player_stats")

BASE_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download"
    "/{tag}/stats_player_week_{season}.csv"
)


def season_urls(season: int) -> list[str]:
    """Candidate download URLs for a season, newest naming first."""

    return [BASE_URL.format(tag=tag, season=season) for tag in RELEASE_TAGS]

# Seasons worth probing. The lower bound is arbitrary but generous; the
# upper bound runs a year past the present so a newly published season is
# found without a code change.
FIRST_SEASON = 2016

# nflverse column -> the stat key the NFL scoring tables use. Anything not
# listed is ignored, which is most of the file: it carries EPA, air
# yards, target share and much else that no scoring system pays for.
STAT_COLUMNS = {
    "passing_yards": "pass_yd",
    "passing_tds": "pass_td",
    "passing_interceptions": "pass_int",
    "rushing_yards": "rush_yd",
    "rushing_tds": "rush_td",
    "receptions": "rec",
    "receiving_yards": "rec_yd",
    "receiving_tds": "rec_td",
    "special_teams_tds": "return_td",
}

# Stats the sites score as one number but nflverse splits across the
# phase of play they happened in.
FUMBLE_COLUMNS = (
    "sack_fumbles_lost",
    "rushing_fumbles_lost",
    "receiving_fumbles_lost",
)

TWO_POINT_COLUMNS = (
    "passing_2pt_conversions",
    "rushing_2pt_conversions",
    "receiving_2pt_conversions",
)

# Positions the fantasy sites roster. nflverse carries every player who
# recorded a snap, including linemen and defenders who are not rosterable
# in classic contests.
FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "FB", "HB"}


class CollectorError(RuntimeError):
    """Raised when the upstream data cannot be retrieved."""


def _as_float(value: Any) -> float:
    try:
        return float(value) if value not in (None, "", "NA") else 0.0
    except (TypeError, ValueError):
        return 0.0


def week_date(season: int, week: int) -> str:
    """An approximate calendar date for a season and week.

    `game_logs` is keyed and filtered by date, because the projection
    engine takes a `before` cutoff so a backtest cannot see the game it
    is predicting. nflverse supplies season and week rather than a date,
    so one is derived: NFL week one opens on the first Thursday falling
    on or after 4 September, and each week advances seven days. The
    Sunday of that week is used, since that is when most games are
    played.

    Approximate by design. It is used for ordering and for the cutoff,
    never for display, and it is monotonic within a season, which is all
    either use requires.
    """

    anchor = date(season, 9, 4)
    # weekday(): Monday is 0, so Thursday is 3.
    days_to_thursday = (3 - anchor.weekday()) % 7
    week_one_thursday = anchor + timedelta(days=days_to_thursday)
    sunday = week_one_thursday + timedelta(days=3 + (week - 1) * 7)
    return sunday.isoformat()


def _open(url: str, timeout: int = 90):
    request = urllib.request.Request(url, headers={"User-Agent": "dfs-edge/1.0"})
    return urllib.request.urlopen(request, timeout=timeout)


def season_is_available(season: int, timeout: int = 30) -> bool:
    """Whether nflverse has published this season under either naming."""

    return resolve_season_url(season, timeout) is not None


def resolve_season_url(season: int, timeout: int = 30) -> str | None:
    """The URL that actually serves this season, or None if neither does."""

    for url in season_urls(season):
        request = urllib.request.Request(
            url, headers={"User-Agent": "dfs-edge/1.0"}, method="HEAD"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if 200 <= response.status < 300:
                    return url
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            continue

    return None


def available_seasons(last: int | None = None, count: int = 3) -> list[int]:
    """The most recent `count` seasons nflverse actually has.

    Walks backwards from next calendar year, so a season published
    mid-collection is found without changing anything here.
    """

    if last is None:
        last = date.today().year + 1

    found: list[int] = []
    for season in range(last, FIRST_SEASON - 1, -1):
        if season_is_available(season):
            found.append(season)
        if len(found) >= count:
            break

    return sorted(found)


def fetch_season(season: int) -> Iterator[dict[str, Any]]:
    """Download one season and yield normalised game-log records."""

    url = resolve_season_url(season)

    if url is None:
        raise CollectorError(
            f"nflverse has no file for {season} under either release name "
            f"({', '.join(RELEASE_TAGS)})."
        )

    try:
        with _open(url) as response:
            payload = response.read().decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as error:
        raise CollectorError(f"Could not download {url}: {error}") from error

    for row in csv.DictReader(io.StringIO(payload)):
        record = _normalise_row(row, season)
        if record is not None:
            yield record


def _normalise_row(row: dict[str, str], season: int) -> dict[str, Any] | None:
    position = (row.get("position") or "").upper()
    if position not in FANTASY_POSITIONS:
        return None

    name = row.get("player_display_name") or row.get("player_name") or ""
    if not name:
        return None

    try:
        week = int(float(row.get("week") or 0))
    except (TypeError, ValueError):
        return None
    if week <= 0:
        return None

    stats = {key: _as_float(row.get(column)) for column, key in STAT_COLUMNS.items()}
    stats["fumble_lost"] = sum(_as_float(row.get(column)) for column in FUMBLE_COLUMNS)
    stats["two_point_conv"] = sum(_as_float(row.get(column)) for column in TWO_POINT_COLUMNS)

    # Touches, not snaps -- see the module docstring.
    attempts = _as_float(row.get("attempts"))
    carries = _as_float(row.get("carries"))
    targets = _as_float(row.get("targets"))
    opportunity = attempts + carries if position == "QB" else carries + targets

    # Normalise the two backfield labels nflverse uses onto RB, which is
    # what both salary files call the position.
    if position in {"FB", "HB"}:
        position = "RB"

    return {
        "player_id": make_player_id(name, "NFL"),
        "name": name,
        "sport": "NFL",
        "positions": [position],
        "team": (row.get("team") or "").upper() or None,
        "opponent": (row.get("opponent_team") or "").upper() or None,
        "game_date": week_date(season, week),
        "opportunity": round(opportunity, 2),
        "stats": {key: round(value, 3) for key, value in stats.items() if value},
        "season": season,
        "week": week,
    }


def load_nfl_history(
    database,
    seasons: list[int] | None = None,
    count: int = 3,
    since: str | None = None,
) -> dict[str, Any]:
    """Fetch recent seasons and write them into `game_logs`.

    `since` collects only games on or after that date, which is what
    makes an in-season refresh cheap -- a weekly update reads one season
    and writes the handful of rows that are new.

    Returns a summary rather than printing, so the caller decides how to
    report: the CLI prints it, the Streamlit board renders it, and the
    tests assert on it.
    """

    if seasons is None:
        seasons = available_seasons(count=count)

    if not seasons:
        raise CollectorError(
            "nflverse published no seasons that could be reached. Check network "
            "access to github.com."
        )

    records = []
    for season in seasons:
        for record in fetch_season(season):
            if since and record["game_date"] < since:
                continue
            records.append(record)

    summary = write_records(database, records, "NFL")
    summary["sport"] = "NFL"
    summary["seasons"] = seasons
    return summary
