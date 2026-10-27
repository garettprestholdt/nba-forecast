# NBA Forecast

Daily NBA win probabilities, published before tip-off and scored in public against the betting
market. Every prediction is committed to git with a timestamp, so the track record can't be
edited after the fact.

> **Status:** Phase 2 (Elo + backtest). See the [roadmap](docs/roadmap.md).

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
5. **Publish:** a static dashboard on GitHub Pages, plus agent-written daily recaps (as PRs).

## Data
Raw and historical data stays local (outside the repo, set by `NBAF_DATA_DIR`) and is never
pushed. Only small published outputs live in git under `data/published/`: the predictions log,
current team ratings, and the scorecard.

## Stack
Python · uv · DuckDB + Parquet · dbt · pytest · ruff · GitHub Actions · Evidence.dev · Claude

## Development
```bash
uv sync
uv run pytest
uv run ruff check .
```

*A forecasting and calibration study, not betting advice.*
