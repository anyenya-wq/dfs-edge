"""End-to-end slate processing: salaries in, lineups out.

The whole flow in one place so it can run from a command line, from the
Streamlit board, or from a scheduled job without three implementations
drifting apart.

The order is load, project, price the field, research, optimise. It
matters: research must land before optimisation so a ruled-out player is
gone from the pool rather than merely unattractive, and ownership must
land before optimisation so the tournament objective has something to
fade.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from typing import Any

from dfs.db.database import Database
from dfs.ingest.salaries import expand_roster_eligibility, parse_salaries
from dfs.optimizer.lineup import Lineup, exposure_report, optimize_lineups
from dfs.optimizer.rules import OptimizerSettings, cash_settings, gpp_settings
from dfs.ownership.leverage import apply_leverage, leverage_board
from dfs.ingest.teams import is_defence
from dfs.projections.baseline import build_pool_rates, project_player
from dfs.projections.defence import blend_with_matchup, concession_levels
from dfs.sports import SportConfig, get_config, score_stat_line


@dataclasses.dataclass
class SlateResult:
    slate_id: int
    config: SportConfig
    pool: list[dict[str, Any]]
    lineups: list[Lineup]
    exposure: list[dict[str, Any]]
    leverage: list[dict[str, Any]]
    projected_count: int
    warnings: list[str]


def load_slate(
    database: Database,
    salary_source: str,
    sport: str,
    site: str,
    slate_date: str,
    name: str = "main",
) -> tuple[int, SportConfig, list[dict[str, Any]]]:
    """Parse a salary export into the database and return the pool."""

    config = get_config(sport, site)
    pool = parse_salaries(salary_source, sport, site)

    if not pool:
        raise ValueError(
            "No players parsed from the salary file. Check that it is the "
            "export from the contest entry screen and not a contest-results file."
        )

    pool = expand_roster_eligibility(pool, config)

    slate_id = database.upsert_slate(sport, site, slate_date, name)
    database.save_salaries(slate_id, pool)

    return slate_id, config, database.player_pool(slate_id)


def validate_pool(
    pool: Sequence[dict[str, Any]],
    config: SportConfig,
) -> list[str]:
    """Check that a parsed pool actually belongs to the selected sport.

    Uploading the wrong file is the easiest mistake to make and the
    hardest to diagnose from the symptom, because nothing raises: an NBA
    export read as NFL parses perfectly, produces a pool of players
    eligible for no roster slot, and yields an empty build that looks
    like a solver problem. Checking eligibility up front turns that into
    a sentence naming the actual cause.
    """

    warnings: list[str] = []

    if not pool:
        return ["The salary file parsed to zero players."]

    eligible = sum(
        1
        for player in pool
        if any(slot.accepts([str(p).upper() for p in player.get("positions") or []])
               for slot in config.roster)
    )

    share = eligible / len(pool)

    if share < 0.5:
        found = sorted({
            str(position).upper()
            for player in pool
            for position in (player.get("positions") or [])
        })[:12]
        expected = sorted({position for slot in config.roster for position in slot.eligible})
        warnings.append(
            f"Only {eligible} of {len(pool)} players are eligible for any "
            f"{config.key} roster slot. The file contains positions "
            f"{', '.join(found)}, but {config.sport} expects "
            f"{', '.join(expected)}. This usually means the sport selector "
            f"does not match the file you uploaded."
        )

    return warnings


def project_slate(
    database: Database,
    slate_id: int,
    config: SportConfig,
    pool: Sequence[dict[str, Any]],
    slate_date: str,
    *,
    store: bool = True,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Attach projections to every player in a pool.

    Game logs are fetched with a `before` cutoff at the slate date, so a
    projection can never see the game it is projecting. That guard is
    what makes a backtest over stored slates meaningful rather than
    circular.

    Players with no history fall back to the site's own season average.
    That is a weak projection and it is flagged, but dropping them is
    worse: an unprojected player is invisible to the optimizer, and on
    a slate full of debuts that silently shrinks the pool.
    """

    warnings: list[str] = []

    logs_by_player = {
        str(player["player_id"]): database.game_logs(
            str(player["player_id"]), before=slate_date
        )
        for player in pool
    }

    pool_rates = build_pool_rates(pool, logs_by_player, config)

    # How generous each offence has been to defences. Measured across
    # every defence in the history rather than per player, and only
    # computed when the pool actually contains one.
    levels: dict[str, float] = {}
    league_mean = 0.0
    if any(is_defence(player.get("positions") or []) for player in pool):
        levels, league_mean = concession_levels(
            database.logs_by_player_prefix(
                f"{config.sport.lower()}:dst:", before=slate_date
            ),
            config,
        )

    projected: list[dict[str, Any]] = []
    fallback_count = 0

    for player in pool:
        record = dict(player)
        logs = logs_by_player.get(str(player["player_id"]), [])

        if logs:
            projection = project_player(record, logs, config, pool_rates=pool_rates)
            record["projected_points"] = projection.projected_points
            record["ceiling"] = projection.ceiling
            record["floor"] = projection.floor
            record["stdev"] = projection.stdev
            record["projected_opportunity"] = projection.projected_opportunity
            record["projection_note"] = projection.notes
            record["projection_source"] = "model"

            # A defence's own form is worth almost nothing on its own;
            # the offence it is facing is most of the signal. Applied
            # after the model projection rather than inside it, because
            # this replaces the level rather than scaling the rate.
            if is_defence(record.get("positions") or []):
                blended, note = blend_with_matchup(
                    projection.projected_points,
                    record.get("opponent"),
                    levels,
                    league_mean,
                )
                shift = blended - projection.projected_points
                record["projected_points"] = round(blended, 3)
                record["ceiling"] = round(projection.ceiling + shift, 3)
                record["floor"] = round(projection.floor + shift, 3)
                record["projection_note"] = " ".join(
                    part for part in (projection.notes, note) if part
                )
                record["projection_source"] = "model+matchup"
        else:
            average = float(record.get("site_avg_points") or 0.0)
            record["projected_points"] = round(average, 3)
            # No history means no measured spread either, so a generic
            # 35% coefficient of variation stands in. It is roughly
            # right for most positions in most sports and keeps the
            # ceiling from being either absent or fictional.
            record["stdev"] = round(average * 0.35, 3)
            record["ceiling"] = round(average * 1.45, 3)
            record["floor"] = round(average * 0.55, 3)
            record["projection_note"] = "No game logs; using the site's season average."
            record["projection_source"] = "site_average"
            if average > 0:
                fallback_count += 1

        projected.append(record)

        if store and record["projected_points"] > 0:
            database.save_projection(slate_id, {
                "player_id": record["player_id"],
                "projected_points": record["projected_points"],
                "floor": record["floor"],
                "ceiling": record["ceiling"],
                "stdev": record["stdev"],
                "projected_opportunity": record.get("projected_opportunity"),
                "model": "baseline",
            })

    if fallback_count:
        warnings.append(
            f"{fallback_count} of {len(pool)} players had no game logs and fell "
            "back to the site's season average. Import history to improve them."
        )

    unprojected = sum(1 for record in projected if record["projected_points"] <= 0)
    if unprojected:
        warnings.append(
            f"{unprojected} players have no projection at all and were excluded "
            "from lineup construction."
        )

    return projected, warnings


def build_slate(
    database: Database,
    salary_source: str,
    sport: str,
    site: str,
    slate_date: str,
    *,
    mode: str = "cash",
    lineup_count: int = 1,
    settings: OptimizerSettings | None = None,
    name: str = "main",
    store_lineups: bool = True,
) -> SlateResult:
    """Run the whole flow for one slate."""

    slate_id, config, pool = load_slate(
        database, salary_source, sport, site, slate_date, name
    )

    warnings = validate_pool(pool, config)
    projected, projection_warnings = project_slate(
        database, slate_id, config, pool, slate_date
    )
    warnings.extend(projection_warnings)
    projected = apply_leverage(projected, config.roster_size)

    if settings is None:
        settings = (
            gpp_settings(config, lineups=lineup_count)
            if mode == "gpp"
            else cash_settings(config)
        )

    lineups = optimize_lineups(projected, config, settings, count=lineup_count)

    if not lineups:
        warnings.append(
            "No legal lineup exists for this pool under these constraints. "
            "The usual causes are a sport/file mismatch, a pool too small to "
            "fill every slot, or locks that cannot coexist with the cap."
        )
    elif len(lineups) < lineup_count:
        warnings.append(
            f"Requested {lineup_count} lineups but the constraints only allowed "
            f"{len(lineups)}. Loosen the exposure cap or the uniqueness minimum."
        )

    if store_lineups:
        for lineup in lineups:
            database.save_lineup(slate_id, lineup.as_record())

    return SlateResult(
        slate_id=slate_id,
        config=config,
        pool=projected,
        lineups=lineups,
        exposure=exposure_report(lineups),
        leverage=leverage_board(projected),
        projected_count=sum(1 for p in projected if p["projected_points"] > 0),
        warnings=warnings,
    )


def slate_is_scorable(slate_date: str, today: str | None = None) -> bool:
    """Whether a slate's games are far enough along to score.

    Strictly-past-or-today. A slate dated today becomes scorable as soon
    as its games finish, which is usually the same evening; requiring
    tomorrow would leave the record permanently a day behind. When the
    games have not actually finished, resolution reports that it found
    nothing and says why -- a more useful answer than refusing to try.
    """

    import datetime as _dt

    today = today or _dt.date.today().isoformat()
    return slate_date <= today


def resolve_slate_from_logs(
    database: Database,
    slate_id: int,
    refresh: bool = True,
) -> dict[str, Any]:
    """Resolve a slate from the game logs the collectors already fetch.

    No second data source is needed: the box scores that train the
    projections are the same box scores that score them. Refreshing
    first is the default because a slate is resolved shortly after its
    games finish, which is exactly when the stored history does not yet
    contain them.

    A player in the pool with no log for that date did not play. That is
    recorded as zero rather than skipped -- a projection that expected
    points from someone who never took the floor was wrong, and dropping
    those rows would quietly remove the model's worst misses from its
    own report card.
    """

    slate = database.connection.execute(
        "SELECT sport, site, slate_date FROM slates WHERE id = ?", (slate_id,)
    ).fetchone()

    if slate is None:
        raise ValueError(f"No slate with id {slate_id}.")

    config = get_config(slate["sport"], slate["site"])
    slate_date = slate["slate_date"]

    if refresh:
        from dfs.ingest.stats import refresh_history

        try:
            refresh_history(database, slate["sport"], force=True)
        except Exception:  # noqa: BLE001 - resolution proceeds regardless
            # A failed refresh is not fatal: whatever is already stored
            # may still cover the slate, and reporting how little
            # resolved is more useful than raising.
            pass

    pool = database.player_pool(slate_id)

    logs = {
        row["player_id"]: row
        for row in database.connection.execute(
            "SELECT player_id, opportunity, stats FROM game_logs WHERE sport = ? AND game_date = ?",
            (slate["sport"].upper(), slate_date),
        )
    }

    played = 0
    absent = 0

    for player in pool:
        log = logs.get(player["player_id"])

        if log is None:
            database.save_actual(slate_id, {
                "player_id": player["player_id"],
                "actual_points": 0.0,
                "actual_opportunity": 0.0,
                "stats": {},
            })
            absent += 1
            continue

        stats = json.loads(log["stats"] or "{}")
        database.save_actual(slate_id, {
            "player_id": player["player_id"],
            "actual_points": score_stat_line(stats, player["positions"], config),
            "actual_opportunity": log["opportunity"],
            "stats": stats,
        })
        played += 1

    return {
        "slate_id": slate_id,
        "sport": slate["sport"],
        "site": slate["site"],
        "slate_date": slate_date,
        "resolved": played + absent,
        "played": played,
        "did_not_play": absent,
        "pool": len(pool),
    }


def resolve_slate(
    database: Database,
    slate_id: int,
    box_scores: Sequence[dict[str, Any]],
    config: SportConfig,
) -> int:
    """Record what actually happened, scoring box lines under site rules.

    Takes raw stat lines rather than fantasy totals so the same input
    resolves a DraftKings slate and a FanDuel one correctly, and so a
    later correction to a scoring table can be replayed over history.
    """

    positions_by_player = {
        str(player["player_id"]): player.get("positions") or []
        for player in database.player_pool(slate_id)
    }

    resolved = 0
    for line in box_scores:
        player_id = str(line["player_id"])
        stats = dict(line.get("stats", {}))
        positions = line.get("positions") or positions_by_player.get(player_id, [])

        database.save_actual(slate_id, {
            "player_id": player_id,
            "actual_points": score_stat_line(stats, list(positions), config),
            "actual_opportunity": line.get("opportunity"),
            "actual_ownership": line.get("actual_ownership"),
            "stats": stats,
        })
        resolved += 1

    return resolved
