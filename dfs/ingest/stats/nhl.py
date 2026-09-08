"""Collect NHL box scores from the official NHL API into `game_logs`.

Hockey has the same problem baseball does and the same answer. The
sportsdataverse mirror publishes NHL box scores in a readable form but
stops at the 2023-24 season, which is useless for projecting current
rosters -- two years of trades, call-ups and line changes are exactly
what a projection needs to know. The NHL's own API is current, free and
needs no key, so it is what this reads.

The API is queried a week at a time for the schedule and once per game
for the box score, which is fewer requests than baseball needs because
the schedule endpoint returns a full week rather than a day.

Hockey's own wrinkle is that skaters and goalies are not merely scored
on different tables, they are reported in different sections with
different field names, and a goalie's saves are not a number at all --
the feed writes shots faced as "28/30" and expects the reader to do the
arithmetic. Getting that wrong would silently zero out most of a
goalie's score, since saves are where nearly all of it comes from.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any

from dfs.ingest.salaries import player_id as make_player_id
from dfs.ingest.stats.common import write_records
from dfs.ingest.stats.nfl import CollectorError

SCHEDULE_URL = "https://api-web.nhle.com/v1/schedule/{date}"
BOXSCORE_URL = "https://api-web.nhle.com/v1/gamecenter/{game_id}/boxscore"

# A first run's window. Ninety days is most of what a model weighting
# recent games heavily can use, and hockey's season runs October to
# June so this covers a meaningful stretch of it.
DEFAULT_DAYS = 90

MAX_WORKERS = 6

# Skater box-score field -> the stat key the NHL scoring tables use.
SKATER_COLUMNS = {
    "goals": "goal",
    "assists": "assist",
    "sog": "sog",
    "blockedShots": "blocked_shot",
}

# Game states that mean the box score is complete. Anything else is in
# progress or not started, and collecting it would record a player's
# night as finished when it is not.
FINAL_STATES = {"FINAL", "OFF"}


def _get(url: str, timeout: int = 45) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "dfs-edge/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as error:
        raise CollectorError(f"Could not reach {url}: {error}") from error
    except json.JSONDecodeError as error:
        raise CollectorError(f"Malformed response from {url}: {error}") from error


def time_on_ice_to_minutes(value: Any) -> float:
    """Convert time on ice from "mm:ss" to minutes.

    The opportunity term for hockey. A defenceman's twenty-four minutes
    and a fourth-liner's eight are the difference between the two, and
    reading "18:24" as a number would give 18.24 rather than 18.4 -- a
    small error, but one applied to every single log.
    """

    if value in (None, ""):
        return 0.0

    text = str(value).strip()

    if ":" not in text:
        try:
            return float(text)
        except ValueError:
            return 0.0

    minutes, _, seconds = text.partition(":")

    try:
        return round(float(minutes or 0) + float(seconds or 0) / 60.0, 3)
    except ValueError:
        return 0.0


def saves_from_shots_against(value: Any) -> tuple[float, float]:
    """Read a goalie's "28/30" into (saves, shots against).

    The feed reports the pair as a single string. Saves carry nearly a
    goalie's whole score on both sites, so a reader that fails here does
    not produce a slightly wrong number, it produces almost none.
    """

    if value in (None, ""):
        return 0.0, 0.0

    text = str(value).strip()

    if "/" not in text:
        try:
            return float(text), 0.0
        except ValueError:
            return 0.0, 0.0

    saves, _, shots = text.partition("/")

    try:
        return float(saves or 0), float(shots or 0)
    except ValueError:
        return 0.0, 0.0


def _as_float(value: Any) -> float:
    try:
        return float(value) if value not in (None, "", "-") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _player_name(entry: dict[str, Any]) -> str:
    """Pull a display name out of the API's localised name object."""

    name = entry.get("name")

    if isinstance(name, dict):
        return str(name.get("default") or "").strip()

    if isinstance(name, str):
        return name.strip()

    first = entry.get("firstName")
    last = entry.get("lastName")
    parts = [
        part.get("default") if isinstance(part, dict) else part
        for part in (first, last)
        if part
    ]
    return " ".join(str(part) for part in parts if part).strip()


def game_dates(days: int = DEFAULT_DAYS, end: date | None = None, since: str | None = None) -> tuple[date, date]:
    """The date window to collect."""

    end = end or date.today()
    start = end - timedelta(days=days)

    if since:
        try:
            start = max(start, date.fromisoformat(since[:10]))
        except ValueError:
            pass

    return start, end


def fetch_schedule(start: date, end: date) -> list[dict[str, Any]]:
    """Every finished game in a window.

    The schedule endpoint returns a week per request, so this steps in
    seven day strides rather than daily.
    """

    games: dict[int, dict[str, Any]] = {}
    cursor = start

    while cursor <= end:
        payload = _get(SCHEDULE_URL.format(date=cursor.isoformat()))

        for week_day in payload.get("gameWeek", []):
            game_date = week_day.get("date")
            if not game_date:
                continue
            if not (start.isoformat() <= game_date <= end.isoformat()):
                continue

            for game in week_day.get("games", []):
                state = str(game.get("gameState") or "").upper()
                if state not in FINAL_STATES:
                    continue
                game_id = game.get("id")
                if game_id:
                    games[int(game_id)] = {"game_id": int(game_id), "game_date": game_date}

        cursor += timedelta(days=7)

    return sorted(games.values(), key=lambda game: game["game_date"])


def _skater_record(entry: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
    name = _player_name(entry)
    if not name:
        return None

    stats: dict[str, float] = {}
    for field, key in SKATER_COLUMNS.items():
        value = _as_float(entry.get(field))
        if value:
            stats[key] = value

    # Short-handed points are a bonus on both sites but are not reported
    # as their own field, so they are taken when present and skipped
    # otherwise rather than guessed at from goals and assists.
    short_handed = _as_float(entry.get("shPoints")) or _as_float(entry.get("shorthandedGoals"))
    if short_handed:
        stats["short_handed_point"] = short_handed

    position = str(entry.get("position") or "").upper()

    return {
        "player_id": make_player_id(name, "NHL"),
        "name": name,
        "sport": "NHL",
        "positions": [_normalise_position(position)],
        "team": context["team"],
        "opponent": context["opponent"],
        "home": context["home"],
        "game_date": context["game_date"],
        "opportunity": time_on_ice_to_minutes(entry.get("toi")),
        "stats": {key: round(value, 3) for key, value in stats.items()},
    }


def _goalie_record(entry: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
    name = _player_name(entry)
    if not name:
        return None

    saves, _shots = saves_from_shots_against(entry.get("saveShotsAgainst"))
    goals_against = _as_float(entry.get("goalsAgainst"))
    decision = str(entry.get("decision") or "").upper()
    minutes = time_on_ice_to_minutes(entry.get("toi"))

    stats: dict[str, float] = {}
    if saves:
        stats["save"] = saves
    if goals_against:
        stats["goal_against"] = goals_against

    if decision == "W":
        stats["win"] = 1.0
        # A shutout is a win with nothing conceded. Backups who never
        # left the bench are excluded by the minutes check, so an unused
        # goalie cannot be credited with one.
        if goals_against == 0 and minutes > 0:
            stats["shutout"] = 1.0
    elif decision == "O":
        # An overtime loss still pays on DraftKings; a regulation loss
        # does not, which is why only this one is recorded.
        stats["ot_loss"] = 1.0

    # A goalie who dressed but did not play has no line. Kept as a zero
    # so the projection reads it as availability rather than absence.
    if not stats and minutes == 0:
        return None

    return {
        "player_id": make_player_id(name, "NHL"),
        "name": name,
        "sport": "NHL",
        "positions": ["G"],
        "team": context["team"],
        "opponent": context["opponent"],
        "home": context["home"],
        "game_date": context["game_date"],
        "opportunity": minutes,
        "stats": {key: round(value, 3) for key, value in stats.items()},
    }


def _normalise_position(position: str) -> str:
    """Map the reported position onto what the salary files call it.

    The feed distinguishes left and right wing; both sites list them as
    W, and a prior split across LW and RW is two thin buckets where one
    useful one belongs.
    """

    if position in {"L", "R", "LW", "RW", "W"}:
        return "W"
    if position in {"C", "D", "G"}:
        return position
    return position or "W"


def _side_records(side: dict[str, Any], context: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    for group in ("forwards", "defense", "defensemen"):
        for entry in side.get(group) or []:
            record = _skater_record(entry, context)
            if record is not None:
                records.append(record)

    for entry in side.get("goalies") or []:
        record = _goalie_record(entry, context)
        if record is not None:
            records.append(record)

    return records


def fetch_boxscore(game: dict[str, Any]) -> list[dict[str, Any]]:
    """Every player's line from one game."""

    payload = _get(BOXSCORE_URL.format(game_id=game["game_id"]))

    stats = payload.get("playerByGameStats") or {}
    away_side = stats.get("awayTeam") or {}
    home_side = stats.get("homeTeam") or {}

    if not away_side and not home_side:
        return []

    away_team = _team_abbrev(payload.get("awayTeam"))
    home_team = _team_abbrev(payload.get("homeTeam"))

    # Trimmed to a plain date. The field has been seen as both
    # "2026-01-15" and a full timestamp, and a stored timestamp would
    # never match a slate date, so every log for that game would resolve
    # to nothing -- silently, since a missing log reads as "did not
    # play" rather than as an error.
    game_date = str(payload.get("gameDate") or game["game_date"])[:10]

    return (
        _side_records(away_side, {
            "team": away_team, "opponent": home_team,
            "home": False, "game_date": game_date,
        })
        + _side_records(home_side, {
            "team": home_team, "opponent": away_team,
            "home": True, "game_date": game_date,
        })
    )


def _team_abbrev(team: Any) -> str | None:
    if not isinstance(team, dict):
        return None

    value = team.get("abbrev") or team.get("triCode")
    if isinstance(value, dict):
        value = value.get("default")

    return str(value).upper() if value else None


def load_nhl_history(
    database,
    days: int = DEFAULT_DAYS,
    since: str | None = None,
    seasons: list[int] | None = None,
    count: int | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    """Collect recent NHL box scores into `game_logs`.

    `seasons` and `count` are accepted for signature compatibility with
    the other collectors and mapped onto the day window, since the NHL
    API is queried by date rather than by season.
    """

    if count and not since:
        days = max(days, count * 250)

    start, end = game_dates(days=days, end=end, since=since)
    games = fetch_schedule(start, end)

    if not games:
        return {"sport": "NHL", "seasons": [], "logs": 0, "players": 0, "games": 0}

    records: list[dict[str, Any]] = []
    failures = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for result in pool.map(_safe_boxscore, games):
            if result is None:
                failures += 1
            else:
                records.extend(result)

    if failures and not records:
        raise CollectorError(
            f"All {failures} box-score requests failed. Check network access to "
            "api-web.nhle.com."
        )

    summary = write_records(database, records, "NHL")
    summary["sport"] = "NHL"
    summary["seasons"] = sorted({int(game["game_date"][:4]) for game in games})
    summary["games"] = len(games) - failures
    summary["failed_games"] = failures
    return summary


def _safe_boxscore(game: dict[str, Any]) -> list[dict[str, Any]] | None:
    try:
        return fetch_boxscore(game)
    except CollectorError:
        return None
