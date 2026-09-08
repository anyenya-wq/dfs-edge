"""Collect EPL match stats from the Fantasy Premier League data mirror.

The FPL API itself is the natural source -- official, free, no key --
but `vaastav/Fantasy-Premier-League` mirrors it to a git host as one CSV
per season of per-player, per-gameweek rows, which is both easier to
read and reachable from places the API is not. It carries the current
season within a day or so of each gameweek.

**Read this before trusting a soccer projection.** This collector has a
gap the other four do not, and it is not a bug that can be fixed here:
FPL reports what the fantasy game scores, and DraftKings scores things
FPL never records. Goals, assists, clean sheets, cards, own goals,
penalties, saves and tackles all come through. Shots on goal, chances
created and crosses do not exist in this feed at all.

Two consequences follow, and the second is the one that matters.

Projections will read low, because points the sites pay for are simply
absent from the history. That alone would be tolerable -- the model
learns its rates from the same incomplete stats it scores against, so it
stays internally consistent and the ordering of players survives.

What does not survive is comparing across positions. A forward's missing
shots are worth more than a midfielder's missing crosses, and a
defender loses least of all, so the shortfall is not a constant. Rankings
within a position are meaningful; rankings across positions are skewed
toward defenders, and lineups built on them will over-roster defence.

Combined with the soccer scoring tables being unverified to begin with,
soccer is the weakest of the five sports here by some distance. Treat
its output as a research aid rather than as projections.
"""

from __future__ import annotations

import csv
import io
import urllib.error
import urllib.request
from datetime import date
from typing import Any

from dfs.ingest.salaries import player_id as make_player_id
from dfs.ingest.stats.common import write_records
from dfs.ingest.stats.nfl import CollectorError

BASE_URL = (
    "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master"
    "/data/{season}/gws/merged_gw.csv"
)

TEAMS_URL = (
    "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master"
    "/data/{season}/teams.csv"
)

# The mirror starts here; earlier seasons are missing the per-gameweek
# detail this needs.
FIRST_SEASON_START = 2016

# FPL column -> the stat key the EPL scoring tables use. Only fields
# whose meaning is unambiguous are mapped. `clearances_blocks_interceptions`
# is deliberately left out: it conflates three actions the sites price
# differently, and mapping it to any one of them would invent points.
STAT_COLUMNS = {
    "goals_scored": "goal",
    "assists": "assist",
    "clean_sheets": "clean_sheet",
    "goals_conceded": "goal_allowed",
    "yellow_cards": "yellow_card",
    "red_cards": "red_card",
    "own_goals": "own_goal",
    "penalties_missed": "penalty_miss",
    "saves": "save",
    "penalties_saved": "penalty_save",
    "tackles": "tackle_won",
}

# Stats the sites pay for that this feed does not carry at all. Named
# here so the gap is visible in code rather than only in prose.
MISSING_STATS = ("shot_on_goal", "created_chance", "cross")

# FPL position -> what the salary files call it.
POSITIONS = {"GK": "GK", "DEF": "D", "MID": "M", "FWD": "F"}


def _open(url: str, timeout: int = 90) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "dfs-edge/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as error:
        raise CollectorError(f"Could not download {url}: {error}") from error


def season_label(start_year: int) -> str:
    """The mirror's folder name for a season, e.g. 2026 -> "2026-27"."""

    return f"{start_year}-{str(start_year + 1)[-2:]}"


def season_is_available(start_year: int, timeout: int = 30) -> bool:
    request = urllib.request.Request(
        BASE_URL.format(season=season_label(start_year)),
        headers={"User-Agent": "dfs-edge/1.0"},
        method="HEAD",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return False


def available_seasons(last: int | None = None, count: int = 3) -> list[int]:
    """The most recent `count` season start years the mirror carries."""

    if last is None:
        last = date.today().year

    found: list[int] = []
    for start_year in range(last, FIRST_SEASON_START - 1, -1):
        if season_is_available(start_year):
            found.append(start_year)
        if len(found) >= count:
            break

    return sorted(found)


def _as_float(value: Any) -> float:
    try:
        return float(value) if value not in (None, "", "NA") else 0.0
    except (TypeError, ValueError):
        return 0.0


def fetch_team_names(start_year: int) -> dict[int, str]:
    """Map the numeric opponent id in the match rows to a team name.

    The gameweek file names a player's own team but identifies the
    opponent only by id, so this is what makes the opponent column
    readable. A failure here is not fatal -- the opponent is
    informational, since eligibility and stacking both come from the
    salary file.
    """

    try:
        payload = _open(TEAMS_URL.format(season=season_label(start_year)), timeout=45)
    except CollectorError:
        return {}

    names: dict[int, str] = {}
    for row in csv.DictReader(io.StringIO(payload)):
        try:
            names[int(row["id"])] = row.get("name") or row.get("short_name") or ""
        except (KeyError, TypeError, ValueError):
            continue

    return names


def fetch_season(start_year: int) -> list[dict[str, Any]]:
    """Download one season and return normalised game-log records."""

    payload = _open(BASE_URL.format(season=season_label(start_year)))
    team_names = fetch_team_names(start_year)

    records: list[dict[str, Any]] = []
    for row in csv.DictReader(io.StringIO(payload)):
        record = _normalise_row(row, team_names)
        if record is not None:
            records.append(record)

    return records


def _normalise_row(row: dict[str, str], team_names: dict[int, str]) -> dict[str, Any] | None:
    name = (row.get("name") or "").strip()
    if not name:
        return None

    position = POSITIONS.get((row.get("position") or "").strip().upper())
    if position is None:
        return None

    kickoff = (row.get("kickoff_time") or "").strip()
    if not kickoff:
        return None
    # Trimmed to a plain date so it can match a slate date. The feed
    # writes a full UTC timestamp.
    game_date = kickoff[:10]

    stats: dict[str, float] = {}
    for column, key in STAT_COLUMNS.items():
        value = _as_float(row.get(column))
        if value:
            stats[key] = value

    # Clean sheets and goals conceded are defensive outcomes, credited
    # and charged only to goalkeepers and defenders. FPL records both
    # for every player on the pitch, so a striker's team conceding twice
    # would otherwise cost him two points he is never actually charged.
    if position not in {"GK", "D"}:
        stats.pop("clean_sheet", None)
        stats.pop("goal_allowed", None)

    opponent = None
    opponent_id = row.get("opponent_team")
    if opponent_id:
        try:
            opponent = team_names.get(int(float(opponent_id)))
        except (TypeError, ValueError):
            opponent = None

    return {
        "player_id": make_player_id(name, "EPL"),
        "name": name,
        "sport": "EPL",
        "positions": [position],
        "team": (row.get("team") or "").strip() or None,
        "opponent": opponent,
        "home": str(row.get("was_home") or "").strip().lower() in {"true", "1"},
        "game_date": game_date,
        "opportunity": _as_float(row.get("minutes")),
        "stats": {key: round(value, 3) for key, value in stats.items()},
    }


def load_epl_history(
    database,
    seasons: list[int] | None = None,
    count: int = 2,
    since: str | None = None,
) -> dict[str, Any]:
    """Fetch recent seasons and write them into `game_logs`."""

    if seasons is None:
        seasons = available_seasons(count=count)

    if not seasons:
        raise CollectorError(
            "The FPL mirror published no seasons that could be reached. Check "
            "network access to raw.githubusercontent.com."
        )

    records: list[dict[str, Any]] = []
    for start_year in seasons:
        for record in fetch_season(start_year):
            if since and record["game_date"] < since:
                continue
            records.append(record)

    summary = write_records(database, records, "EPL")
    summary["sport"] = "EPL"
    summary["seasons"] = seasons
    summary["missing_stats"] = list(MISSING_STATS)
    return summary
