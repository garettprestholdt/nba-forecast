"""Walk-forward backtest: tune Elo on early seasons, then score it once on held-out seasons.

- 2004-05 is burn-in (every team starts at the mean) and is never scored.
- Parameters are chosen by log-loss on the training seasons only.
- 2019-20 (COVID suspension + bubble) sits between train and holdout; the model runs through
  it but it is scored in neither.
- The holdout seasons are scored once, with the chosen parameters, against simple baselines.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from nbaforecast import metrics
from nbaforecast.models import elo
from nbaforecast.models.elo import EloParams


def seasons(first_start: int, last_start: int) -> list[str]:
    return [f"{y}-{(y + 1) % 100:02d}" for y in range(first_start, last_start + 1)]


TRAIN_SEASONS = seasons(2005, 2018)
HOLDOUT_SEASONS = seasons(2020, 2025)

DEFAULT_GRID = [
    *elo.grid(
        k=[15, 20, 25, 30],
        home_advantage=[60, 80, 100, 120],
        carryover=[0.6, 0.7, 0.75, 0.8, 0.9],
    ),
    *elo.grid(
        k=[15, 20, 25, 30],
        hca_window=[615, 1230, 2460],  # half a season, one season, two seasons of games
        carryover=[0.6, 0.7, 0.75, 0.8, 0.9],
    ),
]


def score(df: pd.DataFrame, prob_col: str = "p_home") -> dict[str, float]:
    probs, outcomes = df[prob_col].tolist(), df["home_win"].tolist()
    return {
        "n": len(df),
        "log_loss": metrics.log_loss(probs, outcomes),
        "brier": metrics.brier_score(probs, outcomes),
        "accuracy": metrics.accuracy(probs, outcomes),
        # Calibration-in-the-large: a calibrated model's average forecast matches the outcome rate.
        "mean_forecast": sum(probs) / len(probs),
        "home_win_rate": sum(outcomes) / len(outcomes),
        "reliability": metrics.brier_decomposition(probs, outcomes)["reliability"],
    }


def add_baselines(preds: pd.DataFrame) -> pd.DataFrame:
    """Coin flip, and the previous season's home win rate (0.5 at neutral sites)."""
    out = preds.copy()
    out["p_coin_flip"] = 0.5
    home_games = out[~out["is_neutral_site"]]
    rate_by_season = home_games.groupby("season")["home_win"].mean()
    previous = rate_by_season.shift(1)
    out["p_home_rate"] = out["season"].map(previous).fillna(0.5)
    out.loc[out["is_neutral_site"], "p_home_rate"] = 0.5
    return out


def tune(games: pd.DataFrame, candidates: list[EloParams]) -> pd.DataFrame:
    """Train-season scores for every candidate, best (lowest log-loss) first."""
    rows = []
    for params in candidates:
        preds, _ = elo.run(games, params)
        train = preds[preds["season"].isin(TRAIN_SEASONS)]
        rows.append({"label": params.label(), **params.as_dict(), **score(train)})
    return pd.DataFrame(rows).sort_values("log_loss").reset_index(drop=True)


def _params_from_row(row: pd.Series) -> EloParams:
    window = row["hca_window"]
    return EloParams(
        k=float(row["k"]),
        home_advantage=float(row["home_advantage"]),
        hca_window=None if pd.isna(window) else int(window),
        carryover=float(row["carryover"]),
        mov=bool(row["mov"]),
        mean=float(row["mean"]),
    )


@dataclass
class Report:
    champion: EloParams
    tuning: pd.DataFrame
    holdout: pd.DataFrame  # one row per model
    by_season: pd.DataFrame
    calibration: pd.DataFrame
    decomposition: dict[str, float]
    predictions: pd.DataFrame  # champion's pre-game predictions for every game
    data_range: tuple[str, str]


def run(games: pd.DataFrame, candidates: list[EloParams] | None = None) -> Report:
    tuning = tune(games, candidates or DEFAULT_GRID)
    champion = _params_from_row(tuning.iloc[0])
    adaptive = tuning["hca_window"].notna()
    variants = {
        "Elo (chosen)": champion,
        "Elo, best fixed home court": _params_from_row(tuning[~adaptive].iloc[0]),
    }
    if adaptive.any():
        variants["Elo, best adaptive home court"] = _params_from_row(tuning[adaptive].iloc[0])

    # Every Elo variant plus the baselines, as (model name, predictions, probability column).
    scored_models, champion_preds = [], None
    for name, params in variants.items():
        preds = add_baselines(elo.run(games, params)[0])
        if params == champion:
            champion_preds = preds
        scored_models.append((name, params.label(), preds, "p_home"))
    for name, col in [
        ("Previous season's home win rate", "p_home_rate"),
        ("Coin flip", "p_coin_flip"),
    ]:
        scored_models.append((name, "", champion_preds, col))

    holdout_rows, by_season_rows, calibration_rows = [], [], []
    for name, label, preds, col in scored_models:
        held = preds[preds["season"].isin(HOLDOUT_SEASONS)]
        holdout_rows.append({"model": name, "params": label, **score(held, col)})
        for season, group in preds[preds["season"] != "2004-05"].groupby("season"):
            split = (
                "train"
                if season in TRAIN_SEASONS
                else "holdout"
                if season in HOLDOUT_SEASONS
                else "not scored"
            )
            by_season_rows.append(
                {"season": season, "split": split, "model": name, **score(group, col)}
            )
        if col == "p_home":
            calibration_rows += [
                {"model": name, **row}
                for row in metrics.calibration_table(held[col].tolist(), held["home_win"].tolist())
            ]

    held = champion_preds[champion_preds["season"].isin(HOLDOUT_SEASONS)]
    probs, outcomes = held["p_home"].tolist(), held["home_win"].tolist()
    return Report(
        champion=champion,
        tuning=tuning,
        holdout=pd.DataFrame(holdout_rows),
        by_season=pd.DataFrame(by_season_rows),
        calibration=pd.DataFrame(calibration_rows),
        decomposition=metrics.brier_decomposition(probs, outcomes),
        predictions=champion_preds,
        data_range=(str(games["game_date"].min()), str(games["game_date"].max())),
    )


def _fmt(df: pd.DataFrame) -> str:
    """A GitHub-flavored Markdown table (floats to 4 places)."""

    def cell(v) -> str:
        return f"{v:.4f}" if isinstance(v, float) else str(v)

    lines = ["| " + " | ".join(df.columns) + " |", "|" + "---|" * len(df.columns)]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join(lines)


def write(report: Report, out_dir: Path) -> None:
    """Small, publishable outputs (a few KB) for the repo and the Phase 4 site."""
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "data_range": report.data_range,
        "train_seasons": [TRAIN_SEASONS[0], TRAIN_SEASONS[-1]],
        "holdout_seasons": [HOLDOUT_SEASONS[0], HOLDOUT_SEASONS[-1]],
        "candidates_tried": len(report.tuning),
        "champion": report.champion.as_dict(),
        "holdout": report.holdout.to_dict(orient="records"),
        "holdout_brier_decomposition": report.decomposition,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    report.by_season.to_csv(out_dir / "by_season.csv", index=False, float_format="%.5f")
    report.calibration.to_csv(out_dir / "calibration.csv", index=False, float_format="%.5f")
    report.tuning.head(25).to_csv(out_dir / "tuning_top25.csv", index=False, float_format="%.5f")
    (out_dir / "README.md").write_text(markdown(report))


def markdown(report: Report) -> str:
    d = report.decomposition
    return f"""# Elo backtest

Generated by `nbaf backtest` from {report.data_range[0]} to {report.data_range[1]} games.
Tuned on {TRAIN_SEASONS[0]}–{TRAIN_SEASONS[-1]} ({len(report.tuning)} configurations, chosen by
log-loss); scored once on the held-out {HOLDOUT_SEASONS[0]}–{HOLDOUT_SEASONS[-1]} seasons.

**Chosen parameters:** `{report.champion.label()}`

## Held-out seasons

{_fmt(report.holdout)}

Brier decomposition (held out): reliability {d["reliability"]:.4f} (lower is better),
resolution {d["resolution"]:.4f} (higher is better), uncertainty {d["uncertainty"]:.4f}.

## Calibration (held out, each Elo variant)

{_fmt(report.calibration)}

## By season (every model)

{_fmt(report.by_season)}
"""
