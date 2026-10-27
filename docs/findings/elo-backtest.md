# Finding: home-court advantage drifted, and a fixed Elo bonus can't follow it

*Phase 2 backtest, 28,416 games from 2004-05 to 2026-27. Full tables: [data/published/backtest](../../data/published/backtest/README.md).*

## Setup
- FiveThirtyEight-style Elo (K-factor, home-court bonus, margin-of-victory multiplier,
  between-season regression to the mean). Every date is predicted from start-of-day ratings,
  so no prediction ever sees a game that might not have finished yet.
- 140 configurations tuned by log-loss on **2005-06 to 2018-19** (2004-05 is burn-in).
- Scored once on the held-out **2020-21 to 2025-26** seasons (7,776 games). 2019-20 is skipped.

## Result
| Model (held out) | Log-loss | Brier | Accuracy | Mean forecast | Home win rate |
|---|---|---|---|---|---|
| Elo, chosen on training data (fixed home court = 80) | 0.6303 | 0.2200 | 64.7% | 60.0% | 55.4% |
| Elo, adaptive home court (2-season window) | **0.6254** | **0.2180** | **65.0%** | 54.9% | 55.4% |
| Previous season's home win rate | 0.6878 | 0.2473 | 55.4% | 55.4% | 55.4% |
| Coin flip | 0.6931 | 0.2500 | 50.0% | 50.0% | 55.4% |

On the training seasons the two Elo variants were nearly tied (log-loss 0.6021 fixed vs.
0.6027 adaptive), so the honest procedure picked the fixed one. On the held-out seasons the
fixed bonus is visibly miscalibrated: it forecasts home teams to win 60% of the time when they
won 55%, and every calibration bin is 3 to 5 points too optimistic about the home team.

The Brier decomposition isolates the cause. Both models separate good teams from bad equally
well (resolution 0.0281 vs. 0.0283), but the fixed model's reliability error is **17x larger**
(0.00225 vs. 0.00013). The adaptive bonus fell from the 80-point prior to about 39 Elo points,
tracking the league-wide decline in home win rate from 60.5% (2004-05) to 54.4% (2024-25).

## What this means for the live model
The holdout has now been used, so it can't pick the live model without becoming a biased test.
For the 2026-27 live predictions (Phase 3) we'll use the adaptive home court, and treat **this
season's publicly logged predictions** as the new, untouched out-of-sample test.
