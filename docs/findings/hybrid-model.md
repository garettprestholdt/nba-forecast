# Finding: players add real signal to team history, and knowing who's out adds as much again

*Phase 6 hybrid backtest. Tables: [data/published/hybrid](../../data/published/hybrid/summary.json).
Reproduce with `uv run nbaf hybrid` (~12 min, needs the local box scores).*

## Question
Elo only sees final scores. Does knowing *who* plays — player production, plus-minus, roster
turnover, and absences — improve win probabilities beyond team history? And how much of the gain
needs game-day lineup information?

## Setup
**Team history**:
- the live Elo, split into team strength and Elo's adaptive home-court estimate;
- back-to-back flags;
- exponentially weighted box-score form: net rating and the four factors (shooting, turnovers,
  offensive rebounding, free throws), home minus away.

**Player layer** (`models/players.py`), walked date by date so every feature is pre-game:
- **production**: Game Score per minute, shrunk toward a replacement-level player;
- **impact**: plus-minus per minute (recorded from 2008-09), shrunk toward 0.

Each team's strength is its players' values weighted by projected minutes and scaled to 240
minutes. It comes in two versions:
- **Realistic**: the whole roster, weighted by recent availability (minutes per team game,
  counting games missed as 0). Uses nothing from game day.
- **Knows the lineup**: only players *available* for the game, weighted by their usual
  minutes. Available means they played or sat by the coach's choice; injured, resting, and
  suspended players are excluded. This is what an injury report plus the inactive list tells you
  before tip-off. It never uses who got minutes or how many: an early version did, and garbage
  time leaked the result in (worth about 0.002 of log-loss).

**Stack**: a ridge-penalized logistic regression on those features, fit walk-forward. Each season
is predicted by a model trained only on earlier seasons. There is no free intercept: home
advantage enters only through Elo's adaptive estimate. A fixed intercept learned on older seasons
overstated the home edge after 2020 by about 2 points of win probability.

**Tuning**: on walk-forward predictions for 2007-08 to 2018-19 only, in stages: regression,
player memory and shrinkage, projected minutes, team form. 30 settings in all
([tuning.csv](../../data/published/hybrid/tuning.csv)). Then every model was scored once on
**2020-21 to 2025-26**.

## Result (held out, 7,776 games)
| Model | Log-loss | Brier | Accuracy |
|---|---|---|---|
| Elo, live model | 0.6254 | 0.2180 | 65.0% |
| Elo + back-to-back | 0.6232 | 0.2170 | 65.1% |
| Team stack (Elo + rest + box-score form) | 0.6227 | 0.2168 | 65.4% |
| **Hybrid, realistic** | **0.6172** | **0.2142** | **66.0%** |
| Hybrid, knows the lineup | 0.6098 | 0.2112 | 66.8% |
| Players only, knows the lineup | 0.6122 | 0.2122 | 66.5% |

Paired, game-by-game log-loss differences (95% intervals):
- **Elo + back-to-back − hybrid (realistic): +0.0060 (+0.0029 to +0.0091).** Players help with no
  game-day information at all.
- Team stack − hybrid (realistic): +0.0055 (+0.0026 to +0.0085). Box-score form adds little on
  top of Elo; the gain is from the players.
- **Hybrid (realistic) − hybrid (knows the lineup): +0.0074 (+0.0051 to +0.0097).** Knowing who's
  available is worth more than everything else combined.

By season, the realistic hybrid beats Elo + back-to-back in 5 of 6 held-out seasons. 2021-22, a
season of COVID absences, is a tie (0.6309 vs. 0.6304). Knowing the lineup wins all 6.

### Where knowing the lineup helps
Held-out games bucketed by how far the available players' strength falls from the usual roster's:

| Missing talent | Games | Realistic | Knows the lineup |
|---|---|---|---|
| Bottom half | 3,888 | 0.6122 | 0.6119 |
| 50th–80th percentile | 2,333 | 0.6088 | 0.6066 |
| 80th–95th | 1,166 | 0.6296 | 0.6090 |
| Top 5% | 389 | 0.6801 | 0.6111 |

In half the games, lineup information changes nothing. In the 5% with the biggest absences, the
realistic model is barely better than a coin flip (0.680 vs. 0.693). Knowing the lineup brings
those games back to normal.

### What the model leans on
Standardized coefficients, from the fit used for 2025-26:

| Feature | Coefficient |
|---|---|
| Elo strength | 0.33 |
| Player production | 0.30 |
| Shooting edge (eFG) | 0.15 |
| Player impact (plus-minus) | 0.12 |
| Offensive rebounding | 0.10 |
| Home court (Elo's adaptive estimate) | 0.10 |
| Turnovers | 0.08 |
| Back-to-backs | about ±0.1 |
| Elo × roster turnover | 0.06 |

Net rating and free-throw rate add nothing beyond these. Tuning preferred a long memory for player
quality (each game keeps 99.5% of its weight per later game) and a short one for availability
(about the last two team games): talent is stable, and who's healthy changes fast.

## Caveats
- **Calibration:** close, but slightly overconfident at the extremes. Forecasts of 80–90% won 81%
  of the time; reliability is 0.0005 vs. Elo's 0.0001. Worth watching live.
- **Edges of the search:** two settings sit at the edge of the search (player memory, availability
  memory). Widening once improved training log-loss by only 0.0007, so I stopped there.
- **Older seasons:** in 2008-12, ESPN doesn't list players who sat, so in those training seasons
  "available" can only mean "played". The held-out seasons all have DNP reasons.
- **Live use:** the realistic hybrid needs player state carried day to day, as Elo does. It also
  needs each night's box scores, which the daily job already fetches for injury scoring. The
  lineup version needs availability before tip-off; the injury agent's calls are the start of
  that.
- **Market comparison:** historical betting lines aren't available, so the market comparison is
  live only.
