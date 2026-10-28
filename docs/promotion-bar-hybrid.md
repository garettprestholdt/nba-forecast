# Promotion bar: hybrid (team + player) vs. the published model

**Pre-registered on 2026-10-27, before the hybrid logged a single live prediction.** The git
history shows this file landed before the first row of `shadow/hybrid/2026-27.csv` on the `data`
branch. The rule below is fixed. Interim results will show on the site the whole time, but they
don't change the rule, the window, or the end date. The only edits allowed after today are the
decision record at the bottom.

The check is computed mechanically every morning in `scorecard/<season>-hybrid.json` →
`promotion_check` (`shadow.promotion_check`).

## What's being decided
Whether `hybrid-realistic-v1` (`data/published/hybrid/model.json`) replaces the published forecast.
The comparison is with **whatever model was published for each game**: today that's the
adaptive Elo; if Elo + back-to-back is promoted in the meantime, it's that.

## Window
- **The 2026-27 regular season**: every regular-season game from the first shadow prediction to the
  last regular-season game. About 1,150 games.
- Only games **both** models predicted before tip-off count, scored once final. Play-in and
  playoff games are reported but don't count.
- **No early decision.** The check reads "in progress" until the regular season is over, which
  is the morning the first play-in or playoff result appears.

## Metric
**Log-loss, paired by game.** For each game, the published model's log-loss minus the hybrid's
(positive = hybrid better). Report the mean and its 95% confidence interval
(mean ± 1.96 × SD / √n).

Log-loss is the metric every backtest in this project used. It's a proper scoring rule: it
rewards honest probabilities, not just right picks. Pairing by game removes game-to-game
difficulty, which is most of the noise.

## The bar
Promote when **all** of these hold at the end of the window:
1. **Better on average:** mean paired difference > 0.
2. **Not meaningfully worse:** the lower end of the 95% interval is above **−0.005**.
3. **Coverage:** the hybrid predicted at least **90%** of the regular-season games the published
   model predicted in the window, so failed runs can't cherry-pick the sample.
4. **Calibration:** the hybrid's reliability (Brier decomposition) on those games is no more than
   **0.003** above the published model's on the same games.

Outcomes:
- **Promote:** all four hold.
- **Retire:** the *upper* end of the 95% interval is below 0. The hybrid is credibly worse live, so
  shadowing stops and it goes back to research.
- **Extend:** anything else, including a guardrail failure. Shadow through the 2027-28 regular
  season and apply the same four conditions to both seasons pooled. If it still isn't promoted
  then, it isn't promoted.

## Why these numbers
From the backtest (2020-21 to 2025-26), the per-game paired difference has an SD of 0.140, and the
hybrid's edge over Elo + back-to-back was +0.0061. With about 1,150 games the standard error is
about 0.0041. So condition 2 is the binding one: it amounts to "mean difference above about
0.0031".

| True live edge (log-loss) | −0.005 | −0.003 | 0 | +0.003 | +0.0061 (backtest) | +0.010 |
|---|---|---|---|---|---|---|
| P(promote), one season | 2.5% | 7% | 23% | 49% | 77% | 95% |
| P(promote), two seasons pooled | 2.5% | 10% | 41% | 80% | 97% | ~100% |

- **The margin (0.005)** is more than twice what the back-to-back penalty adds (0.0022), so a model
  that much worse would be a real step back. Such a model gets promoted only 2.5% of the time.
- **Why not demand a CI that excludes 0?** One season is too short to show an edge the size of the
  backtest's at 95% confidence (it would usually fail even if the edge is real). The backtest
  already showed superiority over 7,776 games. The live window checks that nothing breaks out of
  sample (a bug, a regime change), so the right test is "not worse, and pointing the right way".
- **A tie can be promoted** (23% one season, 41% pooled). That's acceptable: the realistic hybrid is
  the base the knows-the-lineup version builds on, and that version was worth +0.0074 more in the
  backtest. Switching at no loss in accuracy unlocks that.
- **Calibration guardrail:** a single season's reliability is noisy. It ranged from 0.0006 to 0.0035
  for well-calibrated models in the backtest, and the hybrid ran up to 0.0015 above the live Elo.
  0.003 above the published model allows for that noise and still catches real miscalibration.

## Frozen during the window
- The model file, the feature code, and the player and form settings don't change. Any change
  that would alter a prediction makes a **new version** (e.g. `hybrid-realistic-v2`), shadowed
  separately with its own window, starting from its first logged prediction.
- Fixes that don't change predictions (logging, the site, the scorecard's own arithmetic) are
  allowed.
- The decision is mechanical. If the maintainer ever overrides it, the reason goes in the record
  below, publicly.

## Decision record
*(Append-only. Each entry: date, window, n, mean, 95% CI, coverage, reliability gap, outcome.)*
