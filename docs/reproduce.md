# Reproducing the results

Every number in `docs/findings/` and `data/published/` comes from the commands below, run in
order. Each stage is one `make` target; `make reproduce` runs them all.

**Requirements:** Python 3.12+ and [uv](https://docs.astral.sh/uv/). All dependencies are
pinned in `uv.lock`. Raw data goes to `$NBAF_DATA_DIR` (default `~/Desktop/nbaforecast`) and
never into the repo; ESPN's public JSON is the only data source the history needs.

| Stage | Command | Time (first run) | Writes |
|---|---|---|---|
| 1. Setup | `make setup` | 1 min | `.venv/`, git hooks |
| 2. History | `make data` | ~4 h | games since 2004-05, box scores, rebuilt blank box scores, the dbt warehouse (all tests must pass) |
| 3. Play-by-play | `make plays` | ~45 min | full play-by-play for 2017-18 to 2025-26 (for RAPM) |
| 4. Backtests | `make backtests` | ~25 min | `data/published/{backtest,challenger,hybrid,impact_challenger}/` |
| 5. Model files | `make models` | ~5 min | `data/published/hybrid*/model.json`, season-start ratings, the Cold Case bank |
| 6. Checks | `make check` | 1 min | lint, format, 340+ tests |

After stage 5, `git diff data/published/` should show only the `generated_at` / `created_at`
timestamps changing: every number matches the committed files. Re-running any stage is safe;
finished seasons and cached games are skipped.

## What's deterministic, and what isn't
- **Deterministic:** the warehouse (dbt tests check exact game counts), every backtest
  (walk-forward fits, no random splits), the season simulations (`seed=0`), the Cold Case bank
  (`seed=2026`), and the site build (the same inputs give byte-identical pages).
- **Not reproducible after the fact, by design:** anything that depended on the moment it was
  made. The daily predictions, betting lines, injury reports and agent answers are logged on the
  `data` branch with timestamps instead. That log is the record; `nbaf verify` guarantees no
  row changes after it's written and no prediction lands after tip-off.
- **Upstream drift:** ESPN occasionally corrects old box scores. Known source errors are pinned
  in `dbt/seeds/known_data_issues.csv` and `dbt/seeds/player_id_aliases.csv`, so a correction
  shows up as a failing data test, not a silent change.

## The live system
The daily job (`.github/workflows/daily.yml`, 10am Central, with backups at 11:30 and 1pm that
run only if no run has succeeded that day) runs the same code against a checkout
of the `data` branch: `nbaf daily` (results, Elo, predictions), `shadow` and `shadow-v2` (the
challenger models), `box-sync`, `impact`, `venues`, `project`, `rosters`, the agent jobs, and
`verify` before anything is committed. Any of these can be run locally against a checkout:

```bash
git worktree add --detach .data-branch origin/data
uv run nbaf verify --data-dir .data-branch
make site            # builds _site/ from that checkout
```

### Starting a fresh data branch (one time)
These created the first live state and are kept for anyone forking the project:
`nbaf bootstrap --out <dir>` (Elo state), `nbaf shadow-bootstrap` (hybrid state),
`nbaf court-backfill --season <last season>` (shots and stints), `nbaf careers` (player
histories), `nbaf season-start` (opening-night ratings).
