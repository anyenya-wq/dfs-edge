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

**Both NFL pairs are now read from the sites too** -- DraftKings from
its NFL Classic rules page, FanDuel from a contest's own Rules &
Scoring tab. Two things were wrong. FanDuel was set to pay no yardage
bonuses; it pays all three, at 100 rushing, 100 receiving and 300
passing yards, three points each, which had been depressing every
FanDuel ceiling and so every tournament build. And points allowed was
read as the opponent's final score, when both sites exclude touchdowns
the opposing *defence* scored on your own offence -- six points a time,
against a banded scale, so a pick-six was silently costing a defence up
to three fantasy points it should have kept.

A verified table can still have an unverified corner. FanDuel's scoring
tab covers scoring and nothing else, so its $60,000 cap and
four-per-team limit remain assumed, and both the app and the CLI now
print the caveat on a verified table instead of only on an unverified
one -- which is where two such notes had been sitting unread.

**Both soccer pairs are now read from the sites, along with two real
Champions League exports.** This was the worst of the five and the
numbers were only part of it.

Almost every rate was wrong, but three things were wrong in kind. Both
tables charged 5 to 6 points for an own goal, a penalty neither site
imposes. Neither site scores soccer from one table: DraftKings pays a
clean sheet to defenders only and interceptions to everyone but the
keeper, and FanDuel publishes three separate tables, so scoring needed
a third table rather than the one exception every other sport gets. And
FanDuel prices soccer in dollars -- seven players against a $100 cap,
salaries from $5 to $23 -- where the config had nine players and
$60,000.

The exports then found what the rules pages could not. A FanDuel soccer
file was not recognised as soccer at all: it writes FWD/MID/DEF/GK, the
config knew only F/M/D/GK, and detection matched a third of the players
and named NFL as the closest guess. DraftKings writes a soccer matchup
as "LIV vs ATL" rather than "ATL@LIV", so every player in a soccer
slate arrived with no opponent and no game id -- and an absent game is
not an error anywhere downstream, so this silently disabled the
minimum-games rule instead of failing.

That rule turned out not to have worked anywhere. Distinct-game and
distinct-team floors are built from an indicator per group, and the
indicator was only forced *up* by a rostered player, never down when
the group was empty -- so the solver could switch on indicators for
games it rostered nobody from and satisfy the floor by arithmetic. Made
to bite, it changes real output: given one game worth twenty times the
rest, the optimizer used to return a single-game NFL lineup, which
DraftKings rejects at upload.

Still unverified at source: both NBA pairs and both NHL pairs. Each
carries a note in the app naming what specifically is unchecked.

## 1b. The first forward record, and what reading it exposed

One MLB slate locked and scored. The loop works. Reading the output
found four things.

**Locked projections were being overwritten.** The projections table
upserted on conflict without regard for `locked_at`, so re-opening a
locked slate re-ran the engine and replaced the forecast in place.
Calibration then scored a number written *after* the games, built on
history the original never had -- a look-ahead leak into the one
measurement whose entire purpose is to be free of one. Nothing errored;
the skill figure simply moved. This is the serious one.

**The panel ignored the site selector.** It read every slate for the
sport, so a DraftKings record appeared under a FanDuel heading with
only a small scope cell to say otherwise. Filtered now, with the
by-sport table still spanning every pair so nothing is lost.

**The best-looking positions were players who scored nothing.** A
group where every actual is identical has no ordering to get right, so
its skill collapses to whether the model's mean sits nearer that one
number than the site average does -- for a group of zeros, to
projecting lower. Two such groups topped a real position table and
read as the model's strongest. They are flagged now, the count of
zeros is a column, and skill is reported again over players who
actually scored.

**A single slate was read as a track record.** The verdict gated on
projection count alone, and one MLB pool is most of a thousand
projections by itself. Slates are counted now, and below five the
verdict says what the record does and does not establish.

## 1c. Lineups DraftKings rejected

Nine of twenty uploaded lineups came back with "not in a valid roster
position", every named player a multi-position one.

Positions were stored once per player and overwritten by whichever file
was uploaded last, so a FanDuel MLB file rewrote what DraftKings had
said about the same man. Eligibility expansion then read those
positions and added slots the slate never offered. Measured on the two
real files: uploading the FanDuel export widened DraftKings eligibility
for **116 of 942 players** — Oneil Cruz listed at outfield only, given
shortstop; a catcher given first base and outfield — and DraftKings
rejects those at upload.

Fixed in two places. Expansion is now seeded from what the site
published for *this* slate rather than from the player's accumulated
positions, which repairs slates already stored. And positions are now
kept per slate, so the two sites stop overwriting each other at all;
that one also corrects the position column on screen and the
by-position calibration breakdown, both of which had been reading
whichever file was uploaded most recently.

Checked by building twenty lineups from the real DraftKings pool with
the FanDuel file loaded alongside, then validating every placement
against the export's own Roster Position column: zero invalid.

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

## 4. Line and lineup confirmation — entered by hand, not collected

**The controls exist.** *Who is playing* takes confirmed starters at the
one-per-team positions (pitchers, goalies) as a whitelist, plus players
ruled out and players to force in. The optimizer already honoured locks
and bans; nothing had ever exposed them, so until now a pitcher who was
not starting could not be kept out of a lineup at all.

**Baseball is collected.** *Refresh starters from MLB* reads announced
pitchers and posted batting orders from statsapi.mlb.com for the slate's
date, so games that post after the salary file was exported are picked
up without re-downloading it.

What is still missing is collecting them for the other sports. Confirmed NBA lineups, NHL
line combinations and starting goalies, and NFL inactives all land
shortly before lock and all move projections more than any model
refinement would. No free feed covers them reliably, so they stay
manual.

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
