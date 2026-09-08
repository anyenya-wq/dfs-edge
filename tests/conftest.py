"""Shared fixtures, and the switch that runs the suite on either backend.

By default every test uses a throwaway SQLite file: no server, no setup,
and the suite stays around twenty seconds. Setting DFS_TEST_DATABASE_URL
to a PostgreSQL URL points the same tests at a real server instead,
which is how the Postgres backend is verified — by the tests that
already exist rather than by a separate set written to flatter it.

    DFS_TEST_DATABASE_URL=postgresql://... pytest

Each test gets a clean schema. On SQLite that is a fresh file; on
PostgreSQL the tables are dropped and recreated, since one server is
shared across the run.
"""

from __future__ import annotations

import os

import pytest

from dfs.db.connection import is_postgres_url
from dfs.db.database import Database

TEST_URL_VARIABLE = "DFS_TEST_DATABASE_URL"

# Dropped in dependency order, children before parents, so the foreign
# keys PostgreSQL actually enforces do not block the reset.
_TABLES = (
    "player_briefs",
    "lineups",
    "actuals",
    "projections",
    "salaries",
    "game_logs",
    "collector_runs",
    "slates",
    "players",
)


def _reset(database: Database) -> None:
    for table in _TABLES:
        database.connection.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    database.connection.commit()
    database.create_tables()


@pytest.fixture
def database(tmp_path):
    """A clean database, on whichever backend is configured."""

    url = os.environ.get(TEST_URL_VARIABLE)

    if url and is_postgres_url(url):
        instance = Database(url)
        _reset(instance)
        yield instance
        instance.close()
        return

    yield Database(tmp_path / "test.db")


@pytest.fixture
def database_url(tmp_path):
    """The URL a test should hand to code that opens its own connection."""

    return os.environ.get(TEST_URL_VARIABLE) or str(tmp_path / "test.db")
