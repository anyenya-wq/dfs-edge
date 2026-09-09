"""Who, of the people in a pool, is actually in today's game.

A salary export lists everyone on the roster. Three different things
separate that from the people who will take the field, and they are not
symmetric -- which is the whole reason this is its own module rather
than a filter.

**Ruled out.** The site says so. Nothing to infer.

**A pitcher whose team-mate was announced.** One pitcher starts and the
rest do not appear, so an announcement settles the whole staff. A team
that has announced nobody settles nothing, and keeps all of its arms.

**A hitter whose team posted a lineup without him.** The strongest of
the three: the lineup is published and he is not in it, so he is on the
bench and worth zero. But a team that has *not* posted keeps every
hitter, because for a hitter "not yet listed" usually means "will
play" -- unlike a pitcher, where it means nobody yet knows which arm it
is.

The asymmetry matters. Treating an unposted lineup as an absence would
delete most of the slate for most of the afternoon.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from dfs.ingest.salaries import BENCHED, is_ruled_out


def _positions(player: Mapping[str, Any]) -> set[str]:
    return {str(position).upper() for position in player.get("positions") or []}


def is_starter_position(player: Mapping[str, Any], starter_positions) -> bool:
    """Whether this player fills a one-per-team slot: pitcher, goalie."""

    return bool(_positions(player) & {str(p).upper() for p in starter_positions})


def ruled_out(pool: Sequence[Mapping[str, Any]]) -> set[str]:
    """Players the site has marked as not appearing."""

    return {
        str(player["player_id"]) for player in pool
        if is_ruled_out(player.get("injury_status"))
    }


def announced_starters(pool, starter_positions) -> set[str]:
    """Pitchers and goalies the site says are starting."""

    return {
        str(player["player_id"]) for player in pool
        if is_starter_position(player, starter_positions)
        and player.get("starting")
        and str(player["starting"]).upper() != BENCHED
        and not player.get("batting_order")
    }


def teams_with_posted_lineups(pool) -> set[str]:
    """Teams whose batting order has been published."""

    return {
        str(player["team"]) for player in pool
        if player.get("batting_order") and player.get("team")
    }


def benched_hitters(pool, starter_positions) -> set[str]:
    """Hitters left out of their own team's posted lineup.

    Two ways of knowing, and the explicit one wins. FanDuel writes a
    batting order of `0` against a player its published card leaves out,
    and blank against one whose card is not out yet -- so it says
    outright who is sitting. DraftKings leaves both blank, so there it
    has to be inferred: the team posted a lineup and this hitter is not
    in it.

    The inference is very slightly coarser, which is why it is the
    fallback. A player added to a pool after the card was published
    reads as benched under it, and as unknown under FanDuel's column.
    """

    posted = teams_with_posted_lineups(pool)
    benched = set()

    for player in pool:
        if is_starter_position(player, starter_positions):
            continue
        if player.get("batting_order"):
            continue

        if str(player.get("starting") or "").upper() == BENCHED:
            benched.add(str(player["player_id"]))
        elif str(player.get("team") or "") in posted:
            benched.add(str(player["player_id"]))

    return benched


def sidelined_pitchers(pool, confirmed, starter_positions, whole_slate=True) -> set[str]:
    """Pitchers who are not the named starter, and so are not playing.

    Once anybody has been announced, only announced pitchers are worth
    rostering. A pitcher nobody has named is not known to be starting,
    and one who is not starting throws an inning in relief at most --
    while projecting on his rate per inning, which is how a reliever
    priced like a starter wins an optimizer outright.

    `whole_slate` is that rule. Turning it off narrows the exclusion to
    teams that have named someone, which keeps the rest of the league's
    bullpens eligible -- only useful if you know a starter the file does
    not, and even then naming him in the confirmed list is better.

    Nothing is excluded until something is announced. First thing in the
    morning the whole staff of every team is still in, which is correct:
    nobody knows anything yet.
    """

    confirmed = {str(player_id) for player_id in confirmed}

    if not confirmed:
        return set()

    by_id = {str(player["player_id"]): player for player in pool}

    decided = {
        str(by_id[player_id].get("team") or "")
        for player_id in confirmed
        if player_id in by_id and by_id[player_id].get("team")
    }

    return {
        str(player["player_id"]) for player in pool
        if is_starter_position(player, starter_positions)
        and str(player["player_id"]) not in confirmed
        and (whole_slate or str(player.get("team") or "") in decided)
    }
