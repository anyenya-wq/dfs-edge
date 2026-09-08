"""Team defences, assembled from the same nflverse file as the players.

nflverse publishes no DST row. It publishes every defender's line, and a
defence is the sum of them -- so the unit is built here by aggregating a
team's players for a week rather than by finding a feed that already has
it, because no free one does.

Points allowed cannot come from that file at all: it is a property of the
game, not of any player in it, so the scoreboard is read separately and
joined on `game_id`.

Why this is worth building. A defence is a roster slot on both sites, and
without these rows every defence falls back to the site's own average
projection -- which is the number the whole field is looking at. A slot
where you have no opinion is a slot where you cannot be right.
"""

from __future__ import annotations

import csv
import io
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Any

from dfs.ingest.teams import defence_id, normalise_team

# One file, every season, with the final score of every game. nfldata is
# the same organisation as nflverse and is the source its schedule
# helpers read.
SCOREBOARD_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"

# nflverse column -> DST scoring key. Summed across every player on the
# team for that week.
DEFENCE_COLUMNS = {
    "def_interceptions": "def_int",
    # The opponent's fumble, not the team's own. Recovering your own
    # fumble is not a takeaway and neither site pays for it.
    "fumble_recovery_opp": "fumble_recovery",
    "def_safeties": "safety",
    "def_tds": "def_td",
    "special_teams_tds": "return_td",
}

# Blocks are one scoring line on both sites and three columns here.
BLOCK_COLUMNS = ("def_punt_blocks", "def_fg_blocks", "def_pat_blocks")

# Sacks are read from the offence that suffered them, not from the
# defenders credited with them. Checked across the 2024 season: the two
# counts agree on 560 of 570 team-games, and every one of the ten
# disagreements is a sack the offence recorded with no defender credited
# -- an attribution gap upstream, not a phantom sack. Interceptions,
# checked the same way, agree on all 570, so only this one column needs
# the indirection.
SACKS_SUFFERED_COLUMN = "sacks_suffered"


class ScoreboardError(RuntimeError):
    """Raised when the scoreboard cannot be retrieved."""


def _as_float(value: Any) -> float:
    try:
        return float(value) if value not in (None, "", "NA") else 0.0
    except (TypeError, ValueError):
        return 0.0


def fetch_scoreboard(timeout: int = 90) -> dict[str, dict[str, float]]:
    """Points allowed per team per game, keyed by `game_id`.

    Returns `{game_id: {team: points the other side scored}}`.
    """

    request = urllib.request.Request(
        SCOREBOARD_URL, headers={"User-Agent": "dfs-edge/1.0"}
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as error:
        raise ScoreboardError(
            f"Could not download the scoreboard from {SCOREBOARD_URL}: {error}"
        ) from error

    return parse_scoreboard(payload)


def parse_scoreboard(payload: str) -> dict[str, dict[str, float]]:
    """Points allowed per team per game, from the scoreboard CSV."""

    scores: dict[str, dict[str, float]] = {}

    for row in csv.DictReader(io.StringIO(payload)):
        game_id = (row.get("game_id") or "").strip()
        home = normalise_team(row.get("home_team"))
        away = normalise_team(row.get("away_team"))

        if not game_id or not home or not away:
            continue

        # A scheduled game with no score yet. Skipping it is the point:
        # a future fixture read as 0-0 would record two shutouts and pay
        # both defences ten points for a game nobody has played.
        if row.get("home_score") in (None, "", "NA"):
            continue
        if row.get("away_score") in (None, "", "NA"):
            continue

        scores[game_id] = {
            home: _as_float(row.get("away_score")),
            away: _as_float(row.get("home_score")),
        }

    return scores


def defence_records(
    rows,
    season: int,
    scoreboard: dict[str, dict[str, float]],
    week_date,
) -> list[dict[str, Any]]:
    """One `game_logs` record per team per week, from that team's players.

    `week_date` is passed in rather than imported so these rows carry
    exactly the dates the player rows carry. A defence dated differently
    from the offence it lines up against would be invisible to every
    query that joins them.
    """

    totals: dict[tuple[str, int], dict[str, Any]] = {}
    # Sacks suffered by each team in each game, so a defence can read
    # its own sack count off the offence it was facing.
    suffered: dict[tuple[str, str], float] = defaultdict(float)

    for row in rows:
        team = normalise_team(row.get("team"))
        if not team:
            continue

        try:
            week = int(float(row.get("week") or 0))
        except (TypeError, ValueError):
            continue
        if week <= 0:
            continue

        entry = totals.setdefault(
            (team, week),
            {
                "stats": defaultdict(float),
                "game_id": (row.get("game_id") or "").strip(),
                "opponent": normalise_team(row.get("opponent_team")),
            },
        )

        for column, key in DEFENCE_COLUMNS.items():
            entry["stats"][key] += _as_float(row.get(column))

        entry["stats"]["blocked_kick"] += sum(
            _as_float(row.get(column)) for column in BLOCK_COLUMNS
        )

        game_id = (row.get("game_id") or "").strip()
        if game_id:
            suffered[(game_id, team)] += _as_float(row.get(SACKS_SUFFERED_COLUMN))

        # Kept as the fallback for when the opposing team has no rows in
        # this file at all.
        entry["stats"].setdefault("sack", 0.0)
        entry["credited_sacks"] = entry.get("credited_sacks", 0.0) + _as_float(
            row.get("def_sacks")
        )

    records = []

    for (team, week), entry in sorted(totals.items()):
        allowed = scoreboard.get(entry["game_id"], {}).get(team)

        # Without a score there is no points-allowed band, and that band
        # is most of a defence's fantasy points -- a record without it
        # would read as a quiet twelve-point miss rather than as missing
        # data. Dropped rather than guessed.
        if allowed is None:
            continue

        stats = {key: value for key, value in entry["stats"].items()}
        stats["points_allowed"] = allowed

        opponent = entry["opponent"]
        facing = suffered.get((entry["game_id"], opponent)) if opponent else None
        stats["sack"] = entry["credited_sacks"] if facing is None else facing

        records.append({
            "player_id": defence_id(team),
            "name": f"{team} DST",
            "sport": "NFL",
            "positions": ["DST"],
            "team": team,
            "opponent": entry["opponent"],
            "game_date": week_date(season, week),
            # Every defence is on the field for its whole game, so there
            # is no playing time to model: the split the projection
            # engine makes between opportunity and rate collapses, and
            # one opportunity per game leaves the rate as points per
            # game, which is what a defence's history actually is.
            "opportunity": 1.0,
            "stats": stats,
            "season": season,
            "week": week,
        })

    return records
