"""SQLite storage for slates, projections, results, and lineups.

Deliberately a separate database from anything else you run. Daily
fantasy writes player-level rows for every slate -- a single NBA night
is a few hundred, a full season across five sports is six figures --
which is the wrong shape for a database that gets committed to git.
Keep `data/dfs.db` out of version control and let it grow.

The schema's organising idea is that a projection is written *before*
lock and never updated afterwards. `projections.locked_at` records the
moment the slate became unchangeable, and resolution writes to a
separate `actuals` table rather than back into the projection row. That
separation is what makes the calibration numbers trustworthy: there is
no code path that can quietly improve a projection after seeing the
result.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from dfs.db.connection import Connection, connect, is_postgres_url

DEFAULT_PATH = Path("data/dfs.db")

# Where to store, when nothing is passed. A URL in the environment is
# how a hosted deployment points at PostgreSQL without any code change;
# Streamlit Cloud sets it from its secrets, a Codespace leaves it unset
# and gets the local file.
URL_ENVIRONMENT_VARIABLE = "DFS_DATABASE_URL"


class Database:
    """A SQLite handle safe to share across threads.

    Streamlit runs every script pass on a fresh thread while caching
    this object across them, so the default one-thread-per-connection
    rule rejects the second interaction with a confusing
    ProgrammingError. Opening with `check_same_thread=False` lifts that
    restriction, which then makes serialising writes our
    responsibility -- hence the lock around every mutating method.

    Reads are left unguarded. SQLite handles concurrent readers itself,
    and taking the lock on every query would serialise the dashboard
    against a background collector for no benefit.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        target = str(path) if path is not None else os.environ.get(
            URL_ENVIRONMENT_VARIABLE, str(DEFAULT_PATH)
        )

        self.url = target
        self.path = None if is_postgres_url(target) else Path(target)
        self.connection: Connection = connect(target)
        self.dialect = self.connection.dialect
        # Reentrant because the write methods call one another --
        # `save_salaries` takes the lock and then calls `upsert_player`,
        # which takes it again.
        self._lock = threading.RLock()
        self.create_tables()

        for table, columns in self.ADDED_COLUMNS.items():
            self._add_missing_columns(table, columns)

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    # Columns added after the first databases were created. A deployed
    # database holds the collected history and every stored slate, so it
    # is migrated rather than recreated.
    ADDED_COLUMNS = {
        "salaries": {"starting": "TEXT", "batting_order": "INTEGER"},
    }

    def _columns(self, table: str) -> set[str]:
        """The columns a table actually has right now."""

        if self.dialect.name == "postgres":
            rows = self.connection.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                (table,),
            ).fetchall()
            return {str(row["column_name"]) for row in rows}

        rows = self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    def _add_missing_columns(self, table: str, columns: Mapping[str, str]) -> None:
        """Add columns a table is missing, leaving existing data alone.

        `CREATE TABLE IF NOT EXISTS` does nothing to a table that already
        exists, so a new column never reaches a database created before
        it. That is not hypothetical: the deployed database holds the
        collected history and every stored slate, and dropping it to
        pick up a column would throw away the record this whole project
        exists to build.
        """

        existing = self._columns(table)

        with self._lock:
            for name, definition in columns.items():
                if name in existing:
                    continue
                self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            self.connection.commit()

    def _create(self, sql: str) -> None:
        """Run one schema statement, spelled for whichever backend is open."""

        self.connection.execute(self._ddl(sql))

    def _ddl(self, sql: str) -> str:
        """Swap the two type spellings the backends disagree about."""

        sql = sql.replace(
            "INTEGER PRIMARY KEY AUTOINCREMENT", self.dialect.identity_primary_key
        )
        return re.sub(r"\bREAL\b", self.dialect.real, sql)

    def create_tables(self) -> None:
        with self._lock:

            self._create(
                """
                CREATE TABLE IF NOT EXISTS slates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sport TEXT NOT NULL,
                    site TEXT NOT NULL,
                    slate_date TEXT NOT NULL,
                    name TEXT,
                    lock_time TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (sport, site, slate_date, name)
                )
                """
            )

            # Players are keyed by a slug derived from name and sport rather
            # than by a site id, because the same player carries a different
            # id on each site and we need to join a DraftKings pool to a
            # FanDuel one and to a stats feed that knows neither.
            self._create(
                """
                CREATE TABLE IF NOT EXISTS players (
                    player_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    sport TEXT NOT NULL,
                    team TEXT,
                    positions TEXT,
                    dk_id TEXT,
                    fd_id TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            self._create(
                """
                CREATE TABLE IF NOT EXISTS salaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slate_id INTEGER NOT NULL,
                    player_id TEXT NOT NULL,
                    salary INTEGER NOT NULL,
                    roster_positions TEXT,
                    team TEXT,
                    opponent TEXT,
                    home INTEGER,
                    game_id TEXT,
                    site_avg_points REAL,
                    injury_status TEXT,
                    -- What the site itself says about availability. The
                    -- export carries a batting order once a lineup is
                    -- posted, and a code on a pitcher once he is
                    -- announced; both are blank until then.
                    starting TEXT,
                    batting_order INTEGER,
                    UNIQUE (slate_id, player_id),
                    FOREIGN KEY (slate_id) REFERENCES slates(id),
                    FOREIGN KEY (player_id) REFERENCES players(player_id)
                )
                """
            )

            # Historical box scores, normalised across sports. `stats` holds
            # the raw per-sport line as JSON so a scoring-rule change can be
            # replayed over history without re-fetching anything.
            self._create(
                """
                CREATE TABLE IF NOT EXISTS game_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    player_id TEXT NOT NULL,
                    sport TEXT NOT NULL,
                    game_date TEXT NOT NULL,
                    team TEXT,
                    opponent TEXT,
                    home INTEGER,
                    opportunity REAL,
                    stats TEXT,
                    UNIQUE (player_id, game_date),
                    FOREIGN KEY (player_id) REFERENCES players(player_id)
                )
                """
            )

            self._create(
                "CREATE INDEX IF NOT EXISTS idx_logs_player_date ON game_logs(player_id, game_date)"
            )

            self._create(
                "CREATE INDEX IF NOT EXISTS idx_logs_sport_date ON game_logs(sport, game_date)"
            )

            # When each sport's history was last collected. Without this
            # the app cannot tell "loaded three months ago" from "loaded
            # today", and stale history is invisible precisely because
            # nothing fails -- the projections simply keep using games
            # that stopped being recent.
            self._create(
                """
                CREATE TABLE IF NOT EXISTS collector_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sport TEXT NOT NULL,
                    ran_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    logs INTEGER,
                    seasons TEXT
                )
                """
            )

            self._create(
                """
                CREATE TABLE IF NOT EXISTS projections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slate_id INTEGER NOT NULL,
                    player_id TEXT NOT NULL,
                    projected_points REAL NOT NULL,
                    floor REAL,
                    ceiling REAL,
                    stdev REAL,
                    projected_opportunity REAL,
                    projected_ownership REAL,
                    model TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    locked_at TEXT,
                    UNIQUE (slate_id, player_id, model),
                    FOREIGN KEY (slate_id) REFERENCES slates(id),
                    FOREIGN KEY (player_id) REFERENCES players(player_id)
                )
                """
            )

            self._create(
                """
                CREATE TABLE IF NOT EXISTS actuals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slate_id INTEGER NOT NULL,
                    player_id TEXT NOT NULL,
                    actual_points REAL,
                    actual_opportunity REAL,
                    actual_ownership REAL,
                    stats TEXT,
                    resolved_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (slate_id, player_id),
                    FOREIGN KEY (slate_id) REFERENCES slates(id)
                )
                """
            )

            self._create(
                """
                CREATE TABLE IF NOT EXISTS lineups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slate_id INTEGER NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    mode TEXT,
                    total_salary INTEGER,
                    total_projection REAL,
                    total_ceiling REAL,
                    total_ownership REAL,
                    players TEXT NOT NULL,
                    FOREIGN KEY (slate_id) REFERENCES slates(id)
                )
                """
            )

            # Research output, mirroring the brief-not-estimate split that
            # the prediction-market project arrived at: the written read is
            # always stored, the numeric multiplier only when the evidence
            # supported one.
            self._create(
                """
                CREATE TABLE IF NOT EXISTS player_briefs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slate_id INTEGER NOT NULL,
                    player_id TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    status TEXT,
                    situation TEXT,
                    role_change TEXT,
                    what_would_settle_it TEXT,
                    multiplier REAL,
                    estimated INTEGER DEFAULT 0,
                    dispersion REAL,
                    sample_count INTEGER,
                    model TEXT,
                    FOREIGN KEY (slate_id) REFERENCES slates(id)
                )
                """
            )

            self.connection.commit()

        # ------------------------------------------------------------------
        # Slates and players
        # ------------------------------------------------------------------


    def upsert_slate(
        self,
        sport: str,
        site: str,
        slate_date: str,
        name: str = "main",
        lock_time: str | None = None,
    ) -> int:
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO slates (sport, site, slate_date, name, lock_time)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (sport, site, slate_date, name)
                DO UPDATE SET lock_time = COALESCE(excluded.lock_time, slates.lock_time)
                """,
                (sport.upper(), site.upper(), slate_date, name, lock_time),
            )
            self.connection.commit()
            row = self.connection.execute(
                """
                SELECT id FROM slates
                WHERE sport = ? AND site = ? AND slate_date = ? AND name = ?
                """,
                (sport.upper(), site.upper(), slate_date, name),
            ).fetchone()
            return int(row["id"])


    def upsert_player(self, player: Mapping[str, Any]) -> None:
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO players (player_id, name, sport, team, positions, dk_id, fd_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT (player_id) DO UPDATE SET
                    name = excluded.name,
                    team = COALESCE(excluded.team, players.team),
                    positions = COALESCE(excluded.positions, players.positions),
                    dk_id = COALESCE(excluded.dk_id, players.dk_id),
                    fd_id = COALESCE(excluded.fd_id, players.fd_id),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    player["player_id"],
                    player["name"],
                    player["sport"].upper(),
                    player.get("team"),
                    json.dumps(list(player.get("positions", []))),
                    player.get("dk_id"),
                    player.get("fd_id"),
                ),
            )


    def save_salaries(
        self,
        slate_id: int,
        rows: Iterable[Mapping[str, Any]],
        replace: bool = True,
    ) -> int:
        """Store a slate's salary pool.

        Replaces by default, because a salary file *is* the pool rather
        than an addition to it. Re-downloading after a late scratch and
        re-uploading should leave the new pool, not the union of both --
        a merge silently keeps withdrawn players rosterable and, when
        the sport was set wrong on the first attempt, leaves an entire
        foreign sport in the pool where it will quietly be optimised
        over.
        """

        with self._lock:
            if replace:
                self.connection.execute(
                    "DELETE FROM salaries WHERE slate_id = ?", (slate_id,)
                )

                # Projections for players the new file does not contain
                # would otherwise survive their own pool. A re-upload
                # after a late scratch shrinks the slate, and the count
                # of projections would keep reporting the old, larger
                # number -- 971 against a 560-player pool in one real
                # case, all of it unresolvable because resolution walks
                # the current pool.
                #
                # Locked rows are spared. Those are on the calibration
                # record and deleting them would erase a forecast that
                # was legitimately made, which is a far worse fault than
                # a stale count.
                self.connection.execute(
                    "DELETE FROM projections WHERE slate_id = ? AND locked_at IS NULL",
                    (slate_id,),
                )

            count = 0
            for row in rows:
                self.upsert_player(row)
                self.connection.execute(
                    """
                    INSERT INTO salaries (
                        slate_id, player_id, salary, roster_positions,
                        team, opponent, home, game_id, site_avg_points,
                        injury_status, starting, batting_order
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (slate_id, player_id) DO UPDATE SET
                        salary = excluded.salary,
                        roster_positions = excluded.roster_positions,
                        team = excluded.team,
                        opponent = excluded.opponent,
                        home = excluded.home,
                        game_id = excluded.game_id,
                        site_avg_points = excluded.site_avg_points,
                        injury_status = excluded.injury_status,
                        starting = excluded.starting,
                        batting_order = excluded.batting_order
                    """,
                    (
                        slate_id,
                        row["player_id"],
                        int(row["salary"]),
                        json.dumps(list(row.get("roster_positions", row.get("positions", [])))),
                        row.get("team"),
                        row.get("opponent"),
                        1 if row.get("home") else 0,
                        row.get("game_id"),
                        row.get("site_avg_points"),
                        row.get("injury_status"),
                        row.get("starting"),
                        row.get("batting_order"),
                    ),
                )
                count += 1
            self.connection.commit()
            return count


    def find_slate(
        self, sport: str, site: str, slate_date: str, name: str = "main"
    ) -> int | None:
        """An existing slate's id, or None. Creates nothing.

        `upsert_slate` would create one, which is wrong for a caller
        that only wants to know whether a pool was already uploaded --
        an empty slate looks the same as a real one until you read it.
        """

        row = self.connection.execute(
            """
            SELECT id FROM slates
            WHERE sport = ? AND site = ? AND slate_date = ? AND name = ?
            """,
            (sport.upper(), site.upper(), slate_date, name),
        ).fetchone()

        return int(row["id"]) if row else None

    # Ordered so a child is emptied before its parent. Reversed, this
    # is the order rows may be created in.
    RESET_TABLES = (
        "actuals", "projections", "lineups", "salaries", "slates",
        "player_briefs", "game_logs", "collector_runs", "players",
    )

    def row_counts(self) -> dict[str, int]:
        """How many rows each table holds. What a reset would destroy."""

        counts = {}
        for table in self.RESET_TABLES:
            row = self.connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            counts[table] = int(row["n"]) if row else 0
        return counts

    def reset(self) -> dict[str, int]:
        """Empty every table, returning what was removed.

        Deliberately not exposed anywhere a stray click can reach. The
        collected history rebuilds itself, but a locked slate does not:
        it is a forecast made before its games, and once deleted there
        is no way to make an honest one about a game already played.
        """

        removed = self.row_counts()

        with self._lock:
            for table in self.RESET_TABLES:
                self.connection.execute(f"DELETE FROM {table}")
            self.connection.commit()

        return removed

    def slate(self, slate_id: int) -> dict[str, Any] | None:
        """One slate's sport, site and date, by id."""

        row = self.connection.execute(
            "SELECT id, sport, site, slate_date, name, lock_time FROM slates WHERE id = ?",
            (slate_id,),
        ).fetchone()

        return dict(row) if row else None

    def set_slate_date(self, slate_id: int, slate_date: str) -> None:
        """Move a slate to the date its games are actually played.

        Needed because one of the two exports does not carry a date.
        FanDuel's file says nothing about when the games are, so the
        date has to be supplied, and a wrong one is not cosmetic:
        results are matched to a slate by date, so a slate dated a day
        its games were not played can never be scored. It would sit on
        the calibration record for ever, unresolvable.

        Refuses rather than merges when another slate already occupies
        the target, since silently folding two pools together would
        destroy one of them.
        """

        current = self.slate(slate_id)

        if current is None:
            raise ValueError(f"No slate with id {slate_id}.")

        if current["slate_date"] == slate_date:
            return

        clash = self.find_slate(
            current["sport"], current["site"], slate_date, current["name"]
        )

        if clash is not None and clash != slate_id:
            raise ValueError(
                f"A {current['sport']}:{current['site']} slate already exists "
                f"for {slate_date}. Delete or re-upload that one instead."
            )

        with self._lock:
            self.connection.execute(
                "UPDATE slates SET slate_date = ? WHERE id = ?",
                (slate_date, slate_id),
            )
            self.connection.commit()

    def slates_with_players(self, limit: int = 60) -> list[dict[str, Any]]:
        """Stored slates that actually have a pool, newest first.

        The board offers these as somewhere to return to. A slate with
        no salaries is a row created and then abandoned -- it has
        nothing to show, so it is not offered.
        """

        rows = self.connection.execute(
            """
            SELECT sl.id, sl.sport, sl.site, sl.slate_date, sl.name,
                   COUNT(s.player_id) AS players
            FROM slates sl
            JOIN salaries s ON s.slate_id = sl.id
            GROUP BY sl.id, sl.sport, sl.site, sl.slate_date, sl.name
            ORDER BY sl.slate_date DESC, sl.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        return [dict(row) for row in rows]

    def player_pool(self, slate_id: int) -> list[dict[str, Any]]:
        """The salary pool for a slate, joined to any stored projection."""

        rows = self.connection.execute(
            """
            SELECT
                s.player_id, s.salary, s.roster_positions, s.team, s.opponent,
                s.home, s.game_id, s.site_avg_points, s.injury_status,
                s.starting, s.batting_order,
                p.name, p.positions,
                -- The site's own player ids. Needed to export a lineup
                -- back for bulk upload: both sites match on their id,
                -- not on a name.
                p.dk_id, p.fd_id,
                pr.projected_points, pr.floor, pr.ceiling, pr.stdev,
                pr.projected_ownership, pr.projected_opportunity
            FROM salaries s
            JOIN players p ON p.player_id = s.player_id
            LEFT JOIN projections pr ON pr.slate_id = s.slate_id AND pr.player_id = s.player_id
            WHERE s.slate_id = ?
            ORDER BY s.salary DESC
            """,
            (slate_id,),
        ).fetchall()

        pool = []
        for row in rows:
            record = dict(row)
            record["positions"] = json.loads(record["positions"] or "[]")
            record["roster_positions"] = json.loads(record["roster_positions"] or "[]")
            record["home"] = bool(record["home"])
            pool.append(record)
        return pool

    # ------------------------------------------------------------------
    # Game logs
    # ------------------------------------------------------------------

    def save_game_log(self, log: Mapping[str, Any]) -> None:
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO game_logs (
                    player_id, sport, game_date, team, opponent, home, opportunity, stats
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (player_id, game_date) DO UPDATE SET
                    opportunity = excluded.opportunity,
                    stats = excluded.stats
                """,
                (
                    log["player_id"],
                    log["sport"].upper(),
                    log["game_date"],
                    log.get("team"),
                    log.get("opponent"),
                    1 if log.get("home") else 0,
                    log.get("opportunity"),
                    json.dumps(dict(log.get("stats", {}))),
                ),
            )
            self.connection.commit()


    def save_game_logs(self, logs: Iterable[Mapping[str, Any]]) -> int:
        """Write many game logs in a single transaction.

        `save_game_log` commits per row, which is correct for the one-off
        call and ruinous for a collector: loading three NBA seasons meant
        105,000 separate transactions and took ninety-five seconds,
        almost all of it fsync rather than work. Batching the same rows
        into one transaction turns a refresh from something you notice
        into something you do not.
        """

        rows = [
            (
                log["player_id"],
                log["sport"].upper(),
                log["game_date"],
                log.get("team"),
                log.get("opponent"),
                1 if log.get("home") else 0,
                log.get("opportunity"),
                json.dumps(dict(log.get("stats", {}))),
            )
            for log in logs
        ]

        if not rows:
            return 0

        with self._lock:
            self.connection.executemany(
                """
                INSERT INTO game_logs (
                    player_id, sport, game_date, team, opponent, home, opportunity, stats
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (player_id, game_date) DO UPDATE SET
                    opportunity = excluded.opportunity,
                    stats = excluded.stats
                """,
                rows,
            )
            self.connection.commit()

        return len(rows)

    def upsert_players(self, players: Iterable[Mapping[str, Any]]) -> int:
        """Write many players in a single transaction."""

        rows = [
            (
                player["player_id"],
                player["name"],
                player["sport"].upper(),
                player.get("team"),
                json.dumps(list(player.get("positions", []))),
                player.get("dk_id"),
                player.get("fd_id"),
            )
            for player in players
        ]

        if not rows:
            return 0

        with self._lock:
            self.connection.executemany(
                """
                INSERT INTO players (player_id, name, sport, team, positions, dk_id, fd_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT (player_id) DO UPDATE SET
                    name = excluded.name,
                    team = COALESCE(excluded.team, players.team),
                    positions = COALESCE(excluded.positions, players.positions),
                    updated_at = CURRENT_TIMESTAMP
                """,
                rows,
            )
            self.connection.commit()

        return len(rows)

    def latest_game_date(self, sport: str) -> str | None:
        """The most recent game already stored for a sport.

        Lets a collector skip everything it already has, which is what
        makes an in-season refresh cheap enough to run on app start.
        """

        row = self.connection.execute(
            "SELECT MAX(game_date) AS latest FROM game_logs WHERE sport = ?",
            (sport.upper(),),
        ).fetchone()
        return row["latest"] if row and row["latest"] else None

    def record_collector_run(self, sport: str, logs: int, seasons: str) -> None:
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO collector_runs (sport, logs, seasons)
                VALUES (?, ?, ?)
                """,
                (sport.upper(), logs, seasons),
            )
            self.connection.commit()

    def hours_since_collection(self, sport: str) -> float | None:
        """Hours since this sport's history was last refreshed.

        None when it has never run. Drives the staleness check: history
        that loaded once and never again silently decays through a
        season, because the projections keep using games that stop being
        recent.
        """

        row = self.connection.execute(
            f"""
            SELECT {self.dialect.hours_since("ran_at")} AS hours
            FROM collector_runs WHERE sport = ?
            """,
            (sport.upper(),),
        ).fetchone()

        return float(row["hours"]) if row and row["hours"] is not None else None

    def game_log_count(self, sport: str, game_date: str) -> int:
        """How many logs are stored for one sport on one date.

        Asked before scoring a slate. Resolution records a player with
        no log as having scored zero, which is right when he was a
        healthy scratch and catastrophic when the box scores simply are
        not published yet: the whole slate lands on the calibration
        record as a day nobody played, and because it now has results it
        is never revisited.
        """

        row = self.connection.execute(
            "SELECT COUNT(*) AS logs FROM game_logs WHERE sport = ? AND game_date = ?",
            (sport.upper(), game_date),
        ).fetchone()

        return int(row["logs"]) if row else 0

    def slates_awaiting_resolution(self) -> list[dict[str, Any]]:
        """Locked slates whose games have been played but not yet scored.

        A locked slate is a forecast on the record, and it is worth
        nothing until it is scored against what happened. Leaving that
        to a button means the calibration table fills only on the days
        somebody remembers to press it, which is exactly the pattern
        that produces a record too sparse to read.

        Ordered oldest first, so a backlog is worked in the order it
        accumulated.
        """

        rows = self.connection.execute(
            """
            SELECT sl.id, sl.sport, sl.site, sl.slate_date
            FROM slates sl
            WHERE EXISTS (
                SELECT 1 FROM projections p
                WHERE p.slate_id = sl.id AND p.locked_at IS NOT NULL
            )
            AND NOT EXISTS (
                SELECT 1 FROM actuals a WHERE a.slate_id = sl.id
            )
            ORDER BY sl.slate_date ASC, sl.id ASC
            """
        ).fetchall()

        return [dict(row) for row in rows]

    def game_logs(self, player_id: str, before: str | None = None, limit: int = 40) -> list[dict[str, Any]]:
        """Most recent logs first, optionally restricted to before a date.

        `before` exists so a backtest cannot see the game it is
        predicting. Every projection call made during evaluation passes
        the slate date here.
        """

        if before:
            rows = self.connection.execute(
                """
                SELECT * FROM game_logs
                WHERE player_id = ? AND game_date < ?
                ORDER BY game_date DESC LIMIT ?
                """,
                (player_id, before, limit),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM game_logs WHERE player_id = ? ORDER BY game_date DESC LIMIT ?",
                (player_id, limit),
            ).fetchall()

        logs = []
        for row in rows:
            record = dict(row)
            record["stats"] = json.loads(record["stats"] or "{}")
            record["home"] = bool(record["home"])
            logs.append(record)
        return logs

    # SQLite's default limit on bound parameters is 999, so an IN list
    # of a whole MLB pool has to be split whatever the server allows.
    ID_BATCH = 500

    def game_logs_for(
        self, player_ids, before: str | None = None, limit: int = 40
    ) -> dict[str, list[dict[str, Any]]]:
        """Recent logs for many players at once, keyed by player id.

        One query per batch rather than one per player. That distinction
        does not show up locally -- 942 queries against a file take
        under a second -- and dominates everything against a hosted
        database, where each one is a network round trip: a real MLB
        pool cost 942 of them, twenty to forty seconds of waiting, on
        every rerun of the script.

        The per-player limit is applied in the database with a window
        function rather than by fetching everything and slicing, because
        the point is to move less data, not more.
        """

        ids = [str(player_id) for player_id in player_ids]
        logs: dict[str, list[dict[str, Any]]] = {player_id: [] for player_id in ids}

        for start in range(0, len(ids), self.ID_BATCH):
            batch = ids[start:start + self.ID_BATCH]
            placeholders = ", ".join("?" for _ in batch)
            cutoff = "AND game_date < ?" if before else ""
            parameters = [*batch, *([before] if before else []), limit]

            rows = self.connection.execute(
                f"""
                SELECT * FROM (
                    SELECT g.*, ROW_NUMBER() OVER (
                        PARTITION BY player_id ORDER BY game_date DESC
                    ) AS row_number_in_player
                    FROM game_logs g
                    WHERE player_id IN ({placeholders}) {cutoff}
                ) ranked
                WHERE row_number_in_player <= ?
                ORDER BY player_id, game_date DESC
                """,
                tuple(parameters),
            ).fetchall()

            for row in rows:
                record = dict(row)
                record.pop("row_number_in_player", None)
                record["stats"] = json.loads(record["stats"] or "{}")
                record["home"] = bool(record["home"])
                logs[str(record["player_id"])].append(record)

        return logs

    def logs_by_player_prefix(
        self, prefix: str, before: str | None = None, limit: int = 2000
    ) -> list[dict[str, Any]]:
        """Every log whose player id starts with `prefix`.

        Team defences are keyed `nfl:dst:<team>`, and projecting one
        needs the whole set rather than one player's rows: how generous
        an offence has been is measured across every defence that has
        faced it.
        """

        if before:
            rows = self.connection.execute(
                """
                SELECT * FROM game_logs
                WHERE player_id LIKE ? AND game_date < ?
                ORDER BY game_date DESC LIMIT ?
                """,
                (f"{prefix}%", before, limit),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM game_logs WHERE player_id LIKE ? ORDER BY game_date DESC LIMIT ?",
                (f"{prefix}%", limit),
            ).fetchall()

        logs = []
        for row in rows:
            record = dict(row)
            record["stats"] = json.loads(record["stats"] or "{}")
            record["home"] = bool(record["home"])
            logs.append(record)
        return logs

    # ------------------------------------------------------------------
    # Projections, actuals, lineups, briefs
    # ------------------------------------------------------------------

    def save_projection(self, slate_id: int, projection: Mapping[str, Any]) -> None:
        """One projection. `save_projections` writes a whole slate."""

        self.save_projections(slate_id, [projection])


    _PROJECTION_SQL = """
        INSERT INTO projections (
            slate_id, player_id, projected_points, floor, ceiling, stdev,
            projected_opportunity, projected_ownership, model
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (slate_id, player_id, model) DO UPDATE SET
            projected_points = excluded.projected_points,
            floor = excluded.floor,
            ceiling = excluded.ceiling,
            stdev = excluded.stdev,
            projected_opportunity = excluded.projected_opportunity,
            projected_ownership = excluded.projected_ownership
    """

    @staticmethod
    def _projection_row(slate_id: int, projection: Mapping[str, Any]) -> tuple:
        return (
            slate_id,
            projection["player_id"],
            float(projection["projected_points"]),
            projection.get("floor"),
            projection.get("ceiling"),
            projection.get("stdev"),
            projection.get("projected_opportunity"),
            projection.get("projected_ownership"),
            projection.get("model", "baseline"),
        )

    def save_projections(self, slate_id: int, projections) -> int:
        """Write a whole slate's projections in one statement.

        Saving them one at a time meant a round trip and a commit per
        player. On a file that is invisible; against a hosted database
        a full MLB pool spent it 942 times over, on every rerun.
        """

        rows = [self._projection_row(slate_id, p) for p in projections]

        if not rows:
            return 0

        with self._lock:
            self.connection.executemany(self._ddl(self._PROJECTION_SQL), rows)
            self.connection.commit()

        return len(rows)

    def lock_slate(self, slate_id: int) -> int:
        """Stamp every projection for a slate as locked.

        After this the calibration harness will score them. Projections
        written later are still stored but carry no `locked_at`, and the
        scorer ignores them -- an unlocked projection is a draft, not a
        forecast on the record.
        """
        with self._lock:

            cursor = self.connection.execute(
                """
                UPDATE projections SET locked_at = CURRENT_TIMESTAMP
                WHERE slate_id = ? AND locked_at IS NULL
                """,
                (slate_id,),
            )
            self.connection.commit()
            return cursor.rowcount


    def save_actual(self, slate_id: int, actual: Mapping[str, Any]) -> None:
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO actuals (
                    slate_id, player_id, actual_points, actual_opportunity, actual_ownership, stats
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (slate_id, player_id) DO UPDATE SET
                    actual_points = excluded.actual_points,
                    actual_opportunity = excluded.actual_opportunity,
                    actual_ownership = excluded.actual_ownership,
                    stats = excluded.stats,
                    resolved_at = CURRENT_TIMESTAMP
                """,
                (
                    slate_id,
                    actual["player_id"],
                    actual.get("actual_points"),
                    actual.get("actual_opportunity"),
                    actual.get("actual_ownership"),
                    json.dumps(dict(actual.get("stats", {}))),
                ),
            )
            self.connection.commit()


    def resolved_projections(self, sport: str | None = None) -> list[dict[str, Any]]:
        """Locked projections that have an actual result, for scoring."""

        query = """
            SELECT
                sl.sport, sl.site, sl.slate_date, pr.model,
                pr.player_id, p.name, p.positions,
                pr.projected_points, pr.floor, pr.ceiling, pr.stdev,
                pr.projected_ownership,
                a.actual_points, a.actual_ownership,
                pr.created_at, sl.slate_date AS played_on,
                -- A projection written before the games is a forecast;
                -- one reconstructed afterwards is a backtest. Both are
                -- legitimate and they mean different things, so they are
                -- flagged here and reported apart rather than averaged
                -- into a single number that flatters the weaker one.
                CASE WHEN substr(pr.created_at, 1, 10) <= sl.slate_date THEN 1 ELSE 0 END AS forward,
                s.salary,
                -- The season average the site prints beside each player.
                -- This is the baseline skill is measured against, so
                -- omitting it silently disables the headline metric.
                s.site_avg_points
            FROM projections pr
            JOIN slates sl ON sl.id = pr.slate_id
            JOIN actuals a ON a.slate_id = pr.slate_id AND a.player_id = pr.player_id
            JOIN players p ON p.player_id = pr.player_id
            LEFT JOIN salaries s ON s.slate_id = pr.slate_id AND s.player_id = pr.player_id
            WHERE pr.locked_at IS NOT NULL AND a.actual_points IS NOT NULL
        """
        params: tuple[Any, ...] = ()
        if sport:
            query += " AND sl.sport = ?"
            params = (sport.upper(),)

        rows = self.connection.execute(query, params).fetchall()
        results = []
        for row in rows:
            record = dict(row)
            record["positions"] = json.loads(record["positions"] or "[]")
            results.append(record)
        return results

    def save_lineup(self, slate_id: int, lineup: Mapping[str, Any]) -> int:
        with self._lock:
            # The one place the drivers cannot be papered over: SQLite
            # reports the generated id afterwards, PostgreSQL requires
            # asking for it in the statement.
            lineup_id = self.connection.insert_returning_id(
                """
                INSERT INTO lineups (
                    slate_id, mode, total_salary, total_projection,
                    total_ceiling, total_ownership, players
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    slate_id,
                    lineup.get("mode", "cash"),
                    lineup.get("total_salary"),
                    lineup.get("total_projection"),
                    lineup.get("total_ceiling"),
                    lineup.get("total_ownership"),
                    json.dumps(lineup["players"]),
                ),
            )
            self.connection.commit()
            return lineup_id


    def lineups(self, slate_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM lineups WHERE slate_id = ? ORDER BY id",
            (slate_id,),
        ).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            record["players"] = json.loads(record["players"])
            out.append(record)
        return out

    def save_brief(self, slate_id: int, brief: Mapping[str, Any]) -> None:
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO player_briefs (
                    slate_id, player_id, status, situation, role_change,
                    what_would_settle_it, multiplier, estimated, dispersion,
                    sample_count, model
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    slate_id,
                    brief["player_id"],
                    brief.get("status"),
                    brief.get("situation"),
                    brief.get("role_change"),
                    brief.get("what_would_settle_it"),
                    brief.get("multiplier"),
                    1 if brief.get("multiplier") is not None else 0,
                    brief.get("dispersion"),
                    brief.get("sample_count"),
                    brief.get("model"),
                ),
            )
            self.connection.commit()


    def briefs(self, slate_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT b.*, p.name FROM player_briefs b
            JOIN players p ON p.player_id = b.player_id
            WHERE b.slate_id = ? ORDER BY b.created_at DESC
            """,
            (slate_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        self.connection.close()
