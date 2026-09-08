"""Collect NBA box scores from hoopR into `game_logs`.

hoopR publishes ESPN box scores as one parquet file per season in the
`sportsdataverse/hoopR-nba-data` repository, free and without a key. It
is used here in preference to `nba_api` for one practical reason:
`nba_api` talks to stats.nba.com, which rate-limits aggressively and
refuses datacenter addresses outright, so a collector built on it works
on a laptop and fails in exactly the hosted environment this app runs
in. Files served from a git host have neither problem.

Basketball is the sport the projection engine's structure was designed
for. Minutes are published directly, so the opportunity term needs no
proxy -- unlike football, where touches stand in for a snap count that
is not in the feed. And minutes are volatile in the specific way that
creates edge: a starter ruled out reassigns twenty-five of them to
someone whose salary has not moved.

Games a player missed are collected too, with zero opportunity rather
than being dropped. That is deliberate. The projection engine treats a
did-not-play as evidence about availability but not about ability -- it
lowers expected minutes while leaving the per-minute rate alone -- and
it can only do that if the zeros are actually there.
"""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from datetime import date
from typing import Any

import pandas as pd

from dfs.ingest.salaries import player_id as make_player_id
from dfs.ingest.stats.common import write_records
from dfs.ingest.stats.nfl import CollectorError

BASE_URL = (
    "https://raw.githubusercontent.com/sportsdataverse/hoopR-nba-data/main"
    "/nba/player_box/parquet/player_box_{season}.parquet"
)

# hoopR labels a season by the calendar year it ends in, so 2026 is the
# 2025-26 season. The lower bound is well before anything useful for
# projections and exists only to stop the probe walking forever.
FIRST_SEASON = 2003

# Fewest distinct games a team must appear in for its rows to count as
# real basketball. Exhibitions are the problem this solves: All-Star
# rosters are labelled season_type 2, the same as the regular season, so
# that field cannot separate them, and their team codes (STARS, STRIPES,
# WORLD, EAST, WEST and others that change yearly) are not worth
# maintaining a blocklist for.
#
# Games played separates them cleanly and maintains itself. A real team
# plays 82 and an exhibition side plays one or two, so anything in
# between is safely on either side of this line -- a relocated or
# renamed franchise still plays a full season and survives, while a
# format invented next year is excluded without a code change.
#
# Exhibition minutes matter more than their small row count suggests:
# a star resting through an All-Star game at five minutes and zero
# points would drag down both his per-minute rate and his expected
# workload, which is exactly the pair of terms the projection turns on.
MIN_GAMES_FOR_REAL_TEAM = 20

# Box-score column -> the stat key the NBA scoring tables use.
STAT_COLUMNS = {
    "points": "pts",
    "rebounds": "reb",
    "assists": "ast",
    "steals": "stl",
    "blocks": "blk",
    "turnovers": "tov",
    "three_point_field_goals_made": "fg3m",
}

# ESPN mixes specific positions with generic ones. A player listed only
# as "G" is genuinely eligible at either guard spot, so the generic
# labels expand rather than being forced onto one arbitrary choice --
# the value of these is grouping players for the shrinkage prior, and a
# guard belongs in both guard buckets.
POSITION_EXPANSION = {
    "G": ["PG", "SG"],
    "F": ["SF", "PF"],
    "GF": ["SG", "SF"],
    "FG": ["SG", "SF"],
    "FC": ["PF", "C"],
    "CF": ["PF", "C"],
}


def _request(url: str, method: str = "GET"):
    return urllib.request.Request(
        url, headers={"User-Agent": "dfs-edge/1.0"}, method=method
    )


def season_is_available(season: int, timeout: int = 30) -> bool:
    try:
        with urllib.request.urlopen(_request(BASE_URL.format(season=season), "HEAD"), timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return False


def available_seasons(last: int | None = None, count: int = 3) -> list[int]:
    """The most recent `count` seasons hoopR has published."""

    if last is None:
        last = date.today().year + 1

    found: list[int] = []
    for season in range(last, FIRST_SEASON - 1, -1):
        if season_is_available(season):
            found.append(season)
        if len(found) >= count:
            break

    return sorted(found)


def _positions(raw: Any) -> list[str]:
    label = str(raw or "").upper().strip()
    if not label:
        return []
    return POSITION_EXPANSION.get(label, [label])


def fetch_season(season: int) -> list[dict[str, Any]]:
    """Download one season and return normalised game-log records."""

    url = BASE_URL.format(season=season)

    try:
        with urllib.request.urlopen(_request(url), timeout=120) as response:
            payload = response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as error:
        raise CollectorError(f"Could not download {url}: {error}") from error

    try:
        frame = pd.read_parquet(io.BytesIO(payload))
    except Exception as error:  # noqa: BLE001 - pyarrow raises several types
        raise CollectorError(
            f"Could not read the parquet file for {season}: {error}. "
            "Is pyarrow installed?"
        ) from error

    frame = _drop_exhibitions(frame)

    records: list[dict[str, Any]] = []

    for row in frame.to_dict("records"):
        record = _normalise_row(row)
        if record is not None:
            records.append(record)

    return records


def _drop_exhibitions(frame: pd.DataFrame) -> pd.DataFrame:
    """Remove All-Star and other exhibition rows.

    See MIN_GAMES_FOR_REAL_TEAM. Returns the frame unchanged if the
    expected columns are missing, so a schema change upstream degrades
    to collecting too much rather than silently collecting nothing.
    """

    if "team_abbreviation" not in frame.columns or "game_id" not in frame.columns:
        return frame

    games_per_team = frame.groupby("team_abbreviation")["game_id"].nunique()
    real_teams = set(games_per_team[games_per_team >= MIN_GAMES_FOR_REAL_TEAM].index)

    return frame[frame["team_abbreviation"].isin(real_teams)]


def _normalise_row(row: dict[str, Any]) -> dict[str, Any] | None:
    name = row.get("athlete_display_name")
    if not name or pd.isna(name):
        return None

    game_date = row.get("game_date")
    if game_date is None or pd.isna(game_date):
        return None

    positions = _positions(row.get("athlete_position_abbreviation"))
    if not positions:
        return None

    # A did-not-play is a real observation of zero availability, so it is
    # kept with no stats rather than discarded.
    played = not bool(row.get("did_not_play"))

    minutes = row.get("minutes")
    minutes = 0.0 if minutes is None or pd.isna(minutes) else float(minutes)
    if not played:
        minutes = 0.0

    stats: dict[str, float] = {}
    if played:
        for column, key in STAT_COLUMNS.items():
            value = row.get(column)
            if value is not None and not pd.isna(value) and float(value):
                stats[key] = round(float(value), 3)

    team = str(row.get("team_abbreviation") or "").upper() or None
    opponent = str(row.get("opponent_team_abbreviation") or "").upper() or None

    return {
        "player_id": make_player_id(str(name), "NBA"),
        "name": str(name),
        "sport": "NBA",
        "positions": positions,
        "team": team,
        "opponent": opponent,
        "home": str(row.get("home_away") or "").lower() == "home",
        "game_date": game_date.isoformat() if hasattr(game_date, "isoformat") else str(game_date),
        "opportunity": round(minutes, 2),
        "stats": stats,
    }


def load_nba_history(
    database,
    seasons: list[int] | None = None,
    count: int = 3,
    since: str | None = None,
) -> dict[str, Any]:
    """Fetch recent seasons and write them into `game_logs`.

    `since` collects only games on or after that date, which is what
    makes a nightly refresh cheap -- it reads the current season and
    writes the dozen or so games played since the last run.
    """

    if seasons is None:
        seasons = available_seasons(count=count)

    if not seasons:
        raise CollectorError(
            "hoopR published no seasons that could be reached. Check network "
            "access to raw.githubusercontent.com."
        )

    records = []
    for season in seasons:
        for record in fetch_season(season):
            if since and record["game_date"] < since:
                continue
            records.append(record)

    summary = write_records(database, records, "NBA")
    summary["sport"] = "NBA"
    summary["seasons"] = seasons
    return summary
