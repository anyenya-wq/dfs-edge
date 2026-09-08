"""The backend abstraction: placeholders, row access, and URL routing.

These run on SQLite so they always run. Whether the PostgreSQL side
actually works is established by pointing the whole existing suite at a
real server via DFS_TEST_DATABASE_URL, not by a parallel set of tests
written to agree with the implementation.
"""

from __future__ import annotations

import os

import pytest

from dfs.db.connection import Connection, Row, connect, is_postgres_url
from dfs.db.database import URL_ENVIRONMENT_VARIABLE, Database
from dfs.db.dialect import POSTGRES, SQLITE, translate


# --- placeholder translation -----------------------------------------

def test_sqlite_keeps_question_marks():
    assert translate("SELECT * FROM t WHERE a = ?", SQLITE) == "SELECT * FROM t WHERE a = ?"


def test_postgres_gets_percent_s():
    assert translate("SELECT * FROM t WHERE a = ?", POSTGRES) == "SELECT * FROM t WHERE a = %s"


def test_every_placeholder_is_translated():
    assert translate("VALUES (?, ?, ?)", POSTGRES) == "VALUES (%s, %s, %s)"


def test_a_question_mark_inside_a_literal_is_left_alone():
    """A naive replace would corrupt a LIKE pattern or a text default."""

    sql = "SELECT * FROM t WHERE note LIKE 'who? me' AND a = ?"
    assert translate(sql, POSTGRES) == "SELECT * FROM t WHERE note LIKE 'who? me' AND a = %s"


def test_an_escaped_quote_does_not_end_the_literal():
    sql = "SELECT 'it''s a ?' AS q, x FROM t WHERE y = ?"
    translated = translate(sql, POSTGRES)
    assert "'it''s a ?'" in translated
    assert translated.endswith("y = %s")


# --- row access -------------------------------------------------------

def test_a_row_reads_by_name_and_by_position():
    """Both styles are used across the project, so both must work."""

    row = Row(["id", "name"], [7, "alice"])
    assert row["name"] == "alice"
    assert row[0] == 7


def test_a_row_converts_to_a_dict():
    assert dict(Row(["a", "b"], [1, 2])) == {"a": 1, "b": 2}


def test_a_row_is_truthy_when_it_has_columns():
    assert Row(["a"], [None])
    assert not Row([], [])


def test_an_unknown_column_names_what_was_available():
    with pytest.raises(KeyError, match="id, name"):
        Row(["id", "name"], [1, "x"])["nope"]


# --- URL routing ------------------------------------------------------

def test_postgres_urls_are_recognised():
    assert is_postgres_url("postgresql://user@host/db")
    assert is_postgres_url("postgres://user@host/db")


def test_other_targets_are_not_postgres():
    assert not is_postgres_url("data/dfs.db")
    assert not is_postgres_url("sqlite:///data/dfs.db")
    assert not is_postgres_url(":memory:")


def test_a_bare_path_opens_sqlite(tmp_path):
    connection = connect(tmp_path / "x.db")
    assert connection.dialect is SQLITE
    connection.close()


def test_a_sqlite_url_opens_sqlite(tmp_path):
    connection = connect(f"sqlite:///{tmp_path / 'y.db'}")
    assert connection.dialect is SQLITE
    connection.close()


def test_an_in_memory_database_opens():
    connection = connect(":memory:")
    connection.execute("CREATE TABLE t (a INTEGER)")
    connection.close()


def test_a_missing_parent_directory_is_created(tmp_path):
    connect(tmp_path / "nested" / "deep" / "z.db").close()
    assert (tmp_path / "nested" / "deep").is_dir()


# --- the environment switch -------------------------------------------

def test_an_explicit_target_wins_over_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(URL_ENVIRONMENT_VARIABLE, "postgresql://nowhere/db")
    database = Database(tmp_path / "explicit.db")
    assert database.dialect is SQLITE
    database.close()


def test_the_environment_supplies_the_default(tmp_path, monkeypatch):
    """How a hosted deployment points at PostgreSQL with no code change."""

    target = tmp_path / "from-env.db"
    monkeypatch.setenv(URL_ENVIRONMENT_VARIABLE, str(target))
    database = Database()
    assert database.path == target
    database.close()
    assert target.exists()


# --- schema spelling --------------------------------------------------

def test_the_identity_column_is_spelled_per_backend(tmp_path):
    database = Database(tmp_path / "d.db")
    sqlite_ddl = database._ddl("id INTEGER PRIMARY KEY AUTOINCREMENT, x REAL")
    assert "AUTOINCREMENT" in sqlite_ddl
    assert "REAL" in sqlite_ddl
    database.close()


def test_postgres_spellings_differ_from_sqlite():
    assert POSTGRES.identity_primary_key != SQLITE.identity_primary_key
    assert POSTGRES.real == "DOUBLE PRECISION"
    assert "julianday" in SQLITE.hours_since("ran_at")
    assert "EXTRACT" in POSTGRES.hours_since("ran_at")


def test_the_timestamp_expression_names_the_column():
    assert "ran_at" in SQLITE.hours_since("ran_at")
    assert "ran_at" in POSTGRES.hours_since("ran_at")


# --- the live backend, when one is configured -------------------------

needs_postgres = pytest.mark.skipif(
    not os.environ.get("DFS_TEST_DATABASE_URL", "").startswith(("postgres://", "postgresql://")),
    reason="set DFS_TEST_DATABASE_URL to a postgres:// URL to test that backend",
)


@needs_postgres
def test_the_configured_backend_is_postgres(database):
    """When the suite is pointed at PostgreSQL, it really is running there.

    Guards against the whole run silently falling back to SQLite and
    reporting a green Postgres verification that never happened.
    """

    assert database.dialect is POSTGRES
    assert database.path is None


@needs_postgres
def test_a_generated_id_comes_back_from_an_insert(database):
    slate_id = database.upsert_slate("NBA", "DK", "2026-01-15")
    first = database.save_lineup(slate_id, {"players": [], "total_salary": 1})
    second = database.save_lineup(slate_id, {"players": [], "total_salary": 2})
    assert second > first
