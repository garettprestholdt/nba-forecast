# Roadmap

Phases 0–3 are the MVP: get a basic model live early in the 2026–27 season so the public
scorecard covers as much of it as possible. Everything after that is improvement.

## Phase 0: Foundations
- [ ] uv project, ruff, pytest, pre-commit, `CLAUDE.md`
- [ ] CI on every PR (lint, format, tests); `main` protected so merging requires green CI
- [ ] Public GitHub repo; the "one button" flow is branch → PR → CI → Merge

## Phase 1: Historical data
- [ ] Backfill game results 2004-05 → 2025-26 with `nba_api` (run locally, since stats.nba.com
      blocks cloud IPs) into `data/raw/games.parquet`
- [ ] dbt staging models + data tests (unique game IDs, two teams per game, no ties, sane counts)
- [ ] Flag unusual seasons (2011 lockout, 2019-20 bubble, 2020-21 shortened season)

## Phase 2: Elo baseline + honest backtest
- [ ] Elo with home-court advantage, margin-of-victory multiplier, between-season regression
- [ ] Walk-forward backtest: tune on 2004–2018, report held-out results on 2021–2026
- [ ] Log-loss, Brier score (reliability/resolution), accuracy, calibration curve vs. baselines

## Phase 3: Daily pipeline (MVP)
- [ ] Daily GitHub Actions run: results (NBA CDN, ESPN fallback), schedule, odds (The Odds API)
- [ ] Append predictions with `predicted_at` + git SHA; score finished games
- [ ] dbt tests gate the run: a failure means nothing is committed
- [ ] CI check that every prediction was made before tip-off

## Phase 4: Public site
- [ ] Evidence.dev on GitHub Pages: today's games, ratings, scorecard, calibration, methodology

## Phase 5: Agent layer
- [ ] Daily recap agent (opens a PR with `recaps/YYYY-MM-DD.md`)
- [ ] Failure-triage agent (reads failed run logs, opens a fix PR)
- [ ] Injury-report extraction (official PDF → structured data, validated against box scores)
- [ ] Monthly source-scout agent (evaluates candidate APIs from
      [public-apis](https://github.com/public-apis/public-apis) and proposes an adapter)

## Phase 6: Better model
- [ ] Bayesian dynamic team-strength model (offense/defense random walk, rest, travel, injuries)
- [ ] Champion vs. challenger: shadow predictions, promote only on backtest + live wins

## Phase 7: Write-up
- [ ] README with architecture diagram, live link, headline results, reproduction steps
- [ ] Methodology write-up
