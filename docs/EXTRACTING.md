# Moving this into its own repository

This project was built inside `prediction-market-oracle` because the
GitHub App running the session could not create a repository
(`403 Resource not accessible by integration`). It is self-contained —
its own `requirements.txt`, tests, CI workflow, and `.gitignore`, and no
imports reaching outside `dfs-edge/` — so it lifts out cleanly.

## Option 1: keep the commit history for these files

`git subtree split` rewrites just this subtree's history into a new
branch whose root is `dfs-edge/`:

```bash
cd prediction-market-oracle
git subtree split --prefix=dfs-edge -b dfs-edge-only

# Create the empty repo on GitHub first, then:
cd ..
git clone --no-local --single-branch --branch dfs-edge-only \
    prediction-market-oracle dfs-edge
cd dfs-edge
git branch -m dfs-edge-only main
git remote set-url origin https://github.com/<you>/dfs-edge.git
git push -u origin main
```

## Option 2: start clean

Simpler if the history is not worth keeping:

```bash
cp -r prediction-market-oracle/dfs-edge ~/dfs-edge
cd ~/dfs-edge
git init && git add . && git commit -m "Initial commit"
git remote add origin https://github.com/<you>/dfs-edge.git
git push -u origin main
```

## Afterwards

Delete `dfs-edge/` from `prediction-market-oracle` so the prediction
market project goes back to being about prediction markets, and delete
the two workflows that exist only to reach into this subdirectory:

- `.github/workflows/dfs-edge-tests.yml`
- `.github/workflows/dfs-edge-sources.yml`

Their replacements are already here and become active automatically once
this directory is a repository root, since that is the only place GitHub
reads workflows from:

- `.devcontainer/devcontainer.json` — the Codespaces configuration
- `.github/workflows/tests.yml` — the test suite

The one thing that does not carry over is the scheduled upstream check.
`dfs-edge-sources.yml` runs the network-gated tests weekly against the
real data feeds, which is what turns a silent upstream change — nflverse
renaming a release, a field disappearing from a box score — into a red
tick rather than a slow decline in accuracy. Copy it across as
`.github/workflows/sources.yml`, dropping the `working-directory` and
`cache-dependency-path` prefixes.
