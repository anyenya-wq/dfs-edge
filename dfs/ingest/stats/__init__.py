"""Historical stat collectors, one per sport.

Every collector writes into the same `game_logs` table and the
projection engine reads only that table, so collectors are independent
of each other and of everything downstream. Adding a sport means adding
a module and an entry in `COLLECTORS`.
"""

from __future__ import annotations

from typing import Any, Callable

from dfs.ingest.stats.common import STALE_AFTER_HOURS, lookback_from
from dfs.ingest.stats.epl import load_epl_history
from dfs.ingest.stats.mlb import load_mlb_history
from dfs.ingest.stats.nba import load_nba_history
from dfs.ingest.stats.nfl import CollectorError, load_nfl_history
from dfs.ingest.stats.nhl import load_nhl_history

COLLECTORS: dict[str, Callable[..., dict[str, Any]]] = {
    "NFL": load_nfl_history,
    "NBA": load_nba_history,
    "MLB": load_mlb_history,
    "NHL": load_nhl_history,
    "EPL": load_epl_history,
}

# Sports whose collector is not written yet. Named explicitly so the app
# can say "not built yet" rather than failing with a KeyError, and so
# the gap is visible rather than implied. Empty now that all five are
# built; kept because the shape is what the app and tests read.
PLANNED: dict[str, str] = {}

# Collectors whose source does not carry every stat its sport's scoring
# tables pay for. The projections are still internally consistent, but
# they read low and the shortfall differs by position, so the caveat
# belongs in front of the user rather than in a docstring.
INCOMPLETE = {
    "EPL": (
        "The FPL feed carries ten of the stats the sites pay for and is "
        "missing eleven -- shots, shots on goal, chances created, "
        "crosses, accurate passes, interceptions, clearances, blocked shots, "
        "fouls drawn and conceded, and a goalkeeper's win. Projections "
        "therefore read low, and the shortfall differs by position and by "
        "site, so rankings within a position are usable and rankings across "
        "positions are not. It is also a Premier League feed: a Champions "
        "League or MLS slate scores by the same rules but loads with no "
        "history at all."
    ),
}


def has_collector(sport: str) -> bool:
    return sport.upper() in COLLECTORS


def load_history(sport: str, database, **kwargs: Any) -> dict[str, Any]:
    """Populate `game_logs` for one sport."""

    sport = sport.upper()

    if sport not in COLLECTORS:
        planned = PLANNED.get(sport, "no source identified")
        raise CollectorError(
            f"No stat collector for {sport} yet (planned source: {planned}). "
            f"Available: {', '.join(sorted(COLLECTORS))}."
        )

    summary = COLLECTORS[sport](database, **kwargs)
    database.record_collector_run(
        sport,
        summary.get("logs", 0),
        ",".join(str(season) for season in summary.get("seasons", [])),
    )
    return summary


def is_stale(database, sport: str, max_age_hours: float = STALE_AFTER_HOURS) -> bool:
    """Whether a sport's history is old enough to be worth refreshing.

    History that loads once and never again decays silently through a
    season: nothing errors, the projections simply keep leaning on games
    that stopped being recent while the newest weeks -- the ones that
    say most about a player's current role -- are missing entirely.
    """

    if not has_collector(sport):
        return False

    age = database.hours_since_collection(sport)
    if age is None:
        return True

    return age >= max_age_hours


def refresh_history(
    database,
    sport: str,
    max_age_hours: float = STALE_AFTER_HOURS,
    force: bool = False,
) -> dict[str, Any] | None:
    """Bring a sport's history up to date, cheaply.

    Returns None when nothing was due, so a caller can stay silent
    rather than reporting a refresh that did not happen.

    An update reads only the current season and only games at or after
    what is already stored, so an in-season refresh writes a handful of
    rows rather than re-downloading everything. A database with no logs
    at all takes the full path instead, since there is nothing to be
    incremental against.
    """

    if not has_collector(sport):
        return None

    if not force and not is_stale(database, sport, max_age_hours):
        return None

    latest = database.latest_game_date(sport)

    if latest is None:
        return load_history(sport, database)

    return load_history(
        sport, database, count=1, since=lookback_from(latest)
    )


def log_counts(database) -> dict[str, int]:
    """How many game logs are stored per sport."""

    rows = database.connection.execute(
        "SELECT sport, COUNT(*) AS n FROM game_logs GROUP BY sport"
    ).fetchall()
    return {row["sport"]: row["n"] for row in rows}


__all__ = [
    "COLLECTORS", "PLANNED", "STALE_AFTER_HOURS", "CollectorError",
    "has_collector", "is_stale", "load_history", "log_counts", "refresh_history",
]
