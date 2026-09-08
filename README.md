# DFS Edge

Multi-sport daily fantasy research and lineup construction for DraftKings
and FanDuel. Covers NFL, NBA, MLB, NHL, and EPL soccer.

## What this is

Four layers, each usable on its own:

| Layer | What it does |
|---|---|
| **Ingest** | Parses DraftKings and FanDuel salary exports, and collects historical stats for all five sports |
| **Projections** | Splits playing time from per-minute production, with ceiling and floor estimates |
| **Research** | Claude reads current injury and role reporting, with repeated sampling and a gate on the number |
| **Optimizer** | Integer-programming lineup construction with stacking, exposure caps, and ownership leverage |

Plus a calibration harness that scores projections against results, and a
Streamlit board that ties it together.

## Where the edge actually is

Worth being blunt about, because it shapes how the code is organised.

**The optimizer is not the edge.** Every entrant has one, and they all
solve the same integer program. Given identical projections, your lineups
and theirs are identical. The projections are the edge.

**Late news is the most reliable edge available.** A starter ruled out an
hour before lock hands his workload to a backup whose salary has not
moved. This is why projections here are built as `opportunity × rate`
rather than as an average of past fantasy points — it lets the research
layer revise the volatile half without touching the stable half.

**Ownership decides tournaments.** A great lineup that thousands of others
also entered pays a fraction of the same lineup nobody else had. Cash
games and tournaments therefore want opposite things, and the two
`OptimizerSettings` presets reflect that.

**Check calibration before entering.** Both sites print a season average
next to every player. If your projections cannot beat that number, there
is no edge to press, and the calibration harness says so in those words.

## Getting started

```bash
pip install -r requirements-dev.txt
pytest -q
```

Download the salary export from the contest entry screen — on DraftKings
the "Export to CSV" link above the player list, on FanDuel "Download
players list" — then:

History downloads the first time you open a sport and refreshes itself
when it gets stale, so normally there is nothing to run. The refresh
matters more than it sounds: history that loads once and is never updated
does not fail, it just stops being recent, and the projections built on
it quietly get worse as a season goes on. An update reads only the
current season from the newest stored game onward, so it costs a few
seconds rather than a full download.

To do either explicitly:

```bash
python -m dfs.cli load-history --sport NBA            # full, ~10s
python -m dfs.cli load-history --sport NBA --refresh  # current season only, ~3s
python -m dfs.cli load-history --sport MLB            # ~90 days, a few minutes
python -m dfs.cli load-history --sport NHL            # ~90 days, a few minutes
python -m dfs.cli load-history --sport EPL            # ~2 seasons, ~5s
```

Baseball and hockey are slower than the others on a first load, and
unavoidably so: neither has a bulk file carrying the current season, so
both are read one box score at a time from the league's own API.
Afterwards a refresh costs a handful of requests.

```bash
# One cash lineup
python -m dfs.cli build --csv DKSalaries.csv --sport NBA --site DK --date 2026-01-15

# Twenty tournament lineups with stacking and exposure caps
python -m dfs.cli build --csv DKSalaries.csv --sport NFL --site DK \
    --date 2026-09-07 --mode gpp --lineups 20

# Roster and scoring rules for any sport-site pair
python -m dfs.cli rules --sport NBA --site DK

# Score your projections against results
python -m dfs.cli slates                       # what is stored, and how far through
python -m dfs.cli resolve --slate 3            # score a slate against the box scores
python -m dfs.cli resolve --due                # score every locked slate whose games finished
python -m dfs.cli calibrate
```

Or run the board:

```bash
streamlit run app.py
```

From anywhere else in the repository, use the launcher at the root:

```bash
./run-dfs-edge.sh
```

It also installs anything missing from `requirements.txt` first. A
Codespace built from the repository root installs the *root's*
requirements, which are Project Oracle's, so DFS Edge's own
dependencies -- `pulp`, `pyarrow`, and `psycopg` when the database URL
points at PostgreSQL -- are not there by default.

The repository root holds a second Streamlit app -- Project Oracle --
also called `app.py`. Running `streamlit run app.py` from the root
starts that one instead, with no error to say so; it just looks like
DFS Edge stopped updating.

## Hosting

`docs/DEPLOY.md` covers putting the board on Streamlit Community Cloud:
always on, one URL, nothing to start. The app is stateless -- the record
lives in PostgreSQL -- so hosting it is a redeploy rather than a
migration.

A public deployment gates itself behind `APP_PASSWORD`. Without that
set, a deployment says so in a banner rather than quietly serving your
forecasts to anyone with the link.

## Where the data lives

SQLite by default: no server, nothing to configure, and the file sits at
`data/dfs.db`. That is right for a Codespace or a laptop.

It is wrong for a hosted deployment, and the reason is specific. Streamlit
Cloud's filesystem is ephemeral, so a slate locked before Sunday's games
would not exist on Monday — which destroys the forward calibration record,
the one thing a backtest cannot give you. Game logs would merely be
re-downloaded; the record would be gone.

Point `DFS_DATABASE_URL` at PostgreSQL and it persists:

```bash
export DFS_DATABASE_URL="postgresql://user:password@host:5432/dfs"
```

Nothing else changes — same schema, same code, same tests. On Streamlit
Cloud set it in the app's Secrets; a free Neon or Supabase database is
enough, since the record is small. Game logs are the bulky part (105,000
rows for three NBA seasons) and they regenerate themselves, so a slate
plus its projections and results is a few thousand rows a season.

Both backends are verified by the same suite rather than by separate
tests, which is what keeps the claim honest:

```bash
pytest                                                    # SQLite
DFS_TEST_DATABASE_URL=postgresql://... pytest             # PostgreSQL
```

Research briefs need `ANTHROPIC_API_KEY` set. Nothing else requires a key.

## Verify the scoring tables before you trust them

**The scoring rules in `dfs/sports/*.py` are transcribed from published
rules and are not verified.** Both sites revise scoring between seasons,
and the EPL tables in particular are low confidence — the peripheral
rates (crosses, tackles, chances created) are the most likely to be
stale, and FanDuel has withdrawn soccer contests in some markets.

A wrong scoring table produces projections that are wrong in ways that
look entirely plausible, so this matters more than it sounds. Check
against the site's live rules page, correct the dict, then set
`rules_verified=True` on that config. `python -m dfs.cli rules` prints
what still needs checking, and the Streamlit board shows a banner until
it is done.

## Data sourcing

Use the CSV export, not a scraper. Both sites publish a download button
on the draft page; the undocumented JSON endpoints people pass around are
against the terms of use on a strict reading and change shape without
notice. A broken scraper at 12:55 before a 13:00 lock is the worst
possible time to find out, and the CSV is the same data.

For historical stats, the free and genuinely open sources are good:
`nflverse` for football, `nba_api` for basketball, MLB StatsAPI for
baseball. Populate `game_logs` from whichever you use — the projection
engine reads that table and does not care where the rows came from.

## Entering the lineups

Both sites accept a CSV of their own player ids on their bulk-entry
screen, which is the interface they provide for entering many lineups at
once. The Lineups tab offers two files:

- **the upload file** — ids in roster order, for the site
- **a readable copy** — names, teams, salaries and projections, for you

Read the second before uploading the first. The upload file is columns
of bare numbers, which is right for the site and useless for noticing
that you have rostered a pitcher against your own stack.

From the command line:

```bash
python -m dfs.cli build --csv DKSalaries.csv --sport MLB --site DK \
    --date 2026-09-02 --mode gpp --lineups 20 --export upload.csv
```

The column set varies by contest type and both sites have changed it
between seasons, so compare headers against the template on their upload
screen. The ids in the cells are the part that would be laborious by
hand, and those do not change.

## Layout

```
dfs/
  sports/       Roster, scoring, and correlation rules per sport-site pair
  ingest/       Salary export parsing and player-name normalisation
  projections/  Baseline projection model and the calibration harness
  optimizer/    Integer-programming lineup construction
  ownership/    Ownership projection, leverage, Monte Carlo optimal rates
  research/     Claude-researched player status and role briefs
  db/           SQLite storage
  pipeline.py   End-to-end flow
  cli.py        Command line
app.py          Streamlit research board
```

## Design notes

**Sport-site pairs, not sports.** DraftKings and FanDuel disagree on
scoring, salary caps, and roster structure for the same sport. NFL is
full PPR on one and half PPR on the other; FanDuel NBA has no flex slots
where DraftKings has three. Everything downstream takes a `SportConfig`
rather than a sport name.

**The optimizer solves an assignment problem.** Variables are
(player, slot) pairs rather than players. With flexible slots, whether a
set of players forms a legal lineup is itself a matching problem, so
selecting players first and fitting them to slots afterwards can return
nine players who cannot legally be arranged.

**Collection is batched and incremental.** Writing game logs one row at a
time commits per row, which made loading three NBA seasons ninety-five
seconds of almost pure fsync. Batching the same rows into one transaction
is over a hundred times faster, which is what makes refreshing on app
start cheap enough to do silently. An incremental refresh re-reads the
three days before the newest stored game rather than starting exactly
where it left off, because box scores get corrected after the fact and a
late-finishing game can be published after an earlier one on the same
date.

**Projections are locked before results are recorded.** `locked_at`
stamps a projection as on the record; results land in a separate table.
There is no code path that can improve a projection after seeing the
outcome, which is what makes the calibration numbers worth reading.

**Forecasts and backtests are never averaged together.** A projection
written before its slate was played is a forecast made under real
uncertainty; one reconstructed afterwards is a backtest. Both are honest
— game logs are always read with a cutoff, so neither can see the game it
is projecting — but only the forward record can show whether researching
players before lock pays, because a backtest contains no injury news.
Reported apart, since a large backtest would otherwise drown out the
small forward record that is the one worth watching.

**Results come from the collectors, not a second feed.** The box scores
that train the projections are the same ones that score them, so
resolving a slate needs no new data source. A player in the pool with no
log for that date scores zero rather than being skipped — dropping them
would quietly remove the model's worst misses from its own report card.

**Research briefs and their numbers are separate deliverables.** Each
player is briefed several times and the median multiplier is kept. When
the samples disagree beyond the gate, the written brief stands and the
number is dropped — disagreement about whether a player will play is
itself the finding, and averaging it into a confident number would be
worse than having none. This contract is carried over from the
prediction-market project that preceded this one.

## How good are the projections?

Measured, not asserted. Both sports were backtested by projecting each
slate using only games before it, scored against the season average the
sites display:

| Sport | Season | Model MAE | Baseline MAE | Skill | n |
|---|---|---|---|---|---|
| **NBA** | 2025-26 | 8.047 | 8.701 | **+0.075** | 4,419 player-games |
| **NFL** | 2025 | 5.007 | 5.216 | **+0.040** | 5,225 player-weeks |
| NFL | 2024 | 5.035 | 5.228 | +0.037 | 5,199 player-weeks |

Positive at every position in both — NBA strongest at PG (+0.102), NFL
at RB (+0.045) — and closer than the baseline on about 53.5% of cases in
each.

**NBA is roughly twice the edge, and that is not an accident.** The
projection model splits playing time from per-minute production, and
basketball is the sport where that split pays: minutes are published
directly rather than proxied, they swing hard game to game, and a late
scratch reassigns twenty-five of them to someone whose salary has not
moved. Football's opportunity term has to be approximated from touches,
and its workloads are steadier, so there is less for the split to find.

By the tool's own thresholds NBA clears "meaningfully better than the
site average" while NFL sits in the thinner band that is "likely eaten by
rake in anything but the softest fields." Neither is a licence to size
up.

The NFL decay half-life was tuned on 2023, so neither reported season is
fitted to it, and the tuning found the original guess of four games was
already optimal. 2025 is the stronger evidence of the two: it was
collected after the tuning and after 2024 was reported, and the model
held up on it unchanged.

More importantly, **+0.037 is the floor, not the ceiling.** It measures
what the model does with no information advantage at all. The design bet
is that the edge lives in late news — a starter ruled out an hour before
lock — and a backtest over historical box scores structurally cannot
measure that, because it does not know who was ruled out. Whether the
research layer adds to this can only be established forward, by logging
projections before lock and scoring them.

## Status

Implemented and tested (320 tests, plus 12 network tests gated behind
`DFS_NETWORK_TESTS=1`). All five collectors are confirmed against their
live sources — the gated tests pull real box scores and assert on what
comes back, rather than on fixtures.

Two of them, MLB and NHL, could not be reached from the environment they
were written in, so they were verified afterwards from a Codespace. That
run also found two things the original environment could not: a
`pytest.ini` missing `pythonpath`, which only showed up under the bare
`pytest` entrypoint, and an NHL test that asserted games exist in the
last five days — true for eight months of the year. What it does *not* have yet:

- **Complete soccer stats.** The EPL collector works, but its feed has no
  shots on goal, chances created or crosses, and DraftKings pays for all
  three. Projections therefore read low, and — the part that matters —
  the shortfall differs by position, so rankings within a position are
  usable while rankings across positions skew toward defenders. Combined
  with soccer's scoring tables being unverified, it is much the weakest
  of the five. The app says so in a banner rather than hiding it.
- **A defence's own form.** It is worth +0.007 over the baseline, which
  is noise, so the projection leans on the matchup instead (+0.088
  blended). That is the best available from free data, but it means a
  defence that has genuinely improved is priced almost entirely on who
  it is playing.
- **Real ownership data.** The ownership model is a value-based prior.
  Industry projections see actual entry behaviour and are better.
- **A forward record.** The loop is wired — lock a slate before its games,
  score it afterwards — but nothing has run through it yet in earnest.
  Until it has, every skill number here is a backtest.
