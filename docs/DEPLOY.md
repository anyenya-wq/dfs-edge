# Deploying the board

The app is stateless. Every slate, projection, lock and result lives in
PostgreSQL, so the process that renders the page can be replaced at any
time without losing anything. That is what makes hosting it a redeploy
rather than a migration.

## Why not a Codespace

A Codespace works, but it is a development machine, not a host. It
sleeps after about thirty minutes idle, forwards a port only while a
process is listening on it, and needs someone to start that process by
hand every time. Five separate failures came out of that arrangement --
a wrong working directory, a missing driver, a stale module cache,
missing proxy flags, and a stopped process -- none related to each
other, and none of them bugs in the app.

## Streamlit Community Cloud

Free, always on, one fixed URL.

1. Push the branch you want to serve.
2. At <https://share.streamlit.io>, **Create app** → **Deploy a public
   app from GitHub**.
3. Fill in:
   - **Repository**: `anyenya-wq/dfs-edge`
   - **Branch**: `main`
   - **Main file path**: `app.py`
4. Open **Advanced settings** → **Secrets** and paste:

   ```toml
   APP_PASSWORD = "something long that you have not used elsewhere"
   DFS_DATABASE_URL = "postgresql://...your Neon connection string..."
   ```

   Optionally `ANTHROPIC_API_KEY` as well, which turns on the research
   briefs. Leave it out and everything else still works.
5. **Deploy**. The first build installs `requirements.txt` and
   takes a few minutes.

Secrets can be edited afterwards from the app's ⋮ menu → **Settings** →
**Secrets**. Changing them restarts the app.

## This repository is public

The code is public; the app is not. Streamlit's free tier allows one
private app per workspace, and Project Oracle holds that slot in the
repository this was extracted from -- public repositories get unlimited
public apps, which is why this one is public.

Nothing secret is committed here. Every credential lives in Streamlit's
Secrets or in GitHub's, and the database and salary exports are ignored
by git. What a reader can see is how the projections are built, which
is not the edge: the edge is the data behind them and the discipline of
scoring what was forecast.

## The password

Community Cloud serves free apps at a public URL, so the board gates
itself: `require_password` in `app.py` renders nothing until the
password matches, before any database connection is opened or any key
is used.

Set `APP_PASSWORD` and the page is useless to anyone without it. Leave
it unset **on a deployment** and the app says so in an orange banner
rather than failing silently -- a page left open is a page where anyone
can read your forecasts, resolve your slates, or spend your Anthropic
credit.

Locally there is no gate. Nothing is exposed, and requiring a password
to run the app on your own machine would add a step to every run for
nothing.

What this is and is not: one shared password, checked in constant time,
remembered for the browser session. It keeps a public URL private. It
is not user accounts, and it does not survive a password change for
anyone already signed in until their session ends.

## Keeping the record

`DFS_DATABASE_URL` must point at PostgreSQL. With SQLite the file lives
on an ephemeral filesystem and is erased whenever the app restarts,
taking every locked slate and recorded result with it -- and nothing
errors when that happens, so the sidebar names the backend in use on
every page.

## The nightly scoring job

Separate from the app. `.github/workflows/resolve.yml` scores locked
slates whose games have finished, and needs `DFS_DATABASE_URL` as a
**repository secret** (Settings → Secrets and variables → Actions).
GitHub runs scheduled workflows only from the default branch, which is
where this one lives.
