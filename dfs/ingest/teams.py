"""Team codes, normalised to one spelling per franchise.

DraftKings, FanDuel and nflverse do not agree on how to abbreviate a
team. nflverse writes the Rams as `LA`, DraftKings as `LAR`; Jacksonville
is `JAX` in one place and `JAC` in another. That disagreement is
invisible until something joins on it -- and one thing does: a team
defence has no player name to match on, so its identity *is* its team
code.

Canonical form is nflverse's, because that is what the collected history
is keyed by. Relocations map to the current franchise (`STL` and `SD`
resolve to the teams those franchises became), since the history is
continuous even though the abbreviation is not.
"""

from __future__ import annotations

# Every alias that is not already canonical. Canonical codes are the
# nflverse set and map to themselves.
NFL_TEAM_ALIASES = {
    "ARZ": "ARI",
    "BLT": "BAL",
    "CLV": "CLE",
    "GNB": "GB",
    "HST": "HOU",
    "JAC": "JAX",
    "KAN": "KC",
    "LAR": "LA",
    "LVR": "LV",
    "NWE": "NE",
    "NOR": "NO",
    "SFO": "SF",
    "TAM": "TB",
    "WSH": "WAS",
    "WFT": "WAS",
    # Relocations. The franchise's history is continuous even where the
    # abbreviation is not, so an old code resolves to the current team.
    "OAK": "LV",
    "SD": "LAC",
    "SDG": "LAC",
    "STL": "LA",
    "LAA": "LA",
}

NFL_TEAMS = frozenset({
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
    "DET", "GB", "HOU", "IND", "JAX", "KC", "LA", "LAC", "LV", "MIA",
    "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB",
    "TEN", "WAS",
})


def normalise_team(code: str | None, sport: str = "NFL") -> str | None:
    """One spelling per franchise, or None if the code is unrecognised.

    Returns None rather than guessing. An unknown code means a source
    changed or a file is not what it claims, and inventing a team from
    it would produce a defence joined to the wrong history -- a silent
    wrong answer where None produces a visible gap.
    """

    if not code or sport.upper() != "NFL":
        return None

    code = str(code).strip().upper()
    code = NFL_TEAM_ALIASES.get(code, code)

    return code if code in NFL_TEAMS else None


# Position strings the two sites use for a team defence.
DEFENCE_POSITIONS = frozenset({"DST", "DEF", "D", "D/ST"})


def is_defence(positions) -> bool:
    return any(str(position).strip().upper() in DEFENCE_POSITIONS for position in positions or ())


def defence_id(team: str | None, sport: str = "NFL") -> str | None:
    """The identity of a team defence: its team, not its name.

    Names are useless here. DraftKings lists the unit as `Ravens`,
    FanDuel spells it differently again, and nflverse has no row for it
    at all -- so a name-based id would leave every defence unmatched and
    quietly falling back to the site's own average, which is the exact
    hole this closes.
    """

    team = normalise_team(team, sport)
    return f"{sport.lower()}:dst:{team.lower()}" if team else None
