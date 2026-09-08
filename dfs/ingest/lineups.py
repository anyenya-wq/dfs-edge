"""Confirmed lineups and announced starters, from MLB's own API.

The salary export already carries this, but only as a snapshot: it is
true at the moment it was exported and goes stale as the afternoon's
lineups post. Reading the same facts from the source means a slate can
be brought up to date without downloading the file again.

statsapi.mlb.com is the obvious source and the only one used. It is
official, free, needs no key, and the stat collector already reads it.
The alternative -- scraping one of the lineup aggregators -- is
prohibited by most of their terms and fails silently when a page
changes, which hands you stale lineups with no error at all. Stale
lineups are worse than none, because you act on them.

Parsed defensively. This module was written without being able to reach
the API from the machine it was written on, so every field is treated
as optional and a shape that is not understood yields nothing rather
than something wrong. The live test in the suite is what proves it
against the real thing.
"""

from __future__ import annotations

import dataclasses
import json
import urllib.error
import urllib.request
from typing import Any

from dfs.ingest.salaries import player_id as make_player_id

SCHEDULE_URL = (
    "https://statsapi.mlb.com/api/v1/schedule"
    "?sportId=1&startDate={date}&endDate={date}&hydrate=probablePitcher,lineups,team"
)

# The code written against an announced starting pitcher, matching what
# the salary export writes so the two sources agree on vocabulary.
STARTING_PITCHER = "SP"


class LineupError(RuntimeError):
    """Raised when the schedule cannot be retrieved."""


@dataclasses.dataclass(frozen=True)
class Availability:
    """What the source says about one player's part in today's game."""

    player_id: str
    name: str
    team: str | None
    starting: str
    batting_order: int | None = None


def _get(url: str, timeout: int = 45) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "dfs-edge/1.0"})

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as error:
        raise LineupError(f"Could not read {url}: {error}") from error
    except json.JSONDecodeError as error:
        raise LineupError(f"{url} did not return JSON: {error}") from error


def _team_code(side: dict[str, Any]) -> str | None:
    """A team's abbreviation, wherever this response happens to put it."""

    team = side.get("team") or {}
    for key in ("abbreviation", "teamCode", "fileCode"):
        value = team.get(key)
        if value:
            return str(value).upper()
    return None


def _player_name(player: Any) -> str | None:
    if isinstance(player, dict):
        for key in ("fullName", "name", "boxscoreName"):
            value = player.get(key)
            if value:
                return str(value)
    elif isinstance(player, str):
        return player
    return None


def parse_schedule(payload: dict[str, Any]) -> list[Availability]:
    """Every announced pitcher and posted batting order in one day.

    A game whose lineup has not posted contributes its probable pitcher
    and nothing else, which is exactly the partial state a slate is in
    for most of the afternoon.
    """

    found: list[Availability] = []

    for date_entry in payload.get("dates") or []:
        for game in date_entry.get("games") or []:
            teams = game.get("teams") or {}
            lineups = game.get("lineups") or {}

            for side, players_key in (("away", "awayPlayers"), ("home", "homePlayers")):
                entry = teams.get(side) or {}
                team = _team_code(entry)

                pitcher = _player_name(entry.get("probablePitcher"))
                if pitcher:
                    found.append(Availability(
                        player_id=make_player_id(pitcher, "MLB"),
                        name=pitcher,
                        team=team,
                        starting=STARTING_PITCHER,
                    ))

                for index, player in enumerate(lineups.get(players_key) or [], start=1):
                    name = _player_name(player)
                    # Only the nine that bat. Anything beyond is not a
                    # batting order and is likelier a shape this parser
                    # does not understand than a tenth hitter.
                    if not name or index > 9:
                        continue
                    found.append(Availability(
                        player_id=make_player_id(name, "MLB"),
                        name=name,
                        team=team,
                        starting=str(index),
                        batting_order=index,
                    ))

    return found


def fetch_mlb_lineups(slate_date: str) -> list[Availability]:
    """Announced starters and posted lineups for one date."""

    return parse_schedule(_get(SCHEDULE_URL.format(date=slate_date)))
