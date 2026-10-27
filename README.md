# nba-forecast: data branch

Written by the daily GitHub Actions job (`nbaf daily`) in
[garettprestholdt/nba-forecast](https://github.com/garettprestholdt/nba-forecast).
No code lives here.

- `predictions/<season>.csv`: **append-only.** One row per game, committed before tip-off.
  The git history of this branch is the timestamped record; force-pushes are blocked.
- `results/<season>.csv`: final scores, as ingested each morning
- `scorecard/<season>.json`: season-to-date log-loss, Brier score and accuracy for the model,
  the betting market (same games), and a coin flip
- `state/elo_state.json`: the ratings and settings the next run resumes from
- `availability/`, `injuries/`, `recaps/`: injury calls (append-only), the injury lists they
  were made from, and the morning recaps
- `shadow/hybrid/<season>.csv`: **append-only.** The challenger (team + player hybrid) model's
  predictions, logged before tip-off next to Elo's but not the published forecast
- `scorecard/<season>-hybrid.json`: the challenger vs. Elo (and the market) on the same games
- `state/hybrid_state.json`: player ratings and team form the challenger resumes from

A forecasting and calibration study, not betting advice.
