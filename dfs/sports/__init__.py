"""Registry of sport-site rule configurations.

Look a config up with `get_config("NBA", "DK")`. Everything downstream
-- ingest, projections, optimizer, scoring -- takes a `SportConfig`
rather than a sport name, so adding a sport means adding a module here
and nothing else.
"""

from __future__ import annotations

from dfs.sports.base import Bonus, RosterSlot, SportConfig, StackRule
from dfs.sports.bonuses import apply_combination_bonuses
from dfs.sports.epl import DK_EPL, FD_EPL
from dfs.sports.mlb import DK_MLB, FD_MLB
from dfs.sports.nba import DK_NBA, FD_NBA
from dfs.sports.nfl import DK_NFL, FD_NFL
from dfs.sports.nhl import DK_NHL, FD_NHL

CONFIGS: dict[str, SportConfig] = {
    config.key: config
    for config in (
        DK_NFL, FD_NFL,
        DK_NBA, FD_NBA,
        DK_MLB, FD_MLB,
        DK_NHL, FD_NHL,
        DK_EPL, FD_EPL,
    )
}

SPORTS = ("NFL", "NBA", "MLB", "NHL", "EPL")
SITES = ("DK", "FD")

# What still needs a human to check it against the site's live rules
# page. Surfaced in the Streamlit board so an unverified table cannot
# quietly become the basis for a real entry. Remove an entry once you
# have confirmed it and set `rules_verified=True` on the config.
VERIFICATION_NOTES: dict[str, str] = {
    "NFL:DK": ("Read from DraftKings' published NFL Classic rules: the "
               "scoring table, the three yardage bonuses, the defence table "
               "and its points-allowed bands, the $50,000 cap, the nine-man "
               "roster and the two-game minimum. Touchdowns scored by the "
               "opposing defence do not count against your own -- that is "
               "handled in the collector, not here."),
    "NFL:FD": ("Scoring read from FanDuel's own Rules & Scoring tab, "
               "including the three yardage bonuses, and now matches it line "
               "for line. The tab covers scoring only, so the $60,000 cap and "
               "the four-per-team limit are still assumed."),
    "NBA:DK": "Confirm double-double 1.5 / triple-double 3.0 and the 2.0 steal-block rate.",
    "NBA:FD": "Confirm the 3.0 steal-block rate and that no double bonus is paid.",
    "MLB:DK": ("Read from DraftKings' published MLB Classic rules: both "
               "scoring tables, the $50,000 cap, the ten-man roster, and the "
               "five-hitter-per-team limit, which counts hitters and not "
               "pitchers."),
    "MLB:FD": ("Scoring read from FanDuel's own Rules & Scoring tab and now "
               "matches it line for line. The tab does not show roster "
               "limits, so the $35,000 cap and the four-per-team maximum are "
               "still unconfirmed."),
    "NHL:DK": "Confirm the three skater step bonuses at 3 goals / 5 shots / 3 blocks.",
    "NHL:FD": "Confirm goalie save 0.6 and goal-against -3.0.",
    "EPL:DK": "UNVERIFIED. Peripheral rates (cross, tackle, interception) are low confidence.",
    "EPL:FD": "UNVERIFIED, and confirm FanDuel still runs EPL contests in your market.",
}


def get_config(sport: str, site: str) -> SportConfig:
    """Fetch the rules for a sport-site pair, e.g. ("NBA", "DK")."""

    key = f"{sport.upper()}:{site.upper()}"
    if key not in CONFIGS:
        available = ", ".join(sorted(CONFIGS))
        raise KeyError(f"No rules for {key}. Available: {available}")
    return CONFIGS[key]


def score_stat_line(
    stats: dict[str, float],
    positions: list[str],
    config: SportConfig,
) -> float:
    """Fantasy points for a stat line, including combination bonuses."""

    base = config.score_stat_line(stats, positions)
    return round(apply_combination_bonuses(base, stats, config.sport, config.site), 4)


__all__ = [
    "Bonus", "RosterSlot", "SportConfig", "StackRule",
    "CONFIGS", "SPORTS", "SITES", "VERIFICATION_NOTES",
    "get_config", "score_stat_line",
]
