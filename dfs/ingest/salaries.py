"""Parse DraftKings and FanDuel salary exports into one player pool.

Use the CSV export from the contest entry screen rather than scraping
either site. Both sites publish a download button on the draft page; the
undocumented JSON endpoints people pass around are against the terms of
use on a strict reading and change shape without notice, and a broken
scraper at 12:55 on a 1:00 lock is the worst possible time to discover
it. The CSV is stable and it is the same data.

The two formats disagree on almost every column name, so both are
normalised here into one record shape and every consumer downstream sees
only that shape.

The one field worth understanding is `roster_positions`. DraftKings
publishes the full list of slots a player may fill ("PG/SG/G/UTIL"),
which is exactly what the optimizer's eligibility constraint needs.
FanDuel publishes it inconsistently, so it is reconstructed from the
listed position when absent. Getting this wrong silently shrinks the
search space -- a player who can fill UTIL but is not marked as such
simply never appears there -- so the reconstruction is explicit rather
than a fallback buried in the optimizer.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections.abc import Iterable, Iterator
from typing import Any

from dfs.sports import SportConfig

# Suffixes that appear inconsistently between a salary file and a stats
# feed for the same person. Stripped from the id so the two join.
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def slugify_name(name: str) -> str:
    """A join key for a player name that survives feed disagreements.

    Accents, punctuation, and generational suffixes all vary between
    sources for the same person -- "Luka Doncic" and "Luka Dončić",
    "Michael Pittman Jr." and "Michael Pittman". Normalising them away
    is what lets a DraftKings pool join to a stats feed that has never
    heard of DraftKings ids.
    """

    decomposed = unicodedata.normalize("NFKD", str(name))
    ascii_name = decomposed.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", ascii_name).lower()
    parts = [part for part in cleaned.split() if part and part not in _SUFFIXES]
    return "-".join(parts)


def player_id(name: str, sport: str) -> str:
    return f"{sport.lower()}:{slugify_name(name)}"


def _split_positions(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip().upper() for part in re.split(r"[/,]", str(raw)) if part.strip()]


def _parse_game_info(raw: str | None, team: str | None) -> dict[str, Any]:
    """Pull the matchup out of a site's game string.

    Both sites write the away team first ("DAL@PHX"), which is the only
    place a salary file records who is at home. Home/away matters to a
    projection and is not recoverable from anything else in the file.
    """

    result: dict[str, Any] = {"game_id": None, "opponent": None, "home": None}
    if not raw:
        return result

    match = re.search(r"([A-Za-z]{2,4})\s*@\s*([A-Za-z]{2,4})", str(raw))
    if not match:
        return result

    away, home = match.group(1).upper(), match.group(2).upper()
    result["game_id"] = f"{away}@{home}"

    if team:
        team = team.upper()
        if team == home:
            result["opponent"], result["home"] = away, True
        elif team == away:
            result["opponent"], result["home"] = home, False

    return result


def _rows(source: str | Iterable[str]) -> Iterator[dict[str, str]]:
    """Read CSV from a path or from raw text.

    Text is accepted so the Streamlit uploader can hand over an
    in-memory file without touching disk.
    """

    if isinstance(source, str) and "\n" in source:
        yield from csv.DictReader(io.StringIO(source))
        return

    if isinstance(source, str):
        with open(source, newline="", encoding="utf-8-sig") as handle:
            yield from csv.DictReader(handle)
        return

    yield from csv.DictReader(source)


def _clean_headers(row: dict[str, str]) -> dict[str, str]:
    return {(key or "").strip(): (value or "").strip() for key, value in row.items()}


def parse_draftkings(source: str | Iterable[str], sport: str) -> list[dict[str, Any]]:
    """Parse a DraftKings salary export.

    Columns: Position, Name + ID, Name, ID, Roster Position, Salary,
    Game Info, TeamAbbrev, AvgPointsPerGame.
    """

    pool: list[dict[str, Any]] = []

    for raw_row in _rows(source):
        row = _clean_headers(raw_row)
        name = row.get("Name") or row.get("Nickname")
        salary = row.get("Salary")
        if not name or not salary:
            continue

        team = (row.get("TeamAbbrev") or row.get("Team") or "").upper() or None
        game = _parse_game_info(row.get("Game Info") or row.get("Game"), team)
        positions = _split_positions(row.get("Position"))
        roster_positions = _split_positions(row.get("Roster Position")) or positions

        pool.append(
            {
                "player_id": player_id(name, sport),
                "name": name,
                "sport": sport.upper(),
                "team": team,
                "positions": positions,
                "roster_positions": roster_positions,
                "salary": int(float(salary)),
                "dk_id": row.get("ID") or None,
                "site_avg_points": _as_float(row.get("AvgPointsPerGame")),
                "injury_status": None,
                **game,
            }
        )

    return pool


def parse_fanduel(source: str | Iterable[str], sport: str) -> list[dict[str, Any]]:
    """Parse a FanDuel salary export.

    Columns: Id, Position, First Name, Nickname, Last Name, FPPG,
    Played, Salary, Game, Team, Opponent, Injury Indicator, Injury
    Details, Tier, Roster Position.

    FanDuel gives the opponent in its own column, so the matchup string
    is only needed to work out who is at home.
    """

    pool: list[dict[str, Any]] = []

    for raw_row in _rows(source):
        row = _clean_headers(raw_row)

        name = row.get("Nickname") or " ".join(
            part for part in (row.get("First Name"), row.get("Last Name")) if part
        )
        salary = row.get("Salary")
        if not name or not salary:
            continue

        team = (row.get("Team") or "").upper() or None
        game = _parse_game_info(row.get("Game"), team)
        positions = _split_positions(row.get("Position"))
        roster_positions = _split_positions(row.get("Roster Position")) or positions

        opponent = (row.get("Opponent") or "").upper() or game.get("opponent")

        pool.append(
            {
                "player_id": player_id(name, sport),
                "name": name,
                "sport": sport.upper(),
                "team": team,
                "positions": positions,
                "roster_positions": roster_positions,
                "salary": int(float(salary)),
                "fd_id": row.get("Id") or None,
                "site_avg_points": _as_float(row.get("FPPG")),
                "injury_status": (row.get("Injury Indicator") or "").upper() or None,
                "game_id": game.get("game_id"),
                "opponent": opponent,
                "home": game.get("home"),
            }
        )

    return pool


def _as_float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_salaries(source: str | Iterable[str], sport: str, site: str) -> list[dict[str, Any]]:
    """Dispatch to the right parser for a site."""

    site = site.upper()
    if site == "DK":
        return parse_draftkings(source, sport)
    if site == "FD":
        return parse_fanduel(source, sport)
    raise ValueError(f"Unknown site {site!r}; expected DK or FD")


def expand_roster_eligibility(pool: list[dict[str, Any]], config: SportConfig) -> list[dict[str, Any]]:
    """Fill in roster eligibility the site did not publish.

    A player listed only as "PG" is eligible for the PG slot, and also
    for any G or UTIL slot whose eligible set contains PG. Leaving that
    implicit costs real lineups, because the optimizer would never
    consider putting them there.
    """

    for player in pool:
        listed = set(player.get("roster_positions") or player.get("positions") or [])
        base = set(player.get("positions") or [])

        for slot in config.roster:
            if slot.accepts(sorted(base)):
                listed.add(slot.name)

        player["roster_positions"] = sorted(listed)

    return pool
