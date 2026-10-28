# CLAUDE.md

Daily NBA win probabilities: ingest results and odds → update ratings → publish probabilities
before tip-off → score them publicly. Read the map before opening code:
**[docs/architecture.md](docs/architecture.md)** (layout, layers, data flow, component notes,
how to add a source / model / step / page). Commands to rebuild everything:
**[docs/reproduce.md](docs/reproduce.md)**. Model decisions: `docs/findings/`.

## Where things are
`src/nbaforecast/`: `ingest/` (all network access) · `models/` (pure math) · `pipeline/`
(daily jobs; `pipeline/live.Store` owns the data-branch layout) · `agents/` (Claude agent jobs)
· `research/` (local backtests) · `games/` (Cold Case) · `web/` (static site, one module per
page type) · `cli/` (`nbaf`, one module per area). `tests/test_architecture.py` enforces that
imports only point down these layers.

## Commands
- `make check` (lint + tests, what CI runs) · `make help` lists every stage
- Single steps: `uv run nbaf <command> --help`; the daily job's steps are in `cli/daily.py`
- Local site: `make site` (checks out `origin/data` into `.data-branch/`, builds `_site/`)
- dbt on the CI fixture: copy `tests/fixtures/nbaf_data` somewhere temporary, point
  `NBAF_DATA_DIR` at it, add `--vars '{check_season_counts: false}'`
- Agent shells may lack `uv`/`gh` on PATH: `export PATH="/opt/homebrew/bin:$PATH"`

## Data layout
- **Raw and large data never goes in git**: `config.local_data_dir()` (`NBAF_DATA_DIR`,
  default `~/Desktop/nbaforecast`). Pre-commit rejects files over 500 KB.
- `data/published/` on `main`: reviewed outputs (backtests, model files, Cold Case bank).
- The `data` branch: the public log, written only by the daily workflow. **Never rewrite it.**

## Rules
- **Never push to `main`.** Branch → PR → green CI → the maintainer merges.
- **No Claude/Anthropic `Co-Authored-By` trailers, no `claude[bot]` commits.** A commit-msg hook
  and CI (`python -m nbaforecast.attribution`) enforce it.
- **Append-only predictions**, each made before tip-off; `nbaf verify` gates every commit.
- **No leakage.** A feature for a game uses only data from before its tip-off. Player features
  may use who was *available* (played or healthy scratch), never who got minutes.
- **No network calls in tests.** Parsers are tested against saved JSON in `tests/fixtures/`.
- **Every data source goes through `ingest/`.** stats.nba.com / cdn.nba.com reject non-browser
  clients; don't try to get around that. ESPN JSON is the source.
- **Agents never commit.** They draft into an isolated folder; our code checks the draft and the
  workflow commits as `github-actions[bot]`. Recaps must pass `recap.check_recap` (lexical: every
  number must exist in the facts, of the same kind; it can't prove a sentence true).
- **Promotion bars are pre-registered** (`docs/promotion-bar-*.md`). Never change a rule, window
  or threshold after seeing live results; only append to its decision record. A change that
  alters predictions is a new model version with its own window.
- Changing a prompt, `agents/recap.py`, `agents/availability.py`, `agents/triage.py` or
  `scripts/run_agent.sh` triggers that agent's dry-run workflow on the PR; it must pass.
- Third-party metrics (DARKO, EPM, LEBRON) aren't republished: their terms don't clearly allow it.
- Keep dependencies minimal. Times on the site are Central. Wording stays neutral: a
  forecasting and calibration study, not betting advice.
