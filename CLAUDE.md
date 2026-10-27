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
- Player box scores (local only, ~3 hours for every season): `uv run nbaf boxscores`; for the current
  season it fetches only games not already on disk. Plus-minus is null where ESPN didn't record it
  (before 2008-09). About 13% of 2012-18 team box scores have the roster but no stats at ESPN;
  staging flags them `stats_missing` with null stats. ESPN source errors found by the box-score
  tests go in `seeds/known_data_issues.csv`; duplicate or missing player ids go in
  `seeds/player_id_aliases.csv`.
- dbt: `uv run nbaf dbt build` (sets `NBAF_DATA_DIR` and the project/profile paths for you)
- Backtest: `uv run nbaf backtest` (~4 min; writes `data/published/backtest/`)
- Daily job (what GitHub Actions runs): `uv run nbaf daily --data-dir <data-branch checkout>`,
  then `uv run nbaf verify --data-dir <same>`
- First live state (local, once): `uv run nbaf bootstrap --out <empty dir>`
- Recap (what the daily workflow runs): `uv run nbaf recap-prepare --data-dir <data> --workdir <tmp>`,
  then Claude Code writes `<tmp>/recap_draft.md` (prompt: `prompts/recap.md`), then
  `uv run nbaf recap-publish --data-dir <data> --workdir <tmp>`
- Site: `uv run nbaf site --data-dir <data-branch checkout> --out _site` (GitHub Actions
  deploys it to Pages after every daily run and every merge to main)
- Charts: Vega-Lite specs in `web/__init__.py` use color tokens (`@series-1`) that
  `static/charts.js` resolves from CSS custom properties. Field names must not contain
  `' . [ ]` (Vega path syntax); `tests/test_web.py` enforces this.
- dbt on the CI fixture: copy `tests/fixtures/nbaf_data` somewhere temporary, point
  `NBAF_DATA_DIR` at it, and add `--vars '{check_season_counts: false}'`

## Data sources
- **ESPN scoreboard JSON** (`ingest/espn.py`) for both the backfill and daily results.
  stats.nba.com and cdn.nba.com reject non-browser clients; don't try to get around that.

## Data layout
- **Raw and large data never goes in git.** It lives in `config.local_data_dir()`
  (default `~/Desktop/nbaforecast`, override with `NBAF_DATA_DIR`).
- **Only small published outputs are committed.** On `main`: `data/published/` (backtest
  summaries). The daily predictions, results, scorecard, and Elo state live on the separate
  `data` branch, written only by the daily workflow. Pre-commit rejects any file over 500 KB.
- **Never rewrite the `data` branch.** It's the public, timestamped prediction log.

## Rules
- **Never push to `main`.** Work on a branch and open a PR; CI must pass before merge.
- **No Claude/Anthropic `Co-Authored-By` trailers, and no commits authored by `claude[bot]`.**
  The maintainer doesn't want AI tools in the GitHub contributors list. A commit-msg hook and a
  CI step (`python -m nbaforecast.attribution`) enforce this.
- **Predictions are append-only.** Never edit or delete rows in the `data` branch's `predictions/`. A prediction
  is only valid if its `predicted_at` is before the game's tip-off.
- **No network calls in tests.** Parsers are tested against saved JSON fixtures in `tests/fixtures/`.
- **Every data source sits behind the `ingest/` interface**, so a source can be swapped or added
  without touching models.
- **No leakage.** A feature for a game may only use data timestamped before that game's tip-off.
- Keep dependencies minimal; add one only when a phase needs it.
- **Agents never commit.** An agent writes a draft into an isolated folder; our own code checks
  it and commits as `github-actions[bot]` to the `data` branch. Recaps must pass
  `recap.check_recap` (numbers and teams grounded in the facts file, no betting language).
  The check is lexical, not semantic: every number must exist in the facts, and percentages,
  decimals, and counts ("N games", "N of M") must match numbers of the same kind. It can't
  prove a sentence is true, so keep the prompt asking for copied, not computed, numbers.
- Injury calls (daily workflow): `nbaf availability-prepare` scores yesterday's calls against
  box scores and writes today's candidates to `<workdir>/agent/`; the agent writes
  `availability.json` there; `nbaf availability-publish` validates and appends to the
  append-only `availability/<season>.csv`. The keyword rule's answers stay outside `agent/`.
- Triage: `triage.yml` runs on a failed Daily predictions / Publish site run. Agent script:
  `scripts/run_agent.sh` (shared; `AGENT_TOOLS`, `AGENT_MODEL`, `AGENT_OUTPUT`). Triage issues
  carry the `triage` label; fix them on a normal branch + PR.
- Changing the prompt, `recap.py`, or the agent script triggers `recap-test.yml` on the PR:
  the real agent runs on a throwaway copy of the data branch and the check must pass.
- This is a forecasting and calibration study, not betting advice. Keep README/site wording neutral.
