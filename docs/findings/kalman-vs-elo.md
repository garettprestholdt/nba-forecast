# Finding: rest matters; the model family doesn't

*Phase 6 challenger backtest. Tables: [data/published/challenger](../../data/published/challenger/summary.json).
Reproduce with `uv run nbaf challenger`.*

## Question
Would a Bayesian team-strength model beat the live Elo? The candidate is a Kalman filter on point
margins, with rating uncertainty, a drifting home court, and learned rest and travel effects.

## Setup
- **Kalman model** (`models/kalman.py`): one strength per franchise (points, with full
  covariance) drifting day to day and partly reset between seasons; a drifting home-court term;
  coefficients for each team's back-to-back and the travel-distance gap. Win probability
  includes the state's uncertainty.
- **Elo + back-to-back**: the live Elo plus a single penalty for a team playing its second game in
  two nights, to test whether the Kalman model's edge comes from the model or the features.
- Settings were chosen by log-loss on **2005-06 to 2018-19** only (Kalman: a two-stage grid;
  Elo: the penalty from 0 to 80 Elo points). Each was then scored on **2020-21 to 2025-26**.

## Result (held out, 7,776 games)
| Model | Log-loss | Accuracy | Calibration error |
|---|---|---|---|
| Elo, live model | 0.6254 | 65.0% | 0.00013 |
| **Elo + back-to-back** (35 Elo pts ≈ 1.2 points) | **0.6232** | 65.1% | **0.0001** |
| Kalman + rest/travel | 0.6231 | 65.4% | 0.00029 |
| Kalman, no rest/travel | 0.6256 | 65.1% | 0.00029 |

Paired, game-by-game log-loss differences (95% intervals):
- Live Elo − Elo + back-to-back: **+0.0022 (+0.0012 to +0.0033)**, so the penalty clearly helps.
- Elo + back-to-back − Kalman: **+0.0001 (−0.0018 to +0.0020)**, a tie.
- Without rest and travel, the Kalman model ties the live Elo.

## Interpretation
- **Back-to-backs are the whole gain.** Both models learn about the same effect: ~1.8 points of
  margin in the Kalman model, 35 Elo points in Elo.
- **Travel distance adds almost nothing** (0.08 points per 1,000 km) once back-to-backs are known.
- **The Kalman model's extras (tracked uncertainty, drifting home court) add no measurable
  accuracy**, and its calibration is slightly worse: the home-court drift tuned on the training
  seasons is too slow to follow the decline that Elo's rolling window tracks.
- Caveat: the 2020-26 seasons already chose the live Elo in Phase 2, so this isn't a perfectly
  clean test. Elo + back-to-back's penalty, though, was tuned on training seasons only.

## Next
Team history alone has hit diminishing returns. The next model mixes team-level history with
player-level data (who is actually playing tonight, and how good they are).
