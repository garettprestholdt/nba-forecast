# CLAUDE.md

Guidance for Claude (and other agents) working in this repo.

## What this is
A daily NBA forecasting pipeline: ingest results/schedule/odds → update team ratings → publish
win probabilities before tip-off → score them publicly against the betting market.
See `docs/roadmap.md` for the phased plan.

## Commands
- Install: `uv sync`
- Lint + format: `uv run ruff check . && uv run ruff format .`
- Tests: `uv run pytest`
- CLI: `uv run nbaf --help`
- Backfill history (local only): `uv run nbaf backfill` (finished seasons already on disk are skipped)
- dbt: `uv run nbaf dbt build` (sets `NBAF_DATA_DIR` and the project/profile paths for you)
- Backtest: `uv run nbaf backtest` (~4 min; writes `data/published/backtest/`)
- dbt on the CI fixture: copy `tests/fixtures/nbaf_data` somewhere temporary, point
  `NBAF_DATA_DIR` at it, and add `--vars '{check_season_counts: false}'`

## Data sources
- **ESPN scoreboard JSON** (`ingest/espn.py`) for both the backfill and daily results.
  stats.nba.com and cdn.nba.com reject non-browser clients; don't try to get around that.

## Data layout
- **Raw and large data never goes in git.** It lives in `config.local_data_dir()`
  (default `~/Desktop/nbaforecast`, override with `NBAF_DATA_DIR`).
- **Only small published outputs are committed**, under `data/published/` (predictions log,
  current ratings, scorecard). Pre-commit rejects any file over 500 KB.

## Rules
- **Never push to `main`.** Work on a branch and open a PR; CI must pass before merge.
- **No Claude/Anthropic `Co-Authored-By` trailers, and no commits authored by `claude[bot]`.**
  The maintainer doesn't want AI tools in the GitHub contributors list. A commit-msg hook and a
  CI step (`python -m nbaforecast.attribution`) enforce this.
- **Predictions are append-only.** Never edit or delete rows in `data/published/predictions/`. A prediction
  is only valid if its `predicted_at` is before the game's tip-off.
- **No network calls in tests.** Parsers are tested against saved JSON fixtures in `tests/fixtures/`.
- **Every data source sits behind the `ingest/` interface**, so a source can be swapped or added
  without touching models.
- **No leakage.** A feature for a game may only use data timestamped before that game's tip-off.
- Keep dependencies minimal; add one only when a phase needs it.
- This is a forecasting and calibration study, not betting advice. Keep README/site wording neutral.
