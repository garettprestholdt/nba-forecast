"""Hybrid backtest: team history + player layer vs. Elo, walk-forward.

Same discipline as the other backtests. Every setting is chosen by log-loss on walk-forward
predictions for the training seasons only (2007-08 to 2018-19; 2005-07 are the first training
data), in stages: the regression (link, penalty), then the player ratings (decay, shrinkage),
then projected minutes (decay, season carryover), then team form. Each model is then scored once
on the held-out seasons (2020-21 to 2025-26).

Models compared:
* live Elo, and Elo + back-to-back (the Phase 6 research finding);
* team stack: Elo + rest + box-score form;
* hybrid, realistic: team stack + players weighted by availability (nothing from game day);
* hybrid, knows the lineup: team stack + the players actually available for the game;
* players only (knows the lineup), to see the player layer on its own.
"""

import itertools
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from nbaforecast import metrics
from nbaforecast.models import elo, hybrid, players
from nbaforecast.models.hybrid import FitParams
from nbaforecast.models.players import PlayerParams
from nbaforecast.pipeline.live import LIVE_PARAMS
from nbaforecast.research import backtest
from nbaforecast.research.challenger import paired_log_loss_difference

TRAIN = [s for s in backtest.TRAIN_SEASONS if s >= "2007-08"]
HOLDOUT = backtest.HOLDOUT_SEASONS
ELO_REST = replace(LIVE_PARAMS, rest_penalty=35.0)  # chosen in the Phase 6 challenger backtest

FIT_GRID = [
    FitParams(link, l2, intercept=False)
    for link in ("logistic", "margin")
    for l2 in (0.1, 1.0, 10, 100)
]
PLAYER_STAGES = [
    {"decay": [0.97, 0.99, 0.995], "prior_minutes": [300.0, 600.0, 1200.0]},
    {"minutes_decay": [0.5, 0.7, 0.85], "carryover": [0.7, 0.9, 1.0]},
]
FORM_DECAYS = [0.95, 0.98, 0.99]
MODELS = {
    "Team stack (Elo + rest + form)": "team",
    "Hybrid, realistic": "realistic",
    "Hybrid, knows the lineup": "lineup",
    "Players only, knows the lineup": "players_only",
}


def train_log_loss(preds: pd.DataFrame) -> float:
    return backtest.score(preds[preds["season"].isin(TRAIN)])["log_loss"]


class Inputs:
    """Data plus caches, so tuning doesn't repeat the slow player walk."""

    def __init__(self, games, team_games, player_games):
        self.games, self.team_games, self.player_games = games, team_games, player_games
        self._players: dict[PlayerParams, pd.DataFrame] = {}

    def player_features(self, params: PlayerParams) -> pd.DataFrame:
        if params not in self._players:
            self._players[params] = players.walk(self.player_games, params)
        return self._players[params]

    def features(self, params: PlayerParams, form_decay: float) -> pd.DataFrame:
        return hybrid.build_features(
            self.games, self.team_games, self.player_features(params), LIVE_PARAMS, form_decay
        )


def tune(inputs: Inputs, feature_set: str = "realistic") -> tuple[dict, pd.DataFrame]:
    """Stage-wise search on training seasons for the deployable (realistic) hybrid."""
    names = hybrid.feature_set(feature_set)
    best = {"fit": FitParams(intercept=False), "players": PlayerParams(), "form_decay": 0.95}
    trials = []

    def run(stage: str, label: str, fit_params, player_params, form_decay) -> float:
        feats = inputs.features(player_params, form_decay)
        preds, _ = hybrid.walk_forward(feats, names, fit_params)
        loss = train_log_loss(preds)
        trials.append({"stage": stage, "setting": label, "train_log_loss": loss})
        return loss

    scores = {
        f: run("regression", f.label(), f, best["players"], best["form_decay"]) for f in FIT_GRID
    }
    best["fit"] = min(scores, key=scores.get)
    for i, grid in enumerate(PLAYER_STAGES, start=1):
        keys = list(grid)
        options = [
            replace(best["players"], **dict(zip(keys, values, strict=True)))
            for values in itertools.product(*grid.values())
        ]
        scores = {
            p: run(f"players {i}", p.label(), best["fit"], p, best["form_decay"]) for p in options
        }
        best["players"] = min(scores, key=scores.get)
    scores = {
        d: run("team form", f"form_decay={d:g}", best["fit"], best["players"], d)
        for d in FORM_DECAYS
    }
    best["form_decay"] = min(scores, key=scores.get)
    table = pd.DataFrame(trials).sort_values(["stage", "train_log_loss"]).reset_index(drop=True)
    return best, table


def compare(inputs: Inputs, best: dict) -> dict:
    feats = inputs.features(best["players"], best["form_decay"])
    preds = {
        "Elo, adaptive home court (live model)": feats.assign(p_home=feats["p_home"]),
        "Elo + back-to-back": elo.run(inputs.games, ELO_REST)[0],
    }
    fits = {}
    for name, feature_set in MODELS.items():
        preds[name], fits[name] = hybrid.walk_forward(
            feats, hybrid.feature_set(feature_set), best["fit"]
        )
    held = {name: p[p["season"].isin(HOLDOUT)].set_index("game_id") for name, p in preds.items()}

    rows, by_season, calibration = [], [], []
    for name, p in preds.items():
        rows.append({"model": name, "train_log_loss": train_log_loss(p),
                     **backtest.score(held[name])})  # fmt: skip
        for season, group in held[name].groupby("season"):
            by_season.append({"season": season, "model": name, **backtest.score(group)})
        calibration += [
            {"model": name, **r}
            for r in metrics.calibration_table(
                held[name]["p_home"].tolist(), held[name]["home_win"].tolist()
            )
        ]

    ids = held["Hybrid, realistic"].index
    y = held["Hybrid, realistic"]["home_win"]

    def paired(a: str, b: str) -> dict:
        return paired_log_loss_difference(held[a].loc[ids, "p_home"], held[b].loc[ids, "p_home"], y)

    comparisons = {
        "Elo + back-to-back minus hybrid (realistic)": paired(
            "Elo + back-to-back", "Hybrid, realistic"
        ),
        "Team stack minus hybrid (realistic)": paired(
            "Team stack (Elo + rest + form)", "Hybrid, realistic"
        ),
        "Hybrid (realistic) minus hybrid (knows the lineup)": paired(
            "Hybrid, realistic", "Hybrid, knows the lineup"
        ),
    }
    return {
        "best": best,
        "holdout": pd.DataFrame(rows),
        "by_season": pd.DataFrame(by_season),
        "calibration": pd.DataFrame(calibration),
        "paired": comparisons,
        "absences": absences(feats, held),
        "coefficients": {name: fit["2025-26"].coefficients() for name, fit in fits.items()},
    }


def absences(feats: pd.DataFrame, held: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Where does knowing the lineup help? Held-out games bucketed by how far the available
    players' strength is from the usual roster's (bigger = more missing talent on one side)."""
    f = feats.set_index("game_id").loc[held["Hybrid, realistic"].index]
    gap = (f["production_lineup"] - f["production_real"]).abs()
    buckets = pd.qcut(gap, [0, 0.5, 0.8, 0.95, 1.0], labels=["bottom half", "50-80%", "80-95%",
                                                            "top 5%"])  # fmt: skip
    realistic, lineup = held["Hybrid, realistic"], held["Hybrid, knows the lineup"]
    rows = []
    for label, idx in buckets.groupby(buckets, observed=True).groups.items():
        rows.append(
            {
                "absence_gap": label,
                "games": len(idx),
                "realistic_log_loss": backtest.score(realistic.loc[idx])["log_loss"],
                "lineup_log_loss": backtest.score(lineup.loc[idx])["log_loss"],
            }
        )
    return pd.DataFrame(rows)


def write(result: dict, trials: pd.DataFrame, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    best = result["best"]
    summary = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "chosen": {
            "regression": asdict(best["fit"]),
            "players": asdict(best["players"]),
            "form_decay": best["form_decay"],
        },
        "holdout": result["holdout"].to_dict(orient="records"),
        "paired_log_loss_differences": result["paired"],
        "coefficients_2025_26_fit": result["coefficients"],
        "trials": len(trials),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    result["by_season"].to_csv(out_dir / "by_season.csv", index=False, float_format="%.5f")
    result["calibration"].to_csv(out_dir / "calibration.csv", index=False, float_format="%.5f")
    result["absences"].to_csv(out_dir / "absences.csv", index=False, float_format="%.5f")
    trials.to_csv(out_dir / "tuning.csv", index=False, float_format="%.5f")
