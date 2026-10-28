"""Challenger backtest: the hybrid with RAPM (offense and defense) and who's missing.

Candidates are fit walk-forward exactly like the hybrid (logistic stack, no intercept, ridge),
with the chosen player and form settings from `data/published/hybrid/summary.json`. RAPM
features exist from 2018-19 (each refit needs the previous season's stints), so RAPM models
train from 2018-19 and predict 2020-21 on: the same held-out seasons as every other backtest.
To separate "better features" from "less training data", the current hybrid is also refit on
the same short window.

Diagnostics for choosing features: correlations and variance inflation factors (VIF) between
candidates, in-sample vs walk-forward log-loss (the overfitting gap), how much each coefficient
moves between yearly refits, and a ridge-strength sweep for the everything-in model.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from nbaforecast import config
from nbaforecast.models import hybrid, impact, players
from nbaforecast.models.hybrid import FitParams
from nbaforecast.pipeline.live import LIVE_PARAMS
from nbaforecast.research import backtest, rapm_walk
from nbaforecast.research.challenger import paired_log_loss_difference

OUT_DIR = config.PUBLISHED_DIR / "impact_challenger"
STINT_SEASONS = backtest.seasons(2017, 2025)  # 2017-18 .. 2025-26
FIRST_RAPM_SEASON = "2018-19"
HOLDOUT = backtest.HOLDOUT_SEASONS

TEAM = hybrid.TEAM_FEATURES
CANDIDATES = {  # name -> features (all also get the team history)
    "Team stack": [],
    "Hybrid v1, realistic (current challenger)": [
        "production_real", "impact_real", "elo_x_turnover"],
    "Hybrid v1, knows the lineup": ["production_lineup", "impact_lineup", "elo_x_turnover"],
    "RAPM, whole roster": ["rapm_o_real", "rapm_d_real", "elo_x_turnover"],
    "RAPM, tonight's lineup": ["rapm_o_lineup", "rapm_d_lineup", "elo_x_turnover"],
    "RAPM, whole roster + missing value": [
        "rapm_o_real", "rapm_d_real", "absent_rapm", "elo_x_turnover"],
    "RAPM + production, tonight's lineup": [
        "rapm_o_lineup", "rapm_d_lineup", "production_lineup", "elo_x_turnover"],
    "Everything": ["production_real", "impact_real", "production_lineup", "impact_lineup",
                   "rapm_o_real", "rapm_d_real", "rapm_o_lineup", "rapm_d_lineup",
                   "absent_rapm", "elo_x_turnover"],
}  # fmt: skip
DIAGNOSTIC_FEATURES = TEAM + CANDIDATES["Everything"]
L2_SWEEP = [0.1, 1.0, 10.0, 100.0, 1000.0]


def chosen() -> dict:
    s = json.loads((config.PUBLISHED_DIR / "hybrid" / "summary.json").read_text())["chosen"]
    return {"fit": FitParams(**s["regression"]), "players": players.PlayerParams(**s["players"]),
            "form_decay": s["form_decay"]}  # fmt: skip


def build(games, team_games, player_games, box_rows, data_dir: Path) -> pd.DataFrame:
    """Features for every game, RAPM included where it exists (2018-19 on)."""
    best = chosen()
    stints = rapm_walk.load_stints(data_dir, STINT_SEASONS, games)
    seasons = [s for s in STINT_SEASONS if s >= FIRST_RAPM_SEASON]
    ratings = rapm_walk.walk_ratings(stints, box_rows, games, seasons)
    pf = players.walk(player_games, best["players"], ratings=ratings, unknown=impact.UNKNOWN)
    feats = hybrid.build_features(games, team_games, pf, LIVE_PARAMS, best["form_decay"])
    feats.attrs["stint_games"] = int(stints["game_id"].nunique())
    return feats


def walk(feats: pd.DataFrame, names: list[str], params: FitParams, first: str):
    """Walk-forward from `first`: each season predicted by a fit on every earlier season."""
    seasons = sorted(s for s in feats["season"].unique() if s >= first)
    preds, fits = [], {}
    for i, season in enumerate(seasons[2:], start=2):
        train = feats[feats["season"].isin(seasons[:i])]
        model = hybrid.fit(train, names, params)
        target = feats[feats["season"] == season]
        preds.append(target.assign(p_home=model.predict(target)))
        fits[season] = (model, train)
    return pd.concat(preds, ignore_index=True), fits


def _log_loss(p: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def vif(df: pd.DataFrame) -> pd.Series:
    """Variance inflation factor: 1 / (1 - R²) of each feature regressed on all the others."""
    x = (df - df.mean()) / df.std().replace(0, 1)
    out = {}
    for c in x.columns:
        others = x.drop(columns=c).to_numpy()
        coef, *_ = np.linalg.lstsq(others, x[c].to_numpy(), rcond=None)
        resid = x[c].to_numpy() - others @ coef
        r2 = 1 - resid.var() / x[c].var()
        out[c] = float("inf") if r2 >= 1 else 1 / (1 - r2)
    return pd.Series(out).sort_values(ascending=False)


def run(feats: pd.DataFrame) -> dict:
    best = chosen()
    params = best["fit"]
    rapm_rows = feats[feats["season"] >= FIRST_RAPM_SEASON]
    held_ids = set(feats.loc[feats["season"].isin(HOLDOUT), "game_id"])
    models, preds, coef_rows = [], {}, []

    def record(name, p, fits, window, names):
        h = p[p["game_id"].isin(held_ids)].set_index("game_id")
        insample = [_log_loss(m.predict(tr), tr["home_win"].to_numpy()) for m, tr in fits.values()]
        models.append({"model": name, "train_from": window, "features": len(names),
                       "in_sample_log_loss": float(np.mean(insample)),
                       **backtest.score(h)})  # fmt: skip
        preds[name] = h
        for season, (m, _) in fits.items():
            for f, c in m.coefficients().items():
                coef_rows.append({"model": name, "season": season, "feature": f, "coef": c})

    elo_held = feats[feats["game_id"].isin(held_ids)].set_index("game_id")
    models.append({"model": "Elo, adaptive home court (published)", "train_from": "-",
                   "features": 0, "in_sample_log_loss": None,
                   **backtest.score(elo_held)})  # fmt: skip
    preds["Elo, adaptive home court (published)"] = elo_held
    v1_names = TEAM + CANDIDATES["Hybrid v1, realistic (current challenger)"]
    v1, v1_fits = walk(feats, v1_names, params, hybrid.FIRST_TRAIN_SEASON)
    record("Hybrid v1, realistic (as published: trained from 2005)", v1, v1_fits,
           hybrid.FIRST_TRAIN_SEASON, v1_names)  # fmt: skip
    for name, extra in CANDIDATES.items():
        p, fits = walk(rapm_rows, TEAM + extra, params, FIRST_RAPM_SEASON)
        record(name, p, fits, FIRST_RAPM_SEASON, TEAM + extra)

    ids = sorted(set.intersection(*(set(p.index) for p in preds.values())))
    y = preds["Team stack"].loc[ids, "home_win"]
    base = "Elo, adaptive home court (published)"
    paired = {
        name: paired_log_loss_difference(preds[base].loc[ids, "p_home"], p.loc[ids, "p_home"], y)
        for name, p in preds.items() if name != base
    }  # fmt: skip
    v1_name = "Hybrid v1, realistic (as published: trained from 2005)"
    vs_v1 = {
        name: paired_log_loss_difference(preds[v1_name].loc[ids, "p_home"], p.loc[ids, "p_home"], y)
        for name, p in preds.items() if name not in (base, v1_name)
    }  # fmt: skip

    diag = rapm_rows[DIAGNOSTIC_FEATURES]  # unsupervised: no outcomes, so every season is fine
    corr = diag.corr().round(3)
    vifs = vif(diag).round(2)
    coefs = pd.DataFrame(coef_rows)
    stability = (coefs.groupby(["model", "feature"])["coef"]
                 .agg(["mean", "std", "min", "max"]).round(4).reset_index())  # fmt: skip
    stability["sign_flips"] = coefs.groupby(["model", "feature"])["coef"].apply(
        lambda s: int((np.sign(s) != np.sign(s.iloc[0])).sum())).values  # fmt: skip

    sweep = []
    for l2 in L2_SWEEP:
        p_ = FitParams(params.link, l2, params.intercept)
        p, fits = walk(rapm_rows, TEAM + CANDIDATES["Everything"], p_, FIRST_RAPM_SEASON)
        h = p[p["game_id"].isin(held_ids)]
        insample = [_log_loss(m.predict(tr), tr["home_win"].to_numpy()) for m, tr in fits.values()]
        sweep.append({"l2": l2, "in_sample_log_loss": float(np.mean(insample)),
                      **backtest.score(h)})  # fmt: skip

    lineup_name = "RAPM, tonight's lineup"
    gap = rapm_rows.set_index("game_id").loc[ids, "absent_rapm"].abs()
    buckets = pd.qcut(gap.rank(method="first"), [0, 0.5, 0.8, 0.95, 1.0],
                      labels=["bottom half", "50-80%", "80-95%", "top 5%"])  # fmt: skip
    absences = []
    for label, idx in buckets.groupby(buckets, observed=True).groups.items():
        row = {"missing_value_bucket": label, "games": len(idx),
               "mean_abs_missing": round(float(gap.loc[idx].mean()), 2)}  # fmt: skip
        for name in (base, v1_name, "RAPM, whole roster", lineup_name):
            row[name] = backtest.score(preds[name].loc[idx])["log_loss"]
        absences.append(row)

    return {
        "models": pd.DataFrame(models),
        "paired_vs_elo": paired,
        "paired_vs_v1": vs_v1,
        "correlations": corr,
        "vif": vifs,
        "stability": stability,
        "l2_sweep": pd.DataFrame(sweep),
        "absences": pd.DataFrame(absences),
        "n_common": len(ids),
        "stint_games": feats.attrs.get("stint_games"),
    }


def write(result: dict, out_dir: Path = OUT_DIR) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    result["models"].to_csv(out_dir / "models.csv", index=False)
    result["correlations"].to_csv(out_dir / "correlations.csv")
    result["vif"].rename("vif").to_csv(out_dir / "vif.csv")
    result["stability"].to_csv(out_dir / "coefficient_stability.csv", index=False)
    result["l2_sweep"].to_csv(out_dir / "l2_sweep.csv", index=False)
    result["absences"].to_csv(out_dir / "absences.csv", index=False)
    summary = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "holdout": HOLDOUT, "games_compared": result["n_common"],
        "stint_games": result["stint_games"],
        "paired_vs_elo": result["paired_vs_elo"], "paired_vs_v1": result["paired_vs_v1"],
    }  # fmt: skip
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
