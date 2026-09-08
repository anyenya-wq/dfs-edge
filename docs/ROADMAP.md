# Roadmap

Ordered by how much each would actually improve results, which is not
the order they look most interesting in.

## 1. Stat ingestion — done, with one caveat

**NFL and NBA are built**, and load automatically on first use.
Backtested skill over the season-average baseline: NBA +0.075, NFL
+0.037, NFL team defences +0.088.

NBA deliberately uses hoopR's published box scores rather than `nba_api`.
`nba_api` talks to stats.nba.com, which rate-limits hard and refuses
datacenter addresses, so a collector built on it works on a laptop and
fails in the hosted environment this app actually runs in.

MLB is built against the official StatsAPI, since no reachable bulk file
carries the current season: the sportsdataverse mirror stops at 2022 and
Retrosheet publishes event files long after a season ends. It is the only
collector that walks games one at a time, which is why its first load
takes minutes and its refresh does not.

It is also the only one not verified against its live source, because
statsapi.mlb.com was unreachable from the environment it was written in.
Run the gated live test before relying on it.

Hockey took the same route as baseball and for the same reason: the
sportsdataverse mirror stops at the 2023-24 season, which is useless for
projecting current rosters, so the NHL's own API is what it reads.

All five collect:

- **NFL** — ~~`nflverse`~~ **done**
- **NBA** — ~~hoopR~~ **done**
- **MLB** — ~~MLB StatsAPI~~ **done, verified live**
- **NHL** — ~~NHL API~~ **done, verified live**
- **EPL** — ~~FPL mirror~~ **done, and verified, but incomplete**

Soccer's caveat is not about verification. Its source is reachable and
was checked against three real seasons; the problem is that the FPL feed
does not carry shots on goal, chances created or crosses, and the sites
pay for all three. Projections read low, and the shortfall differs by
position, so cross-position rankings skew toward defenders. Closing that
needs a source with match event data — FBref or Opta-derived — none of
which is free.
- **MLB** — MLB StatsAPI (the richest of the four)

Write one collector per sport that normalises into `game_logs`, following
the NFL one. The projection engine reads that table and does not care
where rows came from, so each collector is independent.

**NFL team defences are built.** nflverse publishes no DST row, so the
unit is aggregated from its own defenders, and points allowed is joined
from the scoreboard. Sacks are read from the offence that suffered them
rather than the defenders credited with them: across 2024 the two counts
agree on 560 of 570 team-games, and all ten disagreements are a sack no
defender was credited with.

The projection is not the player model. Measured over 2023 and 2024, a
defence's own recent form beats the season-average baseline by +0.007 --
noise. The offence it is about to face is worth +0.046 alone, and a
half-and-half blend, with the weight chosen on 2023, is worth **+0.088
on held-out 2024**. A defence projected from its own history is worth
nothing; the matchup is the projection.

## 1b. Scoring rules — both MLB pairs now verified at source

**MLB:FD is read from FanDuel's own Rules & Scoring tab.** Every hitting
line already matched. The pitching table was missing the quality start
entirely -- four points on roughly a third of starts, which DraftKings
does not pay for at all.

It is derived at scoring time rather than at collection, from innings
and earned runs, so history collected before the rule existed scores
correctly without a re-download. That is the pattern to follow for any
scoring line added later.

**MLB:DK is read from DraftKings' published MLB Classic rules.** Both
tables matched, as did the $50,000 cap and the ten-man roster. The team
limit did not: the rule is "no more than 5 *hitters* from any one team",
and the optimizer was counting every player, so a five-man stack
alongside that team's own pitcher -- a legal lineup -- could not be
built. Caps now name the positions they exclude.

Still unverified at source: both NFL pairs, both NBA pairs, both NHL
pairs, and both EPL pairs. Each carries a note in the app naming what
specifically is unchecked.

## 2. Hosting — done

The database is addressed by URL, so SQLite runs locally and PostgreSQL
runs hosted with no code change. That was the blocker for putting the
board on Streamlit Cloud: its filesystem is ephemeral, and with SQLite a
slate locked before its games would not survive to be scored.

What remains is a deployment decision rather than work: create a
PostgreSQL database, set `DFS_DATABASE_URL` in Streamlit Cloud's secrets,
and restrict who can open the app if `ANTHROPIC_API_KEY` is set there —
a public URL with a key behind it is an open tab on your account.

## 3. Salary automation — deliberately not done

DraftKings serves salaries from public JSON endpoints that need no login,
and it would be straightforward to fetch them. FanDuel requires
authentication, so automating it would mean storing credentials and
impersonating a logged-in session.

Both sites prohibit automated access. The realistic cost is not legal but
account limitation, on an account holding real money, in exchange for
saving about thirty seconds per slate -- and not even fully, since you
would still choose which slate. Undocumented endpoints also break without
notice, at the worst possible moment.

The CSV export is the same data, stable, and permitted. If this is ever
built it should be DraftKings only, opt-in and off by default, with the
upload always available as the fallback.

## 4. Line and lineup confirmation

The projection model can already accept an opportunity override; nothing
supplies one automatically. Confirmed NBA lineups, NHL line combinations
and starting goalies, and NFL inactives all land shortly before lock and
all move projections more than any model refinement would.

The research layer reads these per player on demand. A collector that
watches them for a whole slate would be better.

## 5. Real ownership data

The current model is a value-based prior, normalised so ownership sums
to the contest budget. It gets the ordering roughly right and the level
approximately. Industry projections are better because they observe
actual entry behaviour. Worth buying before worth rebuilding.

## 6. Contest results import — partly done

**The projection loop is closed, and runs unattended.** A slate can be
locked before its games and scored afterwards from the collected box
scores, and calibration reports forward forecasts separately from
backtests. A nightly workflow scores every locked slate whose games have
finished, so the record fills without anyone pressing a button — set
`DFS_DATABASE_URL` as a repository secret to switch it on, since it has
to read the same database the app writes to.

Slates whose box scores have not been published are left for the next
run rather than scored: resolution records a player with no log as a
zero, so a premature run would write a whole slate of zeros and, because
the slate then has results, nothing would ever revisit it.

What remains is ownership. Both sites publish contest results with actual
ownership after settlement, which is the only way to check whether the
ownership model is any good — the projection side no longer needs it.

## 7. Backtesting

The pieces are in place — `game_logs` reads accept a `before` cutoff so a
projection cannot see the game it is projecting, and projections lock
before results are recorded. What is missing is a driver that walks
historical slates and reports skill over time. Needs (1) first.

## 8. Correlation in the simulation

`simulate_optimal_rates` samples each player independently, which
understates the variance of a stacked lineup: when the quarterback has a
big game his receiver does too, and independent sampling never produces
that joint outcome. Sampling a game-level factor first, then players
conditional on it, would make optimal rates for stacks honest.

## Deliberately not planned

**Automated entry.** Both sites prohibit it, and the failure modes are
expensive and unattended.

**Scraping salary endpoints.** The CSV export is the same data, stable,
and permitted.
