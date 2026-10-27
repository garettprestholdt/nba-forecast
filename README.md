# NBA Forecast

Daily NBA win probabilities, published before tip-off and scored in public against the betting
market. Every prediction is committed to git with a timestamp, so the track record can't be
edited after the fact.

**Live site: [garettprestholdt.github.io/nba-forecast](https://garettprestholdt.github.io/nba-forecast/)**

> **Status:** Phase 4. Predictions publish every morning to the
> [`data` branch](https://github.com/garettprestholdt/nba-forecast/tree/data) and the site
> rebuilds after each run. See the [roadmap](docs/roadmap.md).

## Results so far
On 7,776 held-out games (2020-21 to 2025-26), Elo predicts the winner **65.0%** of the time
with log-loss **0.625**, against 0.693 for a coin flip. The backtest also found that home-court
advantage has shrunk enough to miscalibrate a fixed-bonus model:
[write-up](docs/findings/elo-backtest.md).

## How it works
1. **Ingest:** last night's results, today's schedule, and market odds (GitHub Actions, daily).
2. **Model:** update team ratings (Elo baseline, Bayesian challenger later).
3. **Predict:** append today's win probabilities with a `predicted_at` timestamp.
4. **Score:** log-loss, Brier score, and calibration vs. the market and simple baselines.
5. **Publish:** a static site on GitHub Pages (agent-written daily recaps come in Phase 5).

## Data
Raw and historical data stays local (outside the repo, set by `NBAF_DATA_DIR`) and is never
pushed. Small outputs live in git:
- the [`data` branch](https://github.com/garettprestholdt/nba-forecast/tree/data): the daily
  predictions log (append-only, force-pushes blocked), results, scorecard, and Elo state
- `data/published/` on `main`: backtest summaries

## Stack
Python · uv · DuckDB + Parquet · dbt · pytest · ruff · GitHub Actions · GitHub Pages · Jinja2 · Vega-Lite

## Development
```bash
uv sync
uv run pytest
uv run ruff check .
```

*A forecasting and calibration study, not betting advice.*
