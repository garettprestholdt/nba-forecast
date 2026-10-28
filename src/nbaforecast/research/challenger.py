"""Challenger backtest: the Kalman team-strength model vs. the live Elo.

Same discipline as the Elo backtest: settings are chosen by log-loss on the training seasons
only, in two stages (noise/drift/carryover, then season variance/home-court drift). An ablation
with rest and travel switched off shows what the features add. Then each model is scored once on
the held-out seasons.

Note: the held-out seasons already chose the live Elo (adaptive home court) in Phase 2, so this
comparison is not a fully clean test. The clean test is the live season, in shadow mode.
"""

import itertools
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from nbaforecast import metrics
from nbaforecast.models import elo, kalman
from nbaforecast.models.kalman import KalmanParams
from nbaforecast.pipeline.live import LIVE_PARAMS
from nbaforecast.research import backtest

STAGE_1 = {
    "sigma": [11.0, 12.0, 13.0],
    "team_drift": [0.08, 0.15, 0.25],
    "carryover": [0.6, 0.7, 0.8],
}
STAGE_2 = {"season_var": [4.0, 9.0, 16.0], "hca_drift": [0.005, 0.01, 0.02]}
REST_PENALTIES = [0, 20, 35, 50, 65, 80]  # Elo points; tuned for "Elo + back-to-back"


def tune_elo_rest(games: pd.DataFrame) -> tuple[elo.EloParams, pd.DataFrame]:
    """The live Elo plus a back-to-back penalty, the penalty chosen on the training seasons."""
    trials = []
    for penalty in REST_PENALTIES:
        params = replace(LIVE_PARAMS, rest_penalty=float(penalty))
        preds, _ = elo.run(games, params)
        trials.append(
            {
                "rest_penalty": penalty,
                **backtest.score(preds[preds["season"].isin(backtest.TRAIN_SEASONS)]),
            }
        )
    table = pd.DataFrame(trials).sort_values("log_loss").reset_index(drop=True)
    return replace(LIVE_PARAMS, rest_penalty=float(table.iloc[0]["rest_penalty"])), table


def _train_score(games: pd.DataFrame, params: KalmanParams) -> dict:
    preds, _ = kalman.run(games, params)
    return backtest.score(preds[preds["season"].isin(backtest.TRAIN_SEASONS)])


def tune(
    games: pd.DataFrame, base: KalmanParams | None = None
) -> tuple[KalmanParams, pd.DataFrame]:
    """Two-stage grid search on the training seasons. Returns (best params, every trial)."""
    best = base or KalmanParams()
    trials = []
    for stage, grid in (("1", STAGE_1), ("2", STAGE_2)):
        names = list(grid)
        for values in itertools.product(*(grid[n] for n in names)):
            params = replace(best, **dict(zip(names, values, strict=True)))
            trials.append(
                {
                    "stage": stage,
                    "label": params.label(),
                    **params.as_dict(),
                    **_train_score(games, params),
                }
            )
        stage_trials = pd.DataFrame([t for t in trials if t["stage"] == stage])
        winner = stage_trials.sort_values("log_loss").iloc[0]
        best = replace(best, **{n: float(winner[n]) for n in names})
    return best, pd.DataFrame(trials).sort_values("log_loss").reset_index(drop=True)


def compare(games: pd.DataFrame) -> dict:
    best, trials = tune(games)
    elo_rest, elo_rest_trials = tune_elo_rest(games)
    no_features = replace(best, use_features=False)
    models = {
        "Elo, adaptive home court (live model)": elo.run(games, LIVE_PARAMS)[0],
        f"Elo + back-to-back ({elo_rest.rest_penalty:g} pts)": elo.run(games, elo_rest)[0],
        "Kalman, rest + travel (challenger)": None,
        "Kalman, no rest/travel (ablation)": kalman.run(games, no_features)[0],
    }
    challenger_preds, challenger_state = kalman.run(games, best)
    models["Kalman, rest + travel (challenger)"] = challenger_preds

    rows, by_season, calibration = [], [], []
    for name, preds in models.items():
        train = preds[preds["season"].isin(backtest.TRAIN_SEASONS)]
        held = preds[preds["season"].isin(backtest.HOLDOUT_SEASONS)]
        rows.append(
            {
                "model": name,
                "train_log_loss": backtest.score(train)["log_loss"],
                **backtest.score(held),
            }
        )
        for season, group in preds[preds["season"].isin(backtest.HOLDOUT_SEASONS)].groupby(
            "season"
        ):
            by_season.append({"season": season, "model": name, **backtest.score(group)})
        calibration += [
            {"model": name, **r}
            for r in metrics.calibration_table(held["p_home"].tolist(), held["home_win"].tolist())
        ]

    # Paired comparison on the same held-out games: is the challenger's edge bigger than noise?
    kal_held = challenger_preds.set_index("game_id")
    paired = {}
    for name in (
        "Elo, adaptive home court (live model)",
        next(m for m in models if m.startswith("Elo + back")),
    ):
        base = models[name].set_index("game_id")
        ids = base.index[base["season"].isin(backtest.HOLDOUT_SEASONS)]
        paired[f"{name} minus Kalman challenger"] = paired_log_loss_difference(
            base.loc[ids, "p_home"], kal_held.loc[ids, "p_home"], base.loc[ids, "home_win"]
        )
    ebb = models[next(m for m in models if m.startswith("Elo + back"))].set_index("game_id")
    live = models["Elo, adaptive home court (live model)"].set_index("game_id")
    ids = live.index[live["season"].isin(backtest.HOLDOUT_SEASONS)]
    paired["Live Elo minus Elo + back-to-back"] = paired_log_loss_difference(
        live.loc[ids, "p_home"], ebb.loc[ids, "p_home"], live.loc[ids, "home_win"]
    )
    return {
        "params": best,
        "trials": trials,
        "holdout": pd.DataFrame(rows),
        "by_season": pd.DataFrame(by_season),
        "calibration": pd.DataFrame(calibration),
        "paired": paired,
        "coefficients": challenger_state.coefficients(),
        "elo_rest": elo_rest,
        "elo_rest_trials": elo_rest_trials,
    }


def paired_log_loss_difference(p_a: pd.Series, p_b: pd.Series, y: pd.Series) -> dict:
    """Per-game log-loss of A minus B (positive = B better), with a 95% interval."""
    import math

    def ll(p, outcome):
        p = min(max(p, 1e-15), 1 - 1e-15)
        return -math.log(p if outcome == 1 else 1 - p)

    diffs = [ll(a, o) - ll(b, o) for a, b, o in zip(p_a, p_b, y, strict=True)]
    n = len(diffs)
    mean = sum(diffs) / n
    sd = math.sqrt(sum((d - mean) ** 2 for d in diffs) / (n - 1))
    half = 1.96 * sd / math.sqrt(n)
    return {"games": n, "mean_difference": mean, "ci95": [mean - half, mean + half]}


def write(result: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "challenger_params": result["params"].as_dict(),
        "learned_coefficients_now": result["coefficients"],
        "holdout": result["holdout"].to_dict(orient="records"),
        "paired_log_loss_differences": result["paired"],
        "elo_rest_penalty_chosen": result["elo_rest"].rest_penalty,
        "trials": len(result["trials"]),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    result["by_season"].to_csv(out_dir / "by_season.csv", index=False, float_format="%.5f")
    result["calibration"].to_csv(out_dir / "calibration.csv", index=False, float_format="%.5f")
    result["trials"].head(25).to_csv(out_dir / "tuning_top25.csv", index=False, float_format="%.5f")
    result["elo_rest_trials"].to_csv(
        out_dir / "elo_rest_tuning.csv", index=False, float_format="%.5f"
    )
