# Finding: defense ratings and who's missing improve the hybrid, but the features are badly collinear

*Challenger backtest, 2018-19 to 2025-26, scored on the held-out 2020-21 to 2025-26 seasons
(7,776 games). Full tables: [data/published/impact_challenger](../../data/published/impact_challenger/).
Code: `impact_backtest.py`, `models/rapm_walk.py`. Status: **awaiting feature selection.***

## Setup
- **Leak-free RAPM.** Every 14 days through each season, RAPM (offense and defense, plain and
  box-score-prior fits, blended as on the site) is refit on the stints of the previous season
  and the current season *before that date* — the same window the live `nbaf impact` step uses.
  Stints come from 10,786 cached play-by-play games (2017-18 to 2025-26). A game only sees the
  latest snapshot dated on or before it.
- **Team features** (home minus away), from the existing player walk's projected minutes:
  - `rapm_o_real`, `rapm_d_real`: the whole roster, weighted by availability (uses nothing
    from game day);
  - `rapm_o_lineup`, `rapm_d_lineup`: only players available tonight, weighted by usual
    minutes (live, from the injury report);
  - `absent_rapm`: the value above replacement of the expected minutes that are missing
    tonight (0 when everyone plays). This is the "Edwards is out" feature.
- Same stack as the hybrid: logistic, no intercept, ridge l2 = 10, team history (Elo strength,
  adaptive home court, back-to-backs, box-score form). RAPM models train from 2018-19, so the
  current hybrid is also refit on that short window to separate better features from less data.

## Results (held out)
| Model | Features | In-sample | Held-out log-loss | vs. hybrid v1 (95% CI) |
|---|---|---|---|---|
| Elo, adaptive (published) | – | – | 0.6254 | |
| Hybrid v1, realistic (current challenger, trained from 2005) | 12 | 0.5970 | 0.6171 | |
| Team stack only (Elo + rest + form) | 9 | 0.6247 | 0.6232 | −0.0061 (−0.0093, −0.0029) |
| Hybrid v1, realistic, short window | 12 | 0.6164 | 0.6160 | +0.0011 (−0.0005, +0.0027) |
| Hybrid v1, knows the lineup | 12 | 0.6116 | 0.6087 | +0.0085 (+0.0056, +0.0113) |
| RAPM, whole roster | 12 | 0.6193 | 0.6181 | −0.0010 (−0.0039, +0.0018) |
| RAPM, tonight's lineup | 12 | 0.6162 | 0.6140 | +0.0031 (−0.0003, +0.0065) |
| RAPM, whole roster + missing value | 13 | 0.6129 | 0.6121 | +0.0050 (+0.0010, +0.0090) |
| **RAPM + production, tonight's lineup** | 13 | 0.6113 | **0.6080** | **+0.0091 (+0.0061, +0.0122)** |
| Everything (all 10 player features) | 19 | 0.6080 | 0.6080 | +0.0091 (+0.0055, +0.0128) |

Positive = better than the current challenger. Every model beats the published Elo.

1. **Knowing who plays is worth more than which rating you use.** Switching v1 from the whole
   roster to tonight's lineup gains 0.0085; RAPM alone, whole roster, is no better than v1.
2. **RAPM on its own is weaker than Game Score production.** RAPM is noisy for most players;
   the box score is stable. The two together are best: RAPM adds defense the box score can't see.
3. **The gain lives in the games with big absences.** Held-out games bucketed by how much
   missing value one side has:

   | Missing value bucket | Games | Elo | Hybrid v1, realistic | RAPM, tonight's lineup |
   |---|---|---|---|---|
   | Bottom half | 3,888 | 0.6241 | 0.6159 | 0.6184 |
   | 50–80% | 2,333 | 0.6186 | 0.6080 | 0.6084 |
   | 80–95% | 1,166 | 0.6330 | 0.6220 | 0.6130 |
   | Top 5% (star-level absences) | 389 | 0.6574 | **0.6697** | **0.6065** |

   In the top 5%, the current challenger is *worse than Elo*: it can't see the absence. The
   lineup model is better there than anywhere else.

## Multicollinearity
Variance inflation factors over the candidate features (2018-19 to 2025-26):

| Feature | VIF | Most correlated with |
|---|---|---|
| `impact_real` / `impact_lineup` (raw plus-minus) | 128 / 121 | each other, r = 0.99 |
| `net_rating` (form) | 65 | `elo_strength`, r = 0.96 |
| `efg_diff` (form) | 42 | `net_rating`, r = 0.86 |
| `elo_strength` | 32 | `elo_x_turnover`, r = 0.94 |
| `rapm_o_real` / `rapm_o_lineup` | 27 / 25 | each other, r = 0.95 |
| `rapm_d_real` / `rapm_d_lineup` | 17 / 16 | each other, r = 0.95 |
| `production_real` / `production_lineup` | 10 / 10 | each other, r = 0.89 |
| `absent_rapm` | 2.0 | – |

The symptoms show up in the coefficients, refit once per season:
- In **Everything**, `rapm_d_lineup` has the wrong sign (−0.09) while `rapm_d_real` is +0.22;
  `rapm_o_real` changes sign between yearly refits (the first of 7 disagrees with the other 6).
  The pair splits one effect.
- `elo_strength` changes sign between refits in the lean models and is *negative* on average, because
  `elo_x_turnover` (r = 0.94) and `net_rating` (r = 0.96) carry the same information.
- In the lean **RAPM + production, tonight's lineup** model, every player coefficient keeps its
  sign in every refit (production +0.35, RAPM defense +0.20, RAPM offense +0.15).

## Overfitting
- **No gap between in-sample and held-out loss** (Everything: 0.6080 vs. 0.6080), so ridge at
  l2 = 10 is already enough for prediction; the problem is interpretability, not fit.
- **Ridge sweep** on Everything: held-out log-loss 0.6083 (l2 = 0.1), 0.6082 (1), 0.6080 (10),
  **0.6075 (100)**, 0.6093 (1,000). Flat until heavy shrinkage.
- **The bigger overfitting risk is us.** These held-out seasons have now been looked at for 9
  candidates. Picking the best of them on this table is selection on the test set; the honest
  check of whatever we choose is the live shadow window, pre-registered like the v1 bar.

## Example: Anthony Edwards out
ESPN lists Edwards as Out. His live RAPM is about +2.8 offense, −0.9 defense: 3.4 points per
100 above replacement over ~35 minutes, so about 2.5 points of missing value. With the
whole-roster-plus-missing-value model's coefficient (−0.25 per SD, SD ≈ 2.2), that's about
−0.29 on the log-odds: a 73% Minnesota win becomes about 67%. Elo and v1 don't move at all.

## Decisions needed
See the PR / conversation: which features to keep, how to resolve each collinear pair, the
ridge strength, and the pre-registered live bar.
