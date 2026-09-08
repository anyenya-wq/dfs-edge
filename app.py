"""Streamlit slate research board.

Decision support first, lineups second. The pages are ordered the way a
slate is actually worked: read the pool, find where the field is wrong,
check the players whose status is in doubt, then build. A tool that
opens on a lineup encourages trusting it, which is exactly the habit
that loses money -- the lineup is the last step, and it is only as good
as the projections behind it.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import datetime as dt
import hmac
import os
from pathlib import Path

import pandas as pd
import streamlit as st

from dfs.db.connection import is_postgres_url
from dfs.db.database import Database
from dfs.ingest.detect import detect
from dfs.export import (
    ExportError, readable_filename, to_readable_csv, to_upload_csv, upload_filename,
)
from dfs.ingest.stats import (
    INCOMPLETE, CollectorError, PLANNED, has_collector, is_stale, load_history,
    log_counts, refresh_history,
)
from dfs.optimizer.lineup import optimize_lineups
from dfs.optimizer.rules import cash_settings, gpp_settings
from dfs.ownership.leverage import apply_leverage, gpp_score
from dfs.pipeline import (
    build_slate, load_slate, project_slate, resolve_slate_from_logs,
    slate_is_scorable, validate_pool,
)
from dfs.projections.calibration import calibration_report
from dfs.sports import SITES, SPORTS, VERIFICATION_NOTES, get_config

st.set_page_config(page_title="DFS Edge", page_icon="🎯", layout="wide")


def _secret(name: str) -> str | None:
    """A setting from Streamlit's secrets, falling back to the environment.

    Hosted deployments keep these in Streamlit's own store rather than
    the process environment, and the accessor raises rather than
    returning empty when no secrets file exists at all -- which is the
    normal local case.
    """

    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass

    return os.environ.get(name)


def require_password() -> bool:
    """Gate the app behind a shared password, when one is configured.

    Streamlit Community Cloud serves free apps at a public URL. This
    board reads and writes a real database: a locked slate is a forecast
    on the record, and anyone who could open the page could add to it,
    resolve it, or read what has been forecast. If `ANTHROPIC_API_KEY`
    is also set, an open page is an open tab on that account.

    So the page renders nothing until the password matches. Returns
    False when it has not, and the caller renders nothing else.

    No password configured means no gate -- that is the local case,
    where the app is reachable only from the machine running it. The
    trade-off is deliberate: requiring one locally would add a step to
    every run for no gain, and forgetting to set one in a deployment is
    caught by the banner below rather than by silence.
    """

    expected = _secret("APP_PASSWORD")

    if not expected:
        # Said out loud rather than assumed. A deployment that meant to
        # set a password and did not is indistinguishable from a local
        # run unless something says so.
        if _secret("DFS_DATABASE_URL"):
            st.warning(
                "No `APP_PASSWORD` is set, so this page is open to anyone "
                "with the link. Set one in the app's Secrets."
            )
        return True

    if st.session_state.get("authenticated"):
        return True

    st.title("DFS Edge")
    st.caption("This board is private. Enter the password to continue.")

    with st.form("password"):
        attempt = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Enter")

    if submitted:
        # Constant-time: a plain `==` leaks the length of the matching
        # prefix through how long it takes to fail.
        if hmac.compare_digest(attempt, expected):
            st.session_state["authenticated"] = True
            # Not kept anywhere. The session flag is what persists, and
            # it lives only in this browser session.
            st.rerun()
        else:
            st.error("Incorrect password.")

    return False


def database_url() -> str | None:
    """Where to store, checking Streamlit's secrets before the environment.

    A hosted deployment sets DFS_DATABASE_URL in the app's Secrets, which
    live in Streamlit's own store rather than the process environment.
    Reading only os.environ would mean a hosted app silently falling back
    to SQLite -- and on an ephemeral filesystem that quietly destroys the
    calibration record, which is the entire reason for using PostgreSQL.
    Silent is the operative word: nothing would fail, the record would
    just never accumulate.
    """

    return _secret("DFS_DATABASE_URL")


def _code_version() -> float:
    """Newest mtime across the package, used as a cache key.

    `st.cache_resource` holds the Database instance across reruns, which
    is what we want: otherwise every widget click opens a new
    connection, and against a remote PostgreSQL that is a fresh TCP and
    TLS handshake each time.

    But the cache also survives a code change. Streamlit re-runs
    `app.py` when the file changes and does not re-import the modules
    it imported earlier, so after a `git pull` the new app code met a
    Database built from the *previous* class and died on the first
    attribute that class did not have. Keying on the source's mtime
    means pulling new code hands back a new instance.
    """

    return max(
        path.stat().st_mtime for path in Path(__file__).parent.glob("dfs/**/*.py")
    )


@st.cache_resource
def get_database(url: str, code_version: float) -> Database:
    return Database(url)


def _ingest_uploads(database, uploads, fallback_date: str) -> None:
    """Store every uploaded salary file as its own slate.

    Each file is asked what it is rather than told. A salary export
    names its site in the header row and its sport in the positions
    inside, so uploading five files across four sports takes one drop
    instead of five rounds of picking selectors.

    A file that cannot be identified is reported and skipped, never
    guessed at. An NBA export read as NFL parses perfectly, produces a
    pool eligible for no roster slot, and yields an empty build that
    looks like a solver problem -- a wrong answer that costs far more
    than the missing one.
    """

    stored, skipped = [], []

    for upload in uploads:
        try:
            text = upload.getvalue().decode("utf-8-sig")
        except UnicodeDecodeError:
            skipped.append((upload.name, "Not a text CSV."))
            continue

        found = detect(text)

        if not found.complete:
            skipped.append((upload.name, found.reason or "Could not identify it."))
            continue

        # FanDuel's export carries no date, so the sidebar's date stands
        # in. DraftKings writes one into every row and it is used.
        date = found.slate_date or fallback_date

        try:
            slate_id, config, pool = load_slate(
                database, text, found.sport, found.site, date
            )
        except (ValueError, KeyError) as error:
            skipped.append((upload.name, str(error)))
            continue

        stored.append({
            "slate_id": slate_id,
            "label": f"{found.sport}:{found.site} {date}",
            "players": len(pool),
            "name": upload.name,
        })

    for entry in stored:
        st.success(f"{entry['label']} — {entry['players']} players", icon="✅")

    for name, reason in skipped:
        st.warning(f"{name}: {reason}", icon="⚠️")

    # Jump straight to a single upload. With several there is nothing to
    # infer about which one you meant, so the picker is left alone.
    if len(stored) == 1 and not skipped:
        st.session_state["slate_choice"] = stored[0]["slate_id"]


def _choose_slate(database, sport: str, site: str, slate_date: str) -> int | None:
    """Which stored slate the board should show.

    The reason this exists: the file uploader empties whenever the page
    reruns, and the board used to treat that as "no slate", falling back
    to the getting-started screen. Slates were being saved and then
    hidden -- switching sport to look at another one meant re-uploading
    the file you had already uploaded.
    """

    slates = database.slates_with_players()

    if not slates:
        return None

    labels = {
        row["id"]: f"{row['sport']}:{row['site']}  {row['slate_date']}  ({row['players']} players)"
        for row in slates
    }

    # Default to the sidebar's selection when a slate matches it, so the
    # selectors still steer the board.
    matching = database.find_slate(sport, site, slate_date)
    default = st.session_state.get("slate_choice")
    if default not in labels:
        default = matching if matching in labels else slates[0]["id"]

    ids = list(labels)
    choice = st.selectbox(
        "Slate",
        ids,
        index=ids.index(default),
        format_func=lambda slate_id: labels[slate_id],
        key="slate_choice",
        help="Every salary file you have uploaded. Switching here does not re-upload anything.",
    )

    return choice


def main() -> None:
    # Before anything else: no database is opened, no history is
    # collected, and no key is used until the password matches.
    if not require_password():
        return

    st.title("DFS Edge")
    st.caption(
        "Multi-sport daily fantasy research. Projections, ownership leverage, "
        "and lineup construction for DraftKings and FanDuel."
    )

    url = database_url() or "data/dfs.db"
    database = get_database(url, _code_version())

    with st.sidebar:
        st.header("Slate")
        sport = st.selectbox("Sport", SPORTS)
        site = st.selectbox("Site", SITES, format_func=lambda s: {"DK": "DraftKings", "FD": "FanDuel"}[s])
        slate_date = st.date_input("Slate date", dt.date.today()).isoformat()
        st.session_state["slate_date"] = slate_date

        config = get_config(sport, site)

        st.divider()
        # Before the history download, not after: collection can take
        # minutes, and where the data is going is exactly what you want
        # to know before it starts going there.
        _show_storage(url)

        st.divider()
        st.header("Salaries")
        uploads = st.file_uploader(
            "Salary exports (CSV)",
            type="csv",
            accept_multiple_files=True,
            help=(
                "The export from the contest entry screen. On DraftKings this "
                "is the 'Export to CSV' link above the player list; on FanDuel "
                "it is 'Download players list'. Drop several at once -- each "
                "file says which sport and site it is, so they do not have to "
                "be uploaded one at a time."
            ),
        )

        if uploads:
            _ingest_uploads(database, uploads, slate_date)

        st.divider()
        _ensure_history(database, sport)

        st.divider()
        st.header("Build")
        mode = st.radio(
            "Contest type",
            ("cash", "gpp"),
            format_func=lambda m: {"cash": "Cash (50/50, double-up)", "gpp": "Tournament (GPP)"}[m],
            help=(
                "Cash maximises the mean projection and spends the cap. "
                "Tournament chases ceilings, fades ownership, and stacks."
            ),
        )
        lineup_count = st.number_input("Lineups", 1, 150, 1 if mode == "cash" else 20)

        with st.expander("Advanced"):
            ceiling_weight = st.slider(
                "Ceiling weight", 0.0, 1.0,
                0.0 if mode == "cash" else 0.65,
                help="0 maximises the mean projection, 1 maximises the ceiling.",
            )
            ownership_penalty = st.slider(
                "Ownership penalty", 0.0, 0.5,
                0.0 if mode == "cash" else 0.12,
                help="Points deducted per percentage point of projected ownership.",
            )
            max_exposure = st.slider("Max exposure", 0.05, 1.0, 1.0 if lineup_count == 1 else 0.4)
            min_unique = st.number_input("Minimum unique players", 1, 5, 1 if mode == "cash" else 2)

        build = st.button("Build lineups", type="primary", use_container_width=True)

    counts = log_counts(database)
    if not has_collector(sport):
        planned = PLANNED.get(sport.upper(), "no source identified")
        st.info(
            f"No stat collector for {sport} yet (planned source: {planned}), so "
            f"projections use the season average the site prints. That is the "
            f"same number every entrant sees, which means no edge — treat "
            f"lineups as illustrative.",
            icon="ℹ️",
        )
    elif sport.upper() in INCOMPLETE and counts.get(sport.upper(), 0):
        # A sport whose feed is missing stats its scoring pays for needs
        # a stronger notice than a caption. The projections are not
        # merely approximate, they are biased by position, which is the
        # kind of error that looks fine and builds the wrong lineup.
        st.warning(
            f"**{sport} projections are incomplete.** {INCOMPLETE[sport.upper()]}",
            icon="⚠️",
        )

    if sport.upper() not in INCOMPLETE and counts.get(sport.upper(), 0):
        latest = database.latest_game_date(sport)
        age = database.hours_since_collection(sport)
        freshness = ""
        if latest:
            freshness = f" Most recent game {latest}."
        if age is not None and age >= 24:
            freshness += f" Last checked {age / 24:.0f} days ago."
        st.caption(
            f"{counts[sport.upper()]:,} {sport} game logs loaded — projections "
            f"are modelled from history rather than the site average.{freshness}"
        )

    if not config.rules_verified:
        note = VERIFICATION_NOTES.get(config.key, "")
        st.warning(
            f"**Scoring rules for {config.key} are unverified.** {note} "
            "Projections built on an incorrect scoring table will be wrong in "
            "ways that look plausible. Check the site's rules page before "
            "entering real contests.",
            icon="⚠️",
        )

    # Read from storage, not from the uploader. Uploading writes the
    # slate; showing one reads it back. Keeping those separate is what
    # lets several sports be loaded at once and switched between, and
    # what stops a rerun -- which always empties the uploader -- from
    # looking like there is nothing to show.
    slate_id = _choose_slate(database, sport, site, slate_date)

    if slate_id is None:
        _show_getting_started(config)
        _show_calibration(database, sport)
        return

    stored = database.slate(slate_id)
    sport, site, slate_date = stored["sport"], stored["site"], stored["slate_date"]
    config = get_config(sport, site)
    pool = database.player_pool(slate_id)

    mismatches = validate_pool(pool, config)
    for mismatch in mismatches:
        st.error(mismatch, icon="🚫")

    projected, warnings = project_slate(database, slate_id, config, pool, slate_date)
    projected = apply_leverage(projected, config.roster_size)

    for warning in warnings:
        st.info(warning, icon="ℹ️")

    settings = gpp_settings(config, lineup_count) if mode == "gpp" else cash_settings(config)
    settings.ceiling_weight = ceiling_weight
    settings.ownership_penalty = ownership_penalty
    settings.max_exposure = max_exposure
    settings.min_unique = int(min_unique)
    settings.random_seed = 42

    pool_tab, leverage_tab, lineups_tab, research_tab, calibration_tab = st.tabs(
        ["Player pool", "Leverage", "Lineups", "Research", "Calibration"]
    )

    with pool_tab:
        _show_pool(projected, config)

    with leverage_tab:
        _show_leverage(projected, ceiling_weight, ownership_penalty)

    with lineups_tab:
        if build:
            with st.spinner(f"Solving {lineup_count} lineup(s)…"):
                lineups = optimize_lineups(projected, config, settings, count=int(lineup_count))
            st.session_state["lineups"] = lineups
            st.session_state["build_attempted"] = True
            for lineup in lineups:
                database.save_lineup(slate_id, lineup.as_record())

        _show_lineups(
            st.session_state.get("lineups", []),
            config,
            int(lineup_count),
            attempted=st.session_state.get("build_attempted", False),
            pool=projected,
        )

    with research_tab:
        _show_research(database, slate_id, projected, sport, slate_date)

    with calibration_tab:
        _show_results_controls(database, slate_id, slate_date)
        st.divider()
        _show_calibration(database, sport)


@st.cache_resource(show_spinner=False)
def _history_attempted(sport: str) -> dict:
    """One collection attempt per sport per server run.

    Cached on the sport so switching sports in the sidebar does not
    re-trigger a download, and so a failed attempt is not retried on
    every rerun -- which would otherwise mean a network outage produced
    an attempt on every widget interaction.
    """

    return {"done": False}


# Sports whose first load walks an API game by game rather than
# downloading a season as one file.
SLOW_FIRST_LOAD = {"MLB", "NHL"}


def _ensure_history(database: Database, sport: str) -> None:
    """Download or refresh game logs without being asked.

    Two distinct cases, and the second was the one worth fixing. A sport
    with no history at all is obvious: projections fall back to the
    season average the site already prints, which is no edge. A sport
    whose history loaded once and was never updated is not obvious at
    all -- nothing errors, and the projections quietly keep leaning on
    games that stopped being recent while the newest weeks, the ones
    that say most about a player's current role, are simply missing.

    An update reads only the current season from the newest stored game
    onward, so it costs a few seconds rather than the full download.
    """

    if not has_collector(sport):
        return

    empty = log_counts(database).get(sport.upper(), 0) == 0

    if not empty and not is_stale(database, sport):
        return

    state = _history_attempted(sport)
    if state["done"]:
        return

    if empty and sport.upper() in SLOW_FIRST_LOAD:
        # Asked rather than assumed, for these two only. The page cannot
        # be used while it runs, and a first MLB load is minutes -- long
        # enough that starting it unannounced looks like a crash rather
        # than like work.
        st.info(
            f"{sport} history is not loaded yet. The first download reads "
            f"games one at a time and takes a few minutes, and the page "
            f"cannot be used while it runs.",
            icon="ℹ️",
        )
        if not st.button(f"Load {sport} history", use_container_width=True):
            return

    state["done"] = True

    # Baseball and hockey are read game by game from an API rather than
    # downloaded as a file, so their first load takes minutes where the
    # others take seconds. Saying so beats a spinner that looks stuck.
    estimate = "a few minutes" if sport.upper() in SLOW_FIRST_LOAD else "about 15 seconds"
    message = (
        f"Downloading {sport} history (one time, {estimate})…"
        if empty
        else f"Refreshing {sport} history…"
    )

    with st.spinner(message):
        try:
            summary = refresh_history(database, sport, force=True)
        except CollectorError as error:
            st.warning(
                f"Could not update {sport} history: {error} Projections will "
                "use whatever is already stored, which may be out of date.",
                icon="⚠️",
            )
            return

    if summary is None:
        return

    if empty:
        st.success(
            f"Loaded {summary['logs']:,} {sport} game logs for "
            f"{summary['players']:,} players.",
            icon="✅",
        )
    elif summary["logs"]:
        st.caption(
            f"Refreshed {sport}: {summary['logs']:,} recent game logs updated."
        )


def _show_storage(url: str) -> None:
    """Say which backend is in use, because the wrong one loses the record.

    On a hosted deployment SQLite is not merely slower, it is wrong: the
    filesystem is ephemeral, so a slate locked before its games will not
    survive to be scored. That failure is invisible without this.

    Reads the configured URL rather than the live Database, which is
    what the caption is actually about. It also means this line cannot
    take the page down: it once read an attribute off a cached Database
    built from an older version of the class, and a cosmetic caption
    killed the whole app before anything else had rendered.
    """

    if is_postgres_url(url):
        st.caption("Storage: PostgreSQL — locked slates and results persist.")
        return

    st.caption(f"Storage: SQLite at `{url}`.")
    st.caption(
        ":orange[Fine locally. On a hosted deployment this file is erased "
        "whenever the app restarts, taking every locked slate and recorded "
        "result with it — set `DFS_DATABASE_URL` to a PostgreSQL URL.]"
    )


def _show_getting_started(config) -> None:
    st.info(
        "Upload a salary export in the sidebar to begin.", icon="👈"
    )

    left, right = st.columns(2)

    with left:
        st.subheader(f"{config.key} roster")
        st.dataframe(
            pd.DataFrame(
                [
                    {"Slot": slot.name, "Count": slot.count, "Eligible": ", ".join(slot.eligible)}
                    for slot in config.roster
                ]
            ),
            hide_index=True,
            use_container_width=True,
        )
        st.caption(
            f"Salary cap ${config.salary_cap:,} · {config.roster_size} slots · "
            f"max {config.max_per_team or 'unlimited'} per team · "
            f"minimum {config.min_games} games"
        )

    with right:
        st.subheader("Where the edge is")
        st.markdown(
            """
            **Projections beat optimizers.** Every entrant has an optimizer
            solving the same problem. The lineup is not the edge — the numbers
            fed into it are.

            **Late news is the most reliable edge.** A starter ruled out an
            hour before lock hands his workload to a backup whose salary has
            not moved. The Research tab exists for that window.

            **Ownership decides tournaments.** A great lineup that thousands
            of others also entered pays a fraction of the same lineup nobody
            else had. The Leverage tab prices that.

            **Check calibration before entering.** If the model cannot beat
            the season average printed next to each player, there is no edge
            to press.
            """
        )


def _show_pool(pool: list[dict], config) -> None:
    playable = [player for player in pool if player.get("projected_points", 0) > 0]

    columns = st.columns(4)
    columns[0].metric("Players", len(pool))
    columns[1].metric("Projected", len(playable))
    columns[2].metric("Teams", len({p.get("team") for p in pool if p.get("team")}))
    columns[3].metric("Games", len({p.get("game_id") for p in pool if p.get("game_id")}))

    frame = pd.DataFrame(
        [
            {
                "Player": player.get("name"),
                "Pos": "/".join(player.get("positions") or []),
                "Team": player.get("team"),
                "Opp": player.get("opponent"),
                "Salary": player.get("salary"),
                "Proj": player.get("projected_points"),
                "Floor": player.get("floor"),
                "Ceiling": player.get("ceiling"),
                "Value": player.get("value"),
                "Own%": player.get("projected_ownership"),
                "Source": player.get("projection_source"),
            }
            for player in pool
        ]
    )

    search = st.text_input("Filter by name or team", "")
    if search:
        mask = frame.apply(
            lambda row: search.lower() in str(row["Player"]).lower()
            or search.lower() in str(row["Team"]).lower(),
            axis=1,
        )
        frame = frame[mask]

    st.dataframe(
        frame.sort_values("Proj", ascending=False),
        hide_index=True,
        use_container_width=True,
        height=560,
    )


def _show_leverage(pool: list[dict], ceiling_weight: float, ownership_penalty: float) -> None:
    st.subheader("Ownership leverage")
    st.caption(
        "Leverage compares how much a player produces to how much of the field "
        "will roster him. Positive means underowned relative to production — the "
        "tournament play. Negative is chalk: fine in cash, expensive in a GPP."
    )

    rows = [
        {
            "Player": player.get("name"),
            "Team": player.get("team"),
            "Salary": player.get("salary"),
            "Proj": player.get("projected_points"),
            "Ceiling": player.get("ceiling"),
            "Own%": player.get("projected_ownership"),
            "Value": player.get("value"),
            "Leverage": player.get("leverage"),
            "GPP score": gpp_score(player, ceiling_weight, ownership_penalty),
        }
        for player in pool
        if player.get("projected_points", 0) > 0
    ]

    frame = pd.DataFrame(rows)
    if frame.empty:
        st.warning("No projected players to rank.")
        return

    left, right = st.columns(2)

    with left:
        st.markdown("**Most leveraged** — produce more than the field expects")
        st.dataframe(
            frame.sort_values("Leverage", ascending=False).head(15),
            hide_index=True, use_container_width=True,
        )

    with right:
        st.markdown("**Biggest chalk** — the field is more interested than the projection warrants")
        st.dataframe(
            frame.sort_values("Leverage").head(15),
            hide_index=True, use_container_width=True,
        )

    st.info(
        "This ranking uses a fast rank-based proxy. The stronger metric is "
        "optimal rate from `dfs.ownership.simulation`, which measures how often "
        "a player lands in the best lineup across simulated slates. It takes "
        "seconds rather than milliseconds, so run it from the CLI before a "
        "tournament rather than on every interaction.",
        icon="💡",
    )


def _show_lineups(lineups: list, config, requested: int, attempted: bool = False, pool: list[dict] | None = None) -> None:
    if not lineups and not attempted:
        st.info("Press **Build lineups** in the sidebar.", icon="👈")
        return

    if not lineups:
        # A build that ran and returned nothing is a different state from
        # a build that never ran, and saying so is the difference between
        # a usable error and a user staring at an unchanged screen.
        st.error(
            "No legal lineup exists for this pool under these constraints.",
            icon="🚫",
        )
        _diagnose_infeasible(pool or [], config)
        return

    if len(lineups) < requested:
        st.warning(
            f"Built {len(lineups)} of {requested} requested. The exposure cap "
            "and uniqueness minimum are in tension with the pool size — raise "
            "the exposure cap or lower the lineup count.",
            icon="⚠️",
        )

    columns = st.columns(4)
    columns[0].metric("Lineups", len(lineups))
    columns[1].metric("Best projection", f"{max(l.total_projection for l in lineups):.1f}")
    columns[2].metric("Median salary", f"${int(sorted(l.total_salary for l in lineups)[len(lineups)//2]):,}")
    columns[3].metric("Median ownership", f"{sorted(l.total_ownership for l in lineups)[len(lineups)//2]:.0f}%")

    _show_export(lineups, config)

    for index, lineup in enumerate(lineups, start=1):
        header = (
            f"Lineup {index} — ${lineup.total_salary:,} · "
            f"proj {lineup.total_projection:.1f} · "
            f"ceiling {lineup.total_ceiling:.1f} · "
            f"own {lineup.total_ownership:.0f}%"
        )
        with st.expander(header, expanded=index == 1):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Slot": player.slot,
                            "Player": player.name,
                            "Team": player.team,
                            "Opp": player.opponent,
                            "Salary": player.salary,
                            "Proj": player.projection,
                            "Ceiling": player.ceiling,
                            "Own%": player.ownership,
                        }
                        for player in lineup.players
                    ]
                ),
                hide_index=True,
                use_container_width=True,
            )

    from dfs.optimizer.lineup import exposure_report

    if len(lineups) > 1:
        st.subheader("Exposure")
        st.caption(
            "How concentrated the build is. Twenty lineups sharing the same "
            "core is one bet at twenty times the stake, not twenty bets."
        )
        st.dataframe(
            pd.DataFrame(exposure_report(lineups)),
            hide_index=True,
            use_container_width=True,
            height=340,
        )


def _show_export(lineups: list, config) -> None:
    """Offer the lineups as files: one to upload, one to read.

    Both sites accept a CSV of their own player ids on their upload
    screen, which is the interface they provide for entering many
    lineups. The readable file exists because the upload file is columns
    of bare numbers -- fine for the site, useless for noticing you have
    rostered a pitcher against your own stack.
    """

    site_name = "DraftKings" if config.site == "DK" else "FanDuel"
    slate_date = st.session_state.get("slate_date", "slate")

    try:
        upload = to_upload_csv(lineups, config)
    except ExportError as error:
        st.warning(f"These lineups cannot be exported: {error}", icon="⚠️")
        return

    left, right = st.columns(2)

    with left:
        st.download_button(
            f"Download for {site_name} upload",
            data=upload,
            file_name=upload_filename(config, slate_date),
            mime="text/csv",
            use_container_width=True,
            help=(
                f"Player ids in roster order. Upload it on {site_name}'s "
                "own bulk-entry screen. Check the headers against their "
                "template first — the column set varies by contest type."
            ),
        )

    with right:
        st.download_button(
            "Download readable copy",
            data=to_readable_csv(lineups, config),
            file_name=readable_filename(config, slate_date),
            mime="text/csv",
            use_container_width=True,
            help="Names, teams, salaries and projections. Read this one before uploading.",
        )


def _diagnose_infeasible(pool: list[dict], config) -> None:
    """Say which specific roster slot could not be filled, and why.

    "Infeasible" on its own is useless to act on. Naming the empty slot
    turns it into one obvious fix.
    """

    playable = [player for player in pool if player.get("projected_points", 0) > 0]

    rows = []
    for slot in config.roster:
        eligible = sum(
            1
            for player in playable
            if slot.accepts([str(p).upper() for p in player.get("positions") or []])
        )
        rows.append(
            {
                "Slot": slot.name,
                "Needs": slot.count,
                "Eligible in pool": eligible,
                "Short by": max(0, slot.count - eligible),
            }
        )

    frame = pd.DataFrame(rows)
    short = frame[frame["Short by"] > 0]

    if not short.empty:
        st.markdown(
            "**These slots cannot be filled.** The pool has no eligible players "
            "for them, which usually means the sport selector does not match "
            "the uploaded file."
        )
        st.dataframe(short, hide_index=True, use_container_width=True)
    else:
        cheapest = sorted(playable, key=lambda p: p.get("salary") or 0)[: config.roster_size]
        floor_cost = sum(p.get("salary") or 0 for p in cheapest)
        st.markdown(
            "Every slot has eligible players, so the conflict is in the "
            "constraints rather than the pool."
        )
        if floor_cost > config.salary_cap:
            st.markdown(
                f"- The {config.roster_size} cheapest eligible players cost "
                f"${floor_cost:,}, above the ${config.salary_cap:,} cap. No "
                "lineup can fit."
            )
        st.markdown(
            "- Check locked players for position or salary conflicts.\n"
            "- Lower the minimum-salary floor in **Advanced**.\n"
            "- Raise the exposure cap or lower the lineup count."
        )

    st.dataframe(frame, hide_index=True, use_container_width=True)


def _show_research(database: Database, slate_id: int, pool: list[dict], sport: str, slate_date: str) -> None:
    st.subheader("Player research")
    st.caption(
        "Claude reads current reporting on a player's availability and role, "
        "briefing each one several times and keeping the median. When the "
        "samples disagree beyond the reproducibility gate the written brief is "
        "kept and the number is dropped — disagreement about whether a player "
        "will play is itself the finding."
    )

    st.warning(
        "Run this on the handful of players whose status is genuinely in doubt, "
        "not the whole slate. Each player costs several web searches times the "
        "sample count.",
        icon="💸",
    )

    names = {
        f"{player.get('name')} ({player.get('team')}) ${player.get('salary'):,}": player
        for player in sorted(pool, key=lambda p: -(p.get("salary") or 0))
    }

    chosen = st.multiselect("Players to research", list(names), max_selections=10)

    if st.button("Research selected", disabled=not chosen):
        from dfs.research.player_brief import PlayerBriefError, brief_player

        progress = st.progress(0.0)
        results = []

        for index, label in enumerate(chosen):
            try:
                brief = brief_player(names[label], slate_date, sport)
                database.save_brief(slate_id, brief.as_record())
                results.append(brief)
            except PlayerBriefError as error:
                st.error(f"{label}: {error}")
            progress.progress((index + 1) / len(chosen))

        st.session_state["briefs"] = results

    for brief in st.session_state.get("briefs", []):
        multiplier = (
            f"{brief.multiplier:.2f}x" if brief.multiplier is not None else "no estimate"
        )
        with st.expander(f"{brief.name} — {brief.status} · {multiplier}", expanded=True):
            st.markdown(f"**Situation.** {brief.situation}")
            st.markdown(f"**Role.** {brief.role_change}")
            if brief.what_would_settle_it:
                st.markdown("**What would settle it**")
                for item in brief.what_would_settle_it:
                    st.markdown(f"- {item}")
            if brief.multiplier is None:
                st.caption(
                    f"No multiplier recorded: {brief.samples} samples disagreed "
                    "beyond the reproducibility gate, or a majority declined to "
                    "estimate. The brief above still stands."
                )
            else:
                st.caption(
                    f"Median of {brief.samples} samples · "
                    f"dispersion {brief.multiplier_stdev} · "
                    f"confidence {brief.confidence}"
                )

    stored = database.briefs(slate_id)
    if stored:
        st.divider()
        st.markdown("**Previously recorded for this slate**")
        st.dataframe(
            pd.DataFrame(stored)[
                ["name", "status", "multiplier", "dispersion", "sample_count", "created_at"]
            ],
            hide_index=True,
            use_container_width=True,
        )


def _show_results_controls(database: Database, slate_id: int, slate_date: str) -> None:
    """Lock this slate before its games, and score it afterwards.

    These two actions are what turn a backtest into a record. Locking
    stamps the projections as final; resolving compares them to what
    happened, using the same game logs that trained them.
    """

    st.subheader("This slate")

    row = database.connection.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM projections p WHERE p.slate_id = ?) AS projected,
            (SELECT COUNT(*) FROM projections p WHERE p.slate_id = ? AND p.locked_at IS NOT NULL) AS locked,
            (SELECT COUNT(*) FROM actuals a WHERE a.slate_id = ?) AS resolved
        """,
        (slate_id, slate_id, slate_id),
    ).fetchone()

    columns = st.columns(3)
    columns[0].metric("Projected", row["projected"])
    columns[1].metric("Locked", row["locked"])
    columns[2].metric("Resolved", row["resolved"])

    upcoming = not slate_is_scorable(slate_date)

    left, right = st.columns(2)

    with left:
        if st.button(
            "Lock projections",
            disabled=row["projected"] == 0,
            use_container_width=True,
            help=(
                "Marks these projections final so they can be scored. Do this "
                "before the slate locks — a projection stamped afterwards is a "
                "backtest, and is reported separately."
            ),
        ):
            locked = database.lock_slate(slate_id)
            st.success(f"Locked {locked} projections.")

    with right:
        if st.button(
            "Score against results",
            disabled=row["locked"] == 0 or upcoming,
            use_container_width=True,
            help=(
                "Compares the locked projections to the box scores. Available "
                "once the slate's games have been played."
            ),
        ):
            with st.spinner("Fetching results…"):
                summary = resolve_slate_from_logs(database, slate_id)

            if summary["played"] == 0:
                st.warning(
                    "No player in this pool has a game log for "
                    f"{slate_date}. Either the games have not been published "
                    "yet, or the slate date does not match when they were played.",
                    icon="⚠️",
                )
            else:
                st.success(
                    f"Scored {summary['resolved']} players — {summary['played']} "
                    f"played, {summary['did_not_play']} did not.",
                    icon="✅",
                )

    if upcoming and row["projected"]:
        st.caption(
            "Lock before the slate starts. Results can be scored once its "
            "games have been played."
        )
    elif row["locked"] and not row["resolved"]:
        st.caption(
            "Score once the games have finished. If box scores have not been "
            "published yet, this reports nothing found rather than failing."
        )


def _show_calibration(database: Database, sport: str) -> None:
    st.subheader("Calibration")

    records = database.resolved_projections(sport)
    report = calibration_report(records)

    st.markdown(f"**{report['verdict']}**")

    if not report["overall"]:
        # Names the buttons, not the Python behind them. This text
        # predated the controls and was still sending people to write
        # code for something the UI does.
        st.caption(
            "Upload a slate, then use **Lock projections** above before its "
            "games start, and **Score against results** once they have "
            "finished. A few hundred locked projections are needed before "
            "the numbers here mean anything."
        )
        return

    if report.get("forward_verdict"):
        st.info(report["forward_verdict"], icon="🎯")

    forward, backtest = report.get("forward"), report.get("backtest")
    if forward or backtest:
        rows = [
            {
                "Record": label.title(),
                "n": row["count"],
                "MAE": row["mae"],
                "Baseline MAE": row["baseline_mae"],
                "Skill": row["skill"],
            }
            for label, row in (("forward", forward), ("backtest", backtest))
            if row
        ]
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        st.caption(
            "Forward projections were locked before their games; backtest ones "
            "were reconstructed afterwards. Only the forward record can show "
            "whether researching players before lock pays, because a backtest "
            "contains no injury news."
        )

    overall = report["overall"]
    columns = st.columns(5)
    columns[0].metric("Resolved", overall["count"])
    columns[1].metric("MAE", overall["mae"])
    columns[2].metric("Baseline MAE", overall["baseline_mae"] or "—")
    columns[3].metric(
        "Skill", f"{overall['skill']:+.3f}" if overall["skill"] is not None else "—"
    )
    columns[4].metric("Rank corr", overall["correlation"])

    st.caption(
        "Skill is the fraction by which the model beats the season average the "
        "site prints next to each player. Zero means no edge; the sign is what "
        "matters, not the absolute error."
    )

    if report["by_position"]:
        st.markdown("**By position** — watch for one group being systematically over-projected")
        st.dataframe(pd.DataFrame(report["by_position"]), hide_index=True, use_container_width=True)

    if report["by_sport"]:
        st.markdown("**By sport and site**")
        st.dataframe(pd.DataFrame(report["by_sport"]), hide_index=True, use_container_width=True)


if __name__ == "__main__":
    main()
