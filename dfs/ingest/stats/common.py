"""Shared machinery for the per-sport collectors.

Kept out of the package `__init__` because that module imports the
collectors, and they need this -- putting it there would be circular.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

# How far before the newest stored game an incremental refresh starts.
# Not zero, for two reasons: box scores are corrected after the fact
# (a rebound reassigned, minutes adjusted), and a late-finishing game can
# be published after an earlier one on the same date has already been
# collected. Re-reading a few days costs almost nothing because the write
# is an upsert, while missing a correction is silent and permanent.
REFRESH_LOOKBACK_DAYS = 3

# How old a sport's history may be before it is refreshed on app start.
# Twelve hours means a slate opened in the evening picks up the previous
# night's games without re-downloading on every interaction.
STALE_AFTER_HOURS = 12.0


def lookback_from(latest: str | None) -> str | None:
    """The cutoff an incremental refresh should collect from."""

    if not latest:
        return None

    try:
        parsed = date.fromisoformat(latest[:10])
    except ValueError:
        return None

    return (parsed - timedelta(days=REFRESH_LOOKBACK_DAYS)).isoformat()


def write_records(database, records: Iterable[dict[str, Any]], sport: str) -> dict[str, Any]:
    """Persist collected records, players first for the foreign key.

    Batched rather than row-by-row. The per-row path commits every
    insert, which made loading three NBA seasons take ninety-five
    seconds of almost pure fsync; batching the same rows is over a
    hundred times faster and is what makes refreshing on app start
    reasonable.
    """

    records = list(records)

    players: dict[str, dict[str, Any]] = {}
    for record in records:
        players.setdefault(record["player_id"], {
            "player_id": record["player_id"],
            "name": record["name"],
            "sport": sport,
            "team": record.get("team"),
            "positions": record.get("positions", []),
        })

    database.upsert_players(players.values())
    written = database.save_game_logs(records)

    return {"logs": written, "players": len(players)}
