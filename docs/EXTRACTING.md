# History

This project was built inside `anyenya-wq/prediction-market-oracle`,
alongside Project Oracle, because the GitHub App running the session
could not create a repository (`403 Resource not accessible by
integration`). It lived under `dfs-edge/` there and was written to lift
out cleanly: its own `requirements.txt`, tests, workflows and
`.gitignore`, and no imports reaching outside its own directory.

It was extracted with `git subtree split --prefix=dfs-edge`, so the
commits here are the ones that touched those files rather than a fresh
`Initial commit`. The full development history -- every step from the
first sport config to the calibration harness -- remains in the
original repository on `claude/daily-fantasy-sports-tool-b761h0`.

Two things changed in the move, both consequences of the subtree
becoming the repository:

- `app.py` sits at the root. In the parent it was `dfs-edge/app.py`,
  and the root's `app.py` was Project Oracle's -- a genuine trap, since
  `streamlit run app.py` from the wrong directory started the wrong
  application with no error to say so.
- The workflows lost their `dfs-edge/` path filters and their
  `working-directory`, and are named for what they do rather than for
  which subtree they watched.
