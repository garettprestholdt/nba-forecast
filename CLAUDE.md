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

## Data layout
- **Raw and large data never goes in git.** It lives in `config.local_data_dir()`
  (default `~/Desktop/nbaforecast`, override with `NBAF_DATA_DIR`).
- **Only small published outputs are committed**, under `data/published/` (predictions log,
  current ratings, scorecard). Pre-commit rejects any file over 500 KB.

## Rules
- **Never push to `main`.** Work on a branch and open a PR; CI must pass before merge.
- **No `Co-Authored-By` trailers** in commit messages.
- **Predictions are append-only.** Never edit or delete rows in `data/published/predictions/`. A prediction
  is only valid if its `predicted_at` is before the game's tip-off.
- **No network calls in tests.** Parsers are tested against saved JSON fixtures in `tests/fixtures/`.
- **Every data source sits behind the `ingest/` interface**, so a source can be swapped or added
  without touching models.
- **No leakage.** A feature for a game may only use data timestamped before that game's tip-off.
- Keep dependencies minimal; add one only when a phase needs it.
- This is a forecasting and calibration study, not betting advice. Keep README/site wording neutral.
