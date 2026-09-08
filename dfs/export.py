"""Write built lineups in the bulk-upload format each site accepts.

Both DraftKings and FanDuel let you upload lineups as a CSV rather than
clicking players one at a time, and both match on their own player id
rather than on a name — two players called Will Smith exist, and a name
match would silently enter the wrong one.

This is not scraping and does not touch either site: it produces a file
you upload through the interface they provide for exactly this. The
salary export comes out, the lineup file goes back in.

One caveat worth reading before a big multi-entry upload. The exact
column set differs by contest type and both sites have changed it
between seasons, so download the site's own template from the upload
screen and compare headers. What never changes is the ids in the cells,
which is the part that would be laborious to produce by hand.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence

from dfs.optimizer.lineup import Lineup
from dfs.sports import SportConfig


class ExportError(RuntimeError):
    """Raised when a lineup cannot be written in a site's format."""


def slot_columns(config: SportConfig) -> list[str]:
    """The upload file's column headers: one per roster spot, in order.

    Repeated deliberately. A DraftKings MLB file has two columns headed
    "P" and three headed "OF", which is what the site expects and what a
    plain list of headers produces.
    """

    columns: list[str] = []
    for slot in config.roster:
        columns.extend([slot.name] * slot.count)
    return columns


def _identifier(player, site: str) -> str:
    """The value the site matches a roster spot on."""

    site_id = player.dk_id if site == "DK" else player.fd_id

    if site_id:
        return str(site_id)

    # Without an id the upload cannot be trusted to land on the right
    # person, so this fails loudly rather than writing a name and
    # letting the site guess.
    raise ExportError(
        f"{player.name} has no {site} player id, so this lineup cannot be "
        f"exported. Re-upload the salary file from the {site} contest screen: "
        "the id column is what the site matches on."
    )


def _ordered_players(lineup: Lineup, config: SportConfig) -> list:
    """Lineup players arranged to match the upload columns.

    Order matters: the site reads each cell as filling the slot its
    column names, so a lineup written out of order fills the wrong
    positions or is rejected.
    """

    remaining = list(lineup.players)
    ordered = []

    for column in slot_columns(config):
        for index, player in enumerate(remaining):
            if player.slot == column:
                ordered.append(remaining.pop(index))
                break
        else:
            raise ExportError(
                f"No player found for the {column} slot. The lineup does not "
                "match this sport's roster, which usually means it was built "
                "for a different site."
            )

    return ordered


def to_upload_csv(lineups: Sequence[Lineup], config: SportConfig) -> str:
    """Every lineup as one row of site player ids, ready to upload."""

    if not lineups:
        raise ExportError("There are no lineups to export.")

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(slot_columns(config))

    for lineup in lineups:
        writer.writerow(
            [_identifier(player, config.site) for player in _ordered_players(lineup, config)]
        )

    return buffer.getvalue()


def to_readable_csv(lineups: Sequence[Lineup], config: SportConfig) -> str:
    """The same lineups as something a person can read and check.

    The upload file is columns of bare numbers, which is right for the
    site and useless for spotting that you have rostered a pitcher
    against your own stack. This is the one to look at before uploading.
    """

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["Lineup", "Slot", "Player", "Team", "Opponent", "Salary",
         "Projection", "Ceiling", "Ownership", f"{config.site} ID"]
    )

    for number, lineup in enumerate(lineups, start=1):
        for player in _ordered_players(lineup, config):
            writer.writerow([
                number, player.slot, player.name, player.team, player.opponent,
                player.salary, player.projection, player.ceiling, player.ownership,
                player.dk_id if config.site == "DK" else player.fd_id,
            ])
        writer.writerow([
            number, "TOTAL", "", "", "", lineup.total_salary,
            round(lineup.total_projection, 2), round(lineup.total_ceiling, 2),
            round(lineup.total_ownership, 1), "",
        ])

    return buffer.getvalue()


def upload_filename(config: SportConfig, slate_date: str) -> str:
    site = "draftkings" if config.site == "DK" else "fanduel"
    return f"{site}-{config.sport.lower()}-{slate_date}-upload.csv"


def readable_filename(config: SportConfig, slate_date: str) -> str:
    return f"{config.sport.lower()}-{slate_date}-lineups.csv"
