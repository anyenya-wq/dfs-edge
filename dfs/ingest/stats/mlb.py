"""Collect MLB box scores from the official StatsAPI into `game_logs`.

Unlike football and basketball, baseball has no bulk file to download.
The sportsdataverse mirror stops at 2022 and Retrosheet publishes event
files long after a season ends, so neither can serve a tool used during
the season. MLB's own StatsAPI is the remaining option, and it is a good
one: free, no key, official, and current within minutes of a game
ending.

The cost is request count. There is no endpoint returning a season of
per-player game lines, so this walks dates, asks the schedule for that
day's games, and reads one box score per game -- roughly fifteen
requests a day plus one. That is why the default window is a number of
recent days rather than whole seasons, and why the incremental refresh
matters more here than anywhere else: after the first load, a nightly
update costs about sixteen requests.

Baseball is also the only sport where the two halves of a roster score
on entirely different tables. Hitters and pitchers are read from
separate sections of the same box score and stored with the position
that decides which scoring table applies later.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any

from dfs.ingest.salaries import player_id as make_player_id
from dfs.ingest.stats.common import write_records
from dfs.ingest.stats.nfl import CollectorError

SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate={start}&endDate={end}"
BOXSCORE_URL = "https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore"

# How much recent history to collect on a first run. Ninety days is most
# of a season's useful signal for a model weighting recent games
# heavily, and keeps the initial load to a few thousand requests rather
# than the tens of thousands a multi-season backfill would need.
DEFAULT_DAYS = 90

# Parallel box-score fetches. Deliberately modest: this is someone
# else's free API and the collector runs unattended, so it stays well
# inside anything that could be called hammering.
MAX_WORKERS = 6

# Batting box-score field -> the stat key the MLB scoring tables use.
# Singles are derived rather than read, because the feed reports total
# hits and the extra-base subtypes separately.
BATTING_COLUMNS = {
    "rbi": "rbi",
    "runs": "run",
    "doubles": "double",
    "triples": "triple",
    "homeRuns": "hr",
    "baseOnBalls": "bb",
    "hitByPitch": "hbp",
    "stolenBases": "sb",
}

PITCHING_COLUMNS = {
    "strikeOuts": "k",
    "earnedRuns": "er",
    "hits": "hit_allowed",
    "baseOnBalls": "bb_allowed",
    "hitBatsmen": "hbp_allowed",
}

PITCHER_POSITIONS = {"P", "SP", "RP"}


def _get(url: str, timeout: int = 45) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "dfs-edge/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as error:
        raise CollectorError(f"Could not reach {url}: {error}") from error
    except json.JSONDecodeError as error:
        raise CollectorError(f"Malformed response from {url}: {error}") from error


def innings_to_float(value: Any) -> float:
    """Convert innings pitched from baseball notation to a number.

    "6.1" means six innings and one out, not six and a tenth. Scoring
    pays per inning, so the thirds have to be real: 6.1 is 6.333, and
    treating it as 6.1 would quietly understate every start.
    """

    if value in (None, ""):
        return 0.0

    text = str(value)
    if "." not in text:
        try:
            return float(text)
        except ValueError:
            return 0.0

    whole, _, fraction = text.partition(".")

    try:
        innings = float(whole or 0)
    except ValueError:
        return 0.0

    outs = {"0": 0.0, "1": 1.0 / 3.0, "2": 2.0 / 3.0}.get(fraction[:1], 0.0)
    return round(innings + outs, 4)


def _as_float(value: Any) -> float:
    try:
        return float(value) if value not in (None, "", "-", ".---") else 0.0
    except (TypeError, ValueError):
        return 0.0


def game_dates(days: int = DEFAULT_DAYS, end: date | None = None, since: str | None = None) -> tuple[str, str]:
    """The date window to collect, as ISO strings.

    `since` wins when it is later than the plain window, which is what
    makes a refresh cheap -- it asks only for days not already stored.
    """

    end = end or date.today()
    start = end - timedelta(days=days)

    if since:
        try:
            since_date = date.fromisoformat(since[:10])
            start = max(start, since_date)
        except ValueError:
            pass

    return start.isoformat(), end.isoformat()


def fetch_schedule(start: str, end: str) -> list[dict[str, Any]]:
    """Every completed game in a date range, as (game_pk, date) pairs."""

    payload = _get(SCHEDULE_URL.format(start=start, end=end))

    games: list[dict[str, Any]] = []
    for day in payload.get("dates", []):
        game_date = day.get("date")
        for game in day.get("games", []):
            state = (game.get("status") or {}).get("abstractGameState")
            # Only finished games have complete box scores. In-progress
            # ones would be collected as though the player's night were
            # over, permanently understating that game.
            if state != "Final":
                continue
            if game.get("gamePk") and game_date:
                games.append({"game_pk": game["gamePk"], "game_date": game_date})

    return games


def _side_records(side: dict[str, Any], opponent: dict[str, Any], game_date: str, home: bool) -> list[dict[str, Any]]:
    team = ((side.get("team") or {}).get("abbreviation") or "").upper() or None
    opponent_team = ((opponent.get("team") or {}).get("abbreviation") or "").upper() or None

    records: list[dict[str, Any]] = []

    for entry in (side.get("players") or {}).values():
        person = entry.get("person") or {}
        name = person.get("fullName")
        if not name:
            continue

        position = ((entry.get("position") or {}).get("abbreviation") or "").upper()
        stats = entry.get("stats") or {}
        batting = stats.get("batting") or {}
        pitching = stats.get("pitching") or {}

        record = _record_for(
            name, position, batting, pitching, team, opponent_team, game_date, home, entry
        )
        if record is not None:
            records.append(record)

    return records


def _record_for(
    name: str,
    position: str,
    batting: dict[str, Any],
    pitching: dict[str, Any],
    team: str | None,
    opponent: str | None,
    game_date: str,
    home: bool,
    entry: dict[str, Any],
) -> dict[str, Any] | None:
    """One game log, scored on whichever table the player belongs to."""

    is_pitcher = position in PITCHER_POSITIONS

    if is_pitcher and pitching:
        innings = innings_to_float(pitching.get("inningsPitched"))
        stats: dict[str, float] = {"ip": innings} if innings else {}

        for field, key in PITCHING_COLUMNS.items():
            value = _as_float(pitching.get(field))
            if value:
                stats[key] = value

        if _decision_is_win(entry, pitching):
            stats["win"] = 1.0

        # A complete game is nine innings by one pitcher; a shutout adds
        # no earned runs. Both are paid on top of the per-inning rate.
        if _as_float(pitching.get("completeGames")):
            stats["complete_game"] = 1.0
            if _as_float(pitching.get("shutouts")):
                stats["complete_game_shutout"] = 1.0

        if not stats:
            return None

        opportunity = innings

    else:
        plate_appearances = _as_float(batting.get("plateAppearances"))
        hits = _as_float(batting.get("hits"))
        doubles = _as_float(batting.get("doubles"))
        triples = _as_float(batting.get("triples"))
        home_runs = _as_float(batting.get("homeRuns"))

        stats = {}
        for field, key in BATTING_COLUMNS.items():
            value = _as_float(batting.get(field))
            if value:
                stats[key] = value

        # Singles are what is left of the hits once the extra-base ones
        # are removed; the feed never reports them directly.
        singles = hits - doubles - triples - home_runs
        if singles > 0:
            stats["single"] = singles

        # A position player who did not come to the plate has no line.
        # Recorded as a zero-opportunity appearance rather than dropped,
        # so the projection reads it as availability information.
        if not plate_appearances and not stats:
            if not batting:
                return None

        opportunity = plate_appearances

    return {
        "player_id": make_player_id(name, "MLB"),
        "name": name,
        "sport": "MLB",
        "positions": [_normalise_position(position, is_pitcher)],
        "team": team,
        "opponent": opponent,
        "home": home,
        "game_date": game_date,
        "opportunity": round(opportunity, 3),
        "stats": {key: round(value, 3) for key, value in stats.items()},
    }


def _normalise_position(position: str, is_pitcher: bool) -> str:
    """Map a fielding position onto what the salary files call it.

    The feed reports the position played, which includes designated
    hitter, pinch hitter, and left/centre/right field. Both sites list
    all three outfield spots as OF, and treat a designated hitter as
    whatever else he qualifies at, so those collapse here. Getting this
    wrong would only skew the shrinkage prior -- eligibility comes from
    the salary file -- but a prior split across LF, CF and RF is three
    thin buckets instead of one useful one.
    """

    if is_pitcher:
        return "P"
    if position in {"LF", "CF", "RF", "OF"}:
        return "OF"
    if position in {"DH", "PH", "PR"}:
        return "UTIL"
    return position or "UTIL"


def _decision_is_win(entry: dict[str, Any], pitching: dict[str, Any]) -> bool:
    """Whether this pitcher was credited with the win.

    The box score records the decision two different ways depending on
    the endpoint: a `wins` counter, or a note reading "(W, 12-4)". Both
    are checked, because relying on one silently loses four points a
    start whenever the feed uses the other.
    """

    if _as_float(pitching.get("wins")):
        return True

    note = str(entry.get("gameStatus", {}).get("note") or entry.get("note") or "")
    return note.strip().startswith("(W")


def fetch_boxscore(game: dict[str, Any]) -> list[dict[str, Any]]:
    """Every player's line from one game."""

    payload = _get(BOXSCORE_URL.format(game_pk=game["game_pk"]))
    teams = payload.get("teams") or {}
    away, home = teams.get("away") or {}, teams.get("home") or {}

    if not away or not home:
        return []

    return (
        _side_records(away, home, game["game_date"], home=False)
        + _side_records(home, away, game["game_date"], home=True)
    )


def load_mlb_history(
    database,
    days: int = DEFAULT_DAYS,
    since: str | None = None,
    seasons: list[int] | None = None,
    count: int | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    """Collect recent MLB box scores into `game_logs`.

    `seasons` and `count` are accepted for signature compatibility with
    the other collectors and mapped onto the day window, since StatsAPI
    is queried by date rather than by season.
    """

    if count and not since:
        # Roughly a season per unit, matching how the other collectors
        # read `count`, without pretending this fetches whole seasons.
        days = max(days, count * 180)

    start, end = game_dates(days=days, end=end, since=since)
    games = fetch_schedule(start, end)

    if not games:
        return {"sport": "MLB", "seasons": [], "logs": 0, "players": 0, "games": 0}

    records: list[dict[str, Any]] = []
    failures = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for result in pool.map(_safe_boxscore, games):
            if result is None:
                failures += 1
            else:
                records.extend(result)

    # One bad game is tolerable; a wholly failed run is not, and would
    # otherwise look identical to a quiet day with no baseball.
    if failures and not records:
        raise CollectorError(
            f"All {failures} box-score requests failed. Check network access to "
            "statsapi.mlb.com."
        )

    summary = write_records(database, records, "MLB")
    summary["sport"] = "MLB"
    summary["seasons"] = sorted({int(game["game_date"][:4]) for game in games})
    summary["games"] = len(games) - failures
    summary["failed_games"] = failures
    return summary


def _safe_boxscore(game: dict[str, Any]) -> list[dict[str, Any]] | None:
    try:
        return fetch_boxscore(game)
    except CollectorError:
        return None
