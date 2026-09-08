"""Opening a database, whichever backend the URL names.

The rest of the project calls `connection.execute(sql, params)` and
reads rows by column name, and neither has to know which engine is
underneath. Two small pieces make that true: placeholders are rewritten
on the way to the driver, and PostgreSQL rows are wrapped in a type that
behaves like `sqlite3.Row` — readable by name *and* by position.

That second piece is what kept this change small. psycopg's own row
factories offer one or the other, so adopting either would have meant
rewriting every query result site in the codebase and the tests.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dfs.db.dialect import POSTGRES, SQLITE, Dialect, translate


class Row(Mapping):
    """A result row readable by column name or by position.

    `sqlite3.Row` supports both, and the project relies on it in both
    directions: `row["actual_points"]` throughout the code, and
    `fetchone()[0]` for scalar counts. This gives PostgreSQL the same
    contract so neither style has to change.
    """

    __slots__ = ("_columns", "_values", "_index")

    def __init__(self, columns: Sequence[str], values: Sequence[Any]) -> None:
        self._columns = columns
        self._values = values
        self._index = {name: position for position, name in enumerate(columns)}

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._values[key]
        try:
            return self._values[self._index[key]]
        except KeyError as error:
            raise KeyError(f"No column {key!r} in row ({', '.join(self._columns)})") from error

    def __iter__(self):
        return iter(self._columns)

    def __len__(self) -> int:
        return len(self._columns)

    def keys(self) -> Sequence[str]:
        return self._columns

    def __repr__(self) -> str:
        pairs = ", ".join(f"{name}={value!r}" for name, value in zip(self._columns, self._values))
        return f"Row({pairs})"


def _postgres_row_factory(cursor):
    columns = [column.name for column in (cursor.description or [])]

    def make_row(values):
        return Row(columns, values)

    return make_row


class Connection:
    """A driver connection with placeholders translated for its dialect.

    Deliberately thin. It exposes only what the storage layer already
    used — execute, executemany, commit, close — plus one helper for
    inserts that need the generated id, which is the single place the two
    drivers cannot be papered over: SQLite reports `lastrowid` after the
    fact, PostgreSQL requires asking for it in the statement.
    """

    def __init__(self, raw: Any, dialect: Dialect, reopen=None) -> None:
        self.raw = raw
        self.dialect = dialect
        # How to obtain a fresh driver connection. Held because a hosted
        # PostgreSQL connection does not last: Neon and its like suspend
        # an idle database and every open connection dies with it, and
        # the app holds one across reruns for as long as the container
        # lives. Without this, the first interaction after a few idle
        # minutes fails, and so does every one after it -- the dead
        # connection is cached, so the app never recovers on its own.
        self._reopen = reopen
        # Writes are serialised by the storage layer's lock; this guards
        # the cursor churn that psycopg needs for executemany.
        self._lock = threading.RLock()

    @property
    def closed(self) -> bool:
        """Whether the driver already knows the connection is unusable."""

        return bool(getattr(self.raw, "closed", False))

    def _ensure_open(self) -> None:
        """Reopen before use if the connection is known to be dead.

        Checked rather than caught, because a connection the driver has
        already marked closed has sent nothing: reopening here cannot
        replay a statement, which makes it safe for writes as well as
        reads.
        """

        if self._reopen is not None and self.closed:
            self.raw = self._reopen()

    def _retry_after_reconnect(self, error: Exception) -> bool:
        """Whether `error` is a dead connection worth reopening for.

        Narrow on purpose. A statement that failed for its own reasons
        must surface, and a write that may have reached the server must
        not be sent twice -- so this asks the driver whether the
        connection itself is gone rather than reading the message.
        """

        if self._reopen is None or not self.closed:
            return False

        self.raw = self._reopen()
        return True

    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()):
        statement = translate(sql, self.dialect)
        self._ensure_open()

        try:
            return self._execute(statement, params)
        except Exception as error:
            if not self._retry_after_reconnect(error):
                raise
            return self._execute(statement, params)

    def _execute(self, statement: str, params):
        if self.dialect.name == "sqlite":
            return self.raw.execute(statement, params)

        cursor = self.raw.cursor()
        cursor.execute(statement, tuple(params))
        return cursor

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        statement = translate(sql, self.dialect)
        # Materialised because a retry has to send the same rows again,
        # and a generator would already be spent.
        batch = [tuple(row) for row in rows]
        self._ensure_open()

        try:
            self._executemany(statement, batch)
        except Exception as error:
            if not self._retry_after_reconnect(error):
                raise
            self._executemany(statement, batch)

    def _executemany(self, statement: str, batch) -> None:
        if self.dialect.name == "sqlite":
            self.raw.executemany(statement, batch)
            return

        with self._lock:
            cursor = self.raw.cursor()
            cursor.executemany(statement, batch)

    def insert_returning_id(self, sql: str, params: Sequence[Any]) -> int:
        """Insert one row and return its generated id."""

        if self.dialect.name == "sqlite":
            cursor = self.execute(sql, params)
            return int(cursor.lastrowid)

        cursor = self.execute(f"{sql.rstrip().rstrip(';')} RETURNING id", params)
        return int(cursor.fetchone()[0])

    def commit(self) -> None:
        # A commit on a connection that has just been reopened has
        # nothing to commit, and raising here would turn a recovered
        # read into an error.
        if self.closed:
            return
        self.raw.commit()

    def close(self) -> None:
        self.raw.close()


def is_postgres_url(url: str) -> bool:
    return str(url).startswith(("postgres://", "postgresql://"))


def connect(url: str | Path) -> Connection:
    """Open a connection from a path or a database URL.

    A bare path or a `sqlite://` URL opens SQLite; a `postgres://` or
    `postgresql://` URL opens PostgreSQL. Accepting a plain path keeps
    every existing caller and test working untouched.
    """

    target = str(url)

    if is_postgres_url(target):
        try:
            import psycopg
        except ImportError as error:  # pragma: no cover - depends on install
            raise RuntimeError(
                "A postgres:// URL needs the psycopg driver, which is not "
                "installed. Run `pip install -r requirements.txt` from the "
                "dfs-edge directory, or `pip install 'psycopg[binary]'` for "
                "the driver alone."
            ) from error

        def open_postgres():
            return psycopg.connect(
                target, row_factory=_postgres_row_factory, autocommit=False
            )

        return Connection(open_postgres(), POSTGRES, reopen=open_postgres)

    if target.startswith("sqlite://"):
        parsed = urlparse(target)
        target = parsed.path or ":memory:"
        # sqlite:///relative/path leaves a leading slash on a relative path.
        if target.startswith("/") and not Path(target).is_absolute():
            target = target[1:]

    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)

    raw = sqlite3.connect(target, check_same_thread=False)
    raw.row_factory = sqlite3.Row
    connection = Connection(raw, SQLITE)

    if SQLITE.needs_foreign_key_pragma:
        connection.execute("PRAGMA foreign_keys = ON")

    return connection
