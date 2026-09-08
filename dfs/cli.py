"""Command-line entry points.

    python -m dfs.cli build   --csv DKSalaries.csv --sport NBA --site DK --date 2026-01-15
    python -m dfs.cli build   --csv DKSalaries.csv --sport NFL --site DK --date 2026-09-07 \
                              --mode gpp --lineups 20
    python -m dfs.cli load-history --sport NFL
    python -m dfs.cli slates
    python -m dfs.cli resolve --slate 3
    python -m dfs.cli resolve --due     # every locked slate whose games have finished
    python -m dfs.cli calibrate
    python -m dfs.cli rules   --sport NBA --site DK
"""

from __future__ import annotations

import argparse
import sys

from dfs.db.database import Database
from dfs.export import ExportError, to_upload_csv, upload_filename
from dfs.ingest.stats import (
    CollectorError, PLANNED, has_collector, load_history, log_counts, refresh_history,
)
from dfs.pipeline import build_slate, resolve_slate_from_logs, slate_is_scorable
from dfs.projections.calibration import calibration_report
from dfs.sports import CONFIGS, SITES, SPORTS, VERIFICATION_NOTES, get_config


def _build(args: argparse.Namespace) -> int:
    database = Database(args.db)
    result = build_slate(
        database,
        args.csv,
        args.sport,
        args.site,
        args.date,
        mode=args.mode,
        lineup_count=args.lineups,
    )

    config = result.config
    print(f"{config.key}  slate {result.slate_id}  {args.date}")
    print(f"{len(result.pool)} players, {result.projected_count} projected\n")

    note = VERIFICATION_NOTES.get(config.key)
    if note and not config.rules_verified:
        print(f"!! Scoring rules unverified for {config.key}: {note}\n")

    for warning in result.warnings:
        print(f"!  {warning}")
    if result.warnings:
        print()

    for index, lineup in enumerate(result.lineups, start=1):
        print(
            f"--- lineup {index}  ${lineup.total_salary:,}  "
            f"proj {lineup.total_projection:.1f}  "
            f"ceil {lineup.total_ceiling:.1f}  "
            f"own {lineup.total_ownership:.0f}%"
        )
        for player in lineup.players:
            print(
                f"  {player.slot:<6} {player.name:<24} {player.team or '':<4} "
                f"${player.salary:>6,}  {player.projection:>6.1f}  "
                f"{player.ownership:>5.1f}%"
            )
        print()

    if args.export and result.lineups:
        try:
            payload = to_upload_csv(result.lineups, config)
        except ExportError as error:
            print(f"Could not export: {error}")
        else:
            if args.export == "-":
                print(payload)
            else:
                with open(args.export, "w", newline="", encoding="utf-8") as handle:
                    handle.write(payload)
                print(f"Wrote {len(result.lineups)} lineup(s) to {args.export}")
                print(f"Suggested name: {upload_filename(config, args.date)}")
            print()

    if len(result.lineups) > 1:
        print("--- exposure ---")
        for row in result.exposure[:15]:
            print(f"  {row['name']:<24} {row['exposure']:>6.0%}  ({row['count']})")

    database.close()
    return 0


def _load_history(args: argparse.Namespace) -> int:
    """Populate game logs so projections stop being the site average."""

    database = Database(args.db)

    if not has_collector(args.sport):
        planned = PLANNED.get(args.sport.upper(), "no source identified")
        print(f"No collector for {args.sport} yet (planned source: {planned}).")
        database.close()
        return 1

    before = log_counts(database).get(args.sport.upper(), 0)

    try:
        if args.refresh:
            summary = refresh_history(database, args.sport, force=True)
            if summary is None:
                print(f"{args.sport}: nothing to refresh.")
                database.close()
                return 0
        else:
            summary = load_history(args.sport, database, count=args.seasons)
    except CollectorError as error:
        print(f"Could not load history: {error}")
        database.close()
        return 1

    after = log_counts(database).get(args.sport.upper(), 0)

    print(
        f"{summary['sport']}: {summary['logs']:,} game logs for "
        f"{summary['players']:,} players across seasons "
        f"{', '.join(str(season) for season in summary['seasons'])}."
    )
    print(f"Stored logs went from {before:,} to {after:,}.")

    database.close()
    return 0


def _slates(args: argparse.Namespace) -> int:
    """List stored slates and how far each has got through the loop."""

    database = Database(args.db)
    rows = database.connection.execute(
        """
        SELECT
            sl.id, sl.sport, sl.site, sl.slate_date,
            (SELECT COUNT(*) FROM salaries s WHERE s.slate_id = sl.id) AS players,
            (SELECT COUNT(*) FROM projections p WHERE p.slate_id = sl.id) AS projected,
            (SELECT COUNT(*) FROM projections p WHERE p.slate_id = sl.id
             AND p.locked_at IS NOT NULL) AS locked,
            (SELECT COUNT(*) FROM actuals a WHERE a.slate_id = sl.id) AS resolved
        FROM slates sl ORDER BY sl.slate_date DESC, sl.id DESC
        """
    ).fetchall()

    if not rows:
        print("No slates stored yet. Build one with `dfs build`.")
        database.close()
        return 0

    print(f"{'id':>4} {'sport':<6} {'site':<5} {'date':<12} {'players':>7} {'proj':>6} {'locked':>7} {'resolved':>9}")
    for row in rows:
        print(
            f"{row['id']:>4} {row['sport']:<6} {row['site']:<5} {row['slate_date']:<12} "
            f"{row['players']:>7} {row['projected']:>6} {row['locked']:>7} {row['resolved']:>9}"
        )

    database.close()
    return 0


def _resolve(args: argparse.Namespace) -> int:
    """Score a slate against what actually happened."""

    if args.due:
        if args.lock:
            # Locking here would stamp slates whose games have already
            # been played, turning a backtest into something the
            # calibration report counts as a forecast.
            print("--lock cannot be combined with --due.")
            return 1
        return _resolve_due(args)

    database = Database(args.db)

    if args.lock:
        locked = database.lock_slate(args.slate)
        print(f"Locked {locked} projections.")

    try:
        summary = resolve_slate_from_logs(database, args.slate)
    except ValueError as error:
        print(error)
        database.close()
        return 1

    print(
        f"Slate {summary['slate_id']} ({summary['sport']}:{summary['site']} "
        f"{summary['slate_date']}): resolved {summary['resolved']} of "
        f"{summary['pool']} — {summary['played']} played, "
        f"{summary['did_not_play']} did not."
    )

    if summary["resolved"] and summary["played"] == 0:
        print(
            "No player in the pool has a game log for that date. Either the "
            "games have not been collected yet, or the slate date is wrong."
        )

    database.close()
    return 0


# How long to wait for box scores before treating their absence as a
# fault rather than as the ordinary lag between a game finishing and a
# feed publishing it.
BOX_SCORE_GRACE_DAYS = 3


def _days_since(slate_date: str) -> int:
    import datetime as _dt

    return (_dt.date.today() - _dt.date.fromisoformat(slate_date)).days


def _resolve_due(args: argparse.Namespace) -> int:
    """Score every locked slate whose games have finished.

    Written for a scheduled run. A locked slate is a forecast on the
    record and is worth nothing until it is scored, so leaving that to
    someone remembering to press a button is how a calibration table
    ends up too sparse to read.

    Refreshes each sport's history first: the box scores a slate is
    scored against are exactly the ones collected after its games, so
    resolving without refreshing finds nothing and records a slate full
    of players who "did not play".
    """

    database = Database(args.db)
    due = database.slates_awaiting_resolution()
    ready = [slate for slate in due if slate_is_scorable(slate["slate_date"])]

    if not ready:
        waiting = len(due) - len(ready)
        if waiting:
            print(f"Nothing to score yet: {waiting} locked slate(s) still in play.")
        else:
            print("Nothing to score: no locked slate is waiting on a result.")
        database.close()
        return 0

    # One refresh per sport, not per slate: two slates for the same
    # sport on the same day would otherwise download it twice.
    for sport in sorted({slate["sport"] for slate in ready}):
        try:
            summary = refresh_history(database, sport, force=True)
        except CollectorError as error:
            print(f"{sport}: could not refresh history ({error}).")
            continue
        if summary:
            print(f"{sport}: refreshed, {summary.get('logs', 0)} logs stored.")

    failures = 0
    for slate in ready:
        # Checked before scoring, not after. Resolution records a player
        # with no log as a zero, so running it before the box scores are
        # published writes a whole slate of zeros -- and the slate then
        # has results, so nothing ever revisits it. Skipping leaves it
        # due for the next run.
        if database.game_log_count(slate["sport"], slate["slate_date"]) == 0:
            waited = _days_since(slate["slate_date"])
            print(
                f"Slate {slate['id']} ({slate['sport']}:{slate['site']} "
                f"{slate['slate_date']}): no box scores stored for that date "
                f"yet, leaving it for the next run."
            )
            # Waiting a night for box scores is ordinary and must not
            # fail the run: a scheduled job that goes red on a normal
            # night is one you stop reading. Waiting a week is not
            # ordinary, and by then the forecast is rotting unscored.
            if waited > BOX_SCORE_GRACE_DAYS:
                print(
                    f"  Still nothing {waited} days after the slate. Either "
                    f"the sport's collector is failing or the slate date is "
                    f"wrong; it cannot be scored until this is fixed."
                )
                failures += 1
            continue

        try:
            summary = resolve_slate_from_logs(database, slate["id"], refresh=False)
        except ValueError as error:
            print(f"Slate {slate['id']}: {error}")
            failures += 1
            continue

        print(
            f"Slate {summary['slate_id']} ({summary['sport']}:{summary['site']} "
            f"{summary['slate_date']}): resolved {summary['resolved']} of "
            f"{summary['pool']} — {summary['played']} played, "
            f"{summary['did_not_play']} did not."
        )

        # Logs exist for the date but none belong to this pool, so the
        # names did not join. Worth a loud line: the slate has been
        # recorded as a day nobody played, and that is a bad row on the
        # calibration record rather than a missing one.
        if summary["played"] == 0:
            print(
                f"  None of slate {summary['slate_id']}'s players matched a "
                f"log for {summary['slate_date']}, though logs exist for that "
                f"date. Check the slate's sport and date."
            )
            failures += 1

    database.close()
    return 1 if failures else 0


def _reset(args: argparse.Namespace) -> int:
    """Empty the database, after saying exactly what that costs.

    Without `--confirm` this reports and changes nothing. Deleting is
    not symmetrical with collecting: game logs rebuild themselves from
    the same feeds, but a locked projection cannot be recreated, because
    a forecast made after the game is not a forecast.
    """

    database = Database(args.db)
    counts = database.row_counts()
    total = sum(counts.values())

    if not total:
        print("The database is already empty.")
        database.close()
        return 0

    locked = database.connection.execute(
        "SELECT COUNT(*) AS n FROM projections WHERE locked_at IS NOT NULL"
    ).fetchone()
    locked = int(locked["n"]) if locked else 0

    print(f"{'table':<16} {'rows':>10}")
    for table, count in counts.items():
        print(f"{table:<16} {count:>10,}")
    print(f"{'total':<16} {total:>10,}")

    if locked:
        print()
        print(
            f"{locked:,} of those projections are LOCKED -- forecasts made "
            f"before their games. Collected history rebuilds itself; these "
            f"cannot be made again."
        )

    if not args.confirm:
        print()
        print("Nothing was deleted. Re-run with --confirm to empty these tables.")
        database.close()
        return 0

    removed = database.reset()
    print()
    print(f"Deleted {sum(removed.values()):,} rows. The database is empty.")
    database.close()
    return 0


def _calibrate(args: argparse.Namespace) -> int:
    database = Database(args.db)
    records = database.resolved_projections(args.sport)
    report = calibration_report(records)

    print(report["verdict"])
    print()

    if report["overall"]:
        overall = report["overall"]
        print(
            f"overall   n={overall['count']:<6} MAE={overall['mae']:<7} "
            f"RMSE={overall['rmse']:<7} bias={overall['bias']:+.2f}  "
            f"rho={overall['correlation']}"
        )

    if report.get("forward_verdict"):
        print(report["forward_verdict"])
        print()

    for label in ("forward", "backtest"):
        row = report.get(label)
        if row:
            skill = f"{row['skill']:+.3f}" if row["skill"] is not None else "n/a"
            print(f"  {label:<10} n={row['count']:<6} MAE={row['mae']:<7} skill={skill}")

    for row in report["by_sport"]:
        skill = f"{row['skill']:+.3f}" if row["skill"] is not None else "n/a"
        print(f"  {row['scope']:<10} n={row['count']:<6} MAE={row['mae']:<7} skill={skill}")

    if report["by_position"]:
        print("\nby position:")
        for row in report["by_position"]:
            print(
                f"  {row['scope']:<10} n={row['count']:<6} MAE={row['mae']:<7} "
                f"bias={row['bias']:+.2f}"
            )

    database.close()
    return 0


def _rules(args: argparse.Namespace) -> int:
    configs = (
        [get_config(args.sport, args.site)]
        if args.sport and args.site
        else sorted(CONFIGS.values(), key=lambda config: config.key)
    )

    for config in configs:
        slots = ", ".join(
            f"{slot.name}x{slot.count}" if slot.count > 1 else slot.name
            for slot in config.roster
        )
        print(f"{config.key:<9} cap ${config.salary_cap:,}  {config.roster_size} slots")
        print(f"          {slots}")
        print(f"          max/team {config.max_per_team or 'none'}, min games {config.min_games}")
        note = VERIFICATION_NOTES.get(config.key)
        if note:
            print(f"          verify: {note}")
        print()

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dfs", description=__doc__)
    parser.add_argument("--db", default="data/dfs.db", help="SQLite path")

    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="Build lineups from a salary export")
    build.add_argument("--csv", required=True, help="Path to the site's salary CSV")
    build.add_argument("--sport", required=True, choices=SPORTS)
    build.add_argument("--site", required=True, choices=SITES)
    build.add_argument("--date", required=True, help="Slate date, YYYY-MM-DD")
    build.add_argument("--mode", default="cash", choices=("cash", "gpp"))
    build.add_argument("--lineups", type=int, default=1)
    build.add_argument(
        "--export", metavar="PATH", default=None,
        help="Write the lineups as a site upload CSV (use - for stdout)",
    )
    build.set_defaults(handler=_build)

    history = subparsers.add_parser(
        "load-history", help="Download historical stats into the game-log table"
    )
    history.add_argument("--sport", required=True, choices=SPORTS)
    history.add_argument(
        "--seasons", type=int, default=3,
        help="How many recent seasons to fetch (default 3)",
    )
    history.add_argument(
        "--refresh", action="store_true",
        help="Update the current season only, from the newest stored game onward",
    )
    history.set_defaults(handler=_load_history)

    slates = subparsers.add_parser("slates", help="List stored slates and their status")
    slates.set_defaults(handler=_slates)

    resolve = subparsers.add_parser("resolve", help="Score a slate against what happened")
    target = resolve.add_mutually_exclusive_group(required=True)
    target.add_argument("--slate", type=int, help="Slate id, from `dfs slates`")
    target.add_argument(
        "--due", action="store_true",
        help="Every locked slate whose games have finished (for a scheduled run)",
    )
    resolve.add_argument(
        "--lock", action="store_true",
        help="Lock the slate's projections first (normally done before its games)",
    )
    resolve.set_defaults(handler=_resolve)

    reset = subparsers.add_parser(
        "reset", help="Empty the database (reports first; needs --confirm)"
    )
    reset.add_argument(
        "--confirm", action="store_true",
        help="Actually delete. Without this the command only reports.",
    )
    reset.set_defaults(handler=_reset)

    calibrate = subparsers.add_parser("calibrate", help="Score projections against results")
    calibrate.add_argument("--sport", default=None, choices=SPORTS)
    calibrate.set_defaults(handler=_calibrate)

    rules = subparsers.add_parser("rules", help="Show roster and scoring rules")
    rules.add_argument("--sport", default=None, choices=SPORTS)
    rules.add_argument("--site", default=None, choices=SITES)
    rules.set_defaults(handler=_rules)

    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
