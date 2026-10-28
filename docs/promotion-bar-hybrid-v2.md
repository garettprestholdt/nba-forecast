# Promotion bar: hybrid v2 (RAPM + who's missing) vs. the published model

**Pre-registered on 2026-10-28, before hybrid v2 logged a single live prediction.** The git
history shows this file landed before the first row of `shadow/hybrid-v2/2026-27.csv` on the
`data` branch. The rule is fixed. Only the decision record at the bottom may change after
today.

The check is computed every morning in `scorecard/<season>-hybrid-v2.json` →
`promotion_check` (`shadow.promotion_check` with `shadow_v2.PROMOTION`).

## What's being decided
Whether `hybrid-lineup-v2` (`data/published/hybrid_v2/model.json`, features and their selection
in `docs/findings/impact-challenger.md`) replaces the published forecast. The comparison is with
**whatever model was published for each game**, exactly as for v1.

v1 (`docs/promotion-bar-hybrid.md`) keeps its own window and rule, untouched. If both pass at
the end of the window, **v2 is promoted** if its mean paired improvement over the published
model is larger than v1's on the games all three predicted; otherwise v1 is.

## Why a separate, live test
The held-out seasons (2020-21 to 2025-26) were looked at for nine candidate feature sets before
this one was chosen, so its backtest number (+0.0180 vs. Elo, +0.0097 vs. v1) is optimistic.
The live window is the first data the choice never saw.

## Window, metric and bar
The same as v1, so the two can be compared:
- **Window:** the 2026-27 regular season, from v2's first shadow prediction; only games both v2
  and the published model predicted before tip-off; play-in and playoffs don't count. No early
  decision: "in progress" until the regular season is over.
- **Metric:** log-loss, paired by game (published minus v2; positive = v2 better), mean and
  95% interval.
- **Promote** when all hold: (1) mean > 0; (2) the interval's lower end is above **−0.005**;
  (3) v2 predicted at least **90%** of the published model's regular-season games in the
  window; (4) v2's reliability is no more than **0.003** above the published model's.
- **Retire** if the interval's upper end is below 0. **Extend** otherwise: pool 2026-27 and
  2027-28 and apply the same four conditions; if it still isn't promoted, it isn't.

## Live inputs, fixed for the window
- Tonight's lineup from the morning's ESPN injury status: Out = 0, Day-To-Day = the injury
  watch's chance of playing (agent, else keyword rule, else 0.5), everyone else = 1; players
  weighted by their usual minutes times that chance.
- RAPM from the daily `nbaf impact` fit (last season + this season's stints, 60/40 blend of
  box-prior and plain fits); production and form from the hybrid shadow state.
- A missing injury report or RAPM file means no prediction that day (and lower coverage), never
  a prediction with stale inputs filled in silently.

## Frozen during the window
The model file, the feature code, and the injury and RAPM inputs' definitions don't change. A
change that would alter a prediction is a new version with its own window. Fixes that don't
change predictions (logging, the site, the scorecard's arithmetic) are allowed.

## Decision record
*(Append-only. Each entry: date, window, n, mean, 95% CI, coverage, reliability gap, outcome.)*
