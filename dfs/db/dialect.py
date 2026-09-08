"""The handful of ways SQLite and PostgreSQL differ for this schema.

Both backends are supported rather than one replacing the other, and the
split is deliberate. SQLite needs no server, which keeps the test suite
at twenty seconds and lets a Codespace work with nothing to configure.
PostgreSQL survives a restart, which is what a hosted deployment needs:
Streamlit Cloud's filesystem is ephemeral, so a slate locked before
Sunday's games would not exist on Monday.

Keeping both honest means the differences live here rather than being
sprinkled through the queries. There are only five that matter, and the
schema was already close enough to portable that the rest is shared
verbatim.
"""

from __future__ import annotations

import dataclasses
import re


@dataclasses.dataclass(frozen=True)
class Dialect:
    name: str

    # SQLite takes `?`, PostgreSQL takes `%s`. Queries are written with
    # `?` throughout and translated on the way to the driver.
    placeholder: str

    # An auto-incrementing primary key. SQLite's AUTOINCREMENT keyword is
    # only legal on INTEGER PRIMARY KEY; PostgreSQL uses an identity
    # column.
    identity_primary_key: str

    # SQLite has no distinct float type and stores REAL; PostgreSQL wants
    # an explicit precision.
    real: str

    # Hours between a stored timestamp and now. SQLite counts in days
    # from a Julian epoch; PostgreSQL subtracts timestamps into an
    # interval and will not subtract a text column at all, so the value
    # is cast first. Timestamps are stored as TEXT on both backends --
    # SQLite writes "2026-09-02 04:14:38" and PostgreSQL writes it with
    # a "+00" offset, and timestamptz accepts either.
    hours_since_template: str

    # Whether the driver enforces foreign keys without being asked.
    # SQLite needs a pragma per connection.
    needs_foreign_key_pragma: bool

    def hours_since(self, column: str) -> str:
        return self.hours_since_template.format(column=column)


SQLITE = Dialect(
    name="sqlite",
    placeholder="?",
    identity_primary_key="INTEGER PRIMARY KEY AUTOINCREMENT",
    real="REAL",
    hours_since_template="(julianday('now') - julianday(MAX({column}))) * 24.0",
    needs_foreign_key_pragma=True,
)

POSTGRES = Dialect(
    name="postgres",
    placeholder="%s",
    identity_primary_key="INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY",
    real="DOUBLE PRECISION",
    hours_since_template="EXTRACT(EPOCH FROM (NOW() - MAX({column})::timestamptz)) / 3600.0",
    needs_foreign_key_pragma=False,
)

# Matches a `?` that is not inside a single-quoted string literal. The
# schema contains no literal question marks today, but a naive replace
# would corrupt one silently the first time somebody adds a LIKE pattern
# or a text default, so the scan skips quoted regions.
_LITERAL_OR_PLACEHOLDER = re.compile(r"'(?:[^']|'')*'|\?")


def translate(sql: str, dialect: Dialect) -> str:
    """Rewrite `?` placeholders for the target driver."""

    if dialect.placeholder == "?":
        return sql

    def replace(match: re.Match[str]) -> str:
        text = match.group(0)
        return dialect.placeholder if text == "?" else text

    return _LITERAL_OR_PLACEHOLDER.sub(replace, sql)
