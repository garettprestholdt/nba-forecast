"""Hybrid model: team history + player layer, stacked with a regularized regression.

Every feature is known before tip-off:

* **team history**: the live Elo's win probability (as a logit; it carries home court), each
  team's back-to-back flag, and exponentially weighted box-score form: net rating and the four
  factors (shooting, turnovers, offensive rebounding, free throws), home minus away;
* **player layer** (`models/players.py`): production (Game Score per minute) and impact
  (plus-minus per minute) of the players expected to play, weighted by projected minutes, home
  minus away; plus Elo's logit scaled by roster turnover, since last season's rating means less
  when the roster changed.

The stack is fit **walk-forward**: each season is predicted by a model trained only on earlier
seasons, so every prediction is out of sample. Two links are tried: logistic regression on
wins, and ridge regression on the point margin turned into a probability with a normal curve.
"""

import math
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import pandas as pd

from nbaforecast.models import elo

FORM_COLUMNS = ["net_rating", "efg_diff", "tov_diff", "oreb_diff", "ft_diff"]
# Elo split into team strength and its adaptive home-court estimate (both on the logit scale).
# The stack has no free intercept, so home advantage can only come from the adaptive estimate:
# a fixed intercept learned on older seasons overstated it after home edges shrank around 2020.
TEAM_FEATURES = ["elo_strength", "elo_home", "home_b2b", "away_b2b", *FORM_COLUMNS]
PLAYER_FEATURES = {
    "realistic": ["production_real", "impact_real", "elo_x_turnover"],
    "lineup": ["production_lineup", "impact_lineup", "elo_x_turnover"],
}
# Home-minus-away differences built from the player walk; the RAPM ones exist only when the
# walk is given leak-free ratings (`models/rapm_walk.py`).
PLAYER_DIFF_COLUMNS = [
    "production_real", "impact_real", "production_lineup", "impact_lineup",
    "rapm_o_real", "rapm_d_real", "rapm_o_lineup", "rapm_d_lineup", "absent_rapm",
    "absent_minutes",
]  # fmt: skip
# Average roster turnover (1 - continuity) in the hybrid v2 training games (2018-19 to
# 2024-25); centering the Elo x turnover interaction on it removes its overlap with Elo.
TURNOVER_CENTER = 0.3645
FIRST_TRAIN_SEASON = "2005-06"  # 2004-05 only warms up the ratings
ELO_TO_LOGIT = math.log(10) / 400


def feature_set(name: str) -> list[str]:
    if name == "team":
        return TEAM_FEATURES
    if name == "players_only":
        return ["elo_home", "home_b2b", "away_b2b", *PLAYER_FEATURES["lineup"][:2]]
    return TEAM_FEATURES + PLAYER_FEATURES[name]


class FormState:
    """Each team's box-score form: exponentially weighted averages of FORM_COLUMNS over its
    earlier games, shrunk toward 0 (league average) by `prior` games' worth of weight, and
    partly reset between seasons. Used by the backtest and the live shadow job alike."""

    def __init__(self, decay: float = 0.95, carryover: float = 0.5, prior: float = 3.0):
        self.decay, self.carryover, self.prior = decay, carryover, prior
        self.num: dict[str, np.ndarray] = defaultdict(lambda: np.zeros(len(FORM_COLUMNS)))
        self.den: dict[str, float] = defaultdict(float)
        self.season: str | None = None

    def start_season(self, season: str) -> None:
        if season != self.season:
            for team in self.num:
                self.num[team] = self.num[team] * self.carryover
                self.den[team] *= self.carryover
            self.season = season

    def value(self, team: str) -> np.ndarray:
        return self.num[team] / (self.den[team] + self.prior)

    def update(self, team: str, values) -> None:
        self.num[team] = self.num[team] * self.decay + np.asarray(values, dtype=float)
        self.den[team] = self.den[team] * self.decay + 1

    def to_dict(self) -> dict:
        return {
            "params": {"decay": self.decay, "carryover": self.carryover, "prior": self.prior},
            "season": self.season,
            "teams": {
                team: {"num": [round(float(v), 8) for v in self.num[team]],
                       "den": round(self.den[team], 8)}
                for team in sorted(self.num)
            },
        }  # fmt: skip

    @classmethod
    def from_dict(cls, data: dict) -> "FormState":
        state = cls(**data["params"])
        state.season = data["season"]
        for team, t in data["teams"].items():
            state.num[team] = np.array(t["num"], dtype=float)
            state.den[team] = float(t["den"])
        return state


def team_form(
    games: pd.DataFrame,
    team_games: pd.DataFrame,
    decay: float = 0.95,
    carryover: float = 0.5,
    prior: float = 3.0,
    state: FormState | None = None,
) -> pd.DataFrame:
    """Pre-game form for both teams in every game (one row per game_id, team_id). Only games
    with box-score stats (`team_games`) update it; every game still gets its teams' current form.
    Pass `state` to keep the form it ends with."""
    state = state or FormState(decay, carryover, prior)
    updates = {date: day for date, day in team_games.groupby("game_date")}
    rows = []
    ordered = games.sort_values(["tipoff_utc", "game_id"])
    for (game_date, season), day in ordered.groupby(["game_date", "season"], sort=False):
        state.start_season(season)
        for g in day.itertuples(index=False):
            for team in (g.home_team_id, g.away_team_id):
                rows.append((g.game_id, team, *state.value(team)))
        if game_date in updates:
            done = updates[game_date]
            for team, vals in zip(
                done["team_id"], done[FORM_COLUMNS].to_numpy(dtype=float), strict=True
            ):
                state.update(team, vals)
    return pd.DataFrame(rows, columns=["game_id", "team_id", *FORM_COLUMNS])


def back_to_backs(games: pd.DataFrame) -> pd.DataFrame:
    """home_b2b / away_b2b: the team also played the previous day."""
    last: dict[str, object] = {}
    rows = []
    for g in games.sort_values(["tipoff_utc", "game_id"]).itertuples(index=False):
        flags = []
        for team in (g.home_team_id, g.away_team_id):
            prev = last.get(team)
            flags.append(int(prev is not None and (g.game_date - prev).days == 1))
        rows.append((g.game_id, *flags))
        last[g.home_team_id] = last[g.away_team_id] = g.game_date
    return pd.DataFrame(rows, columns=["game_id", "home_b2b", "away_b2b"])


def build_features(
    games: pd.DataFrame,
    team_games: pd.DataFrame,
    player_features: pd.DataFrame,
    elo_params: elo.EloParams,
    form_decay: float = 0.95,
) -> pd.DataFrame:
    """One row per game: every candidate feature (home minus away), outcome, and margin."""
    preds, _ = elo.run(games, elo_params)
    p = preds["p_home"].clip(1e-6, 1 - 1e-6)
    base = preds[
        [
            "game_id",
            "season",
            "season_type",
            "game_date",
            "home_team_id",
            "away_team_id",
            "home_win",
            "p_home",
        ]
    ].assign(  # fmt: skip
        elo_logit=np.log(p / (1 - p)),
        elo_strength=(preds["home_elo_pre"] - preds["away_elo_pre"]) * ELO_TO_LOGIT,
        elo_home=preds["home_court"] * ELO_TO_LOGIT,
    )
    base = base.merge(back_to_backs(games), on="game_id")
    scores = games.set_index("game_id")
    base["margin"] = base["game_id"].map(scores["home_score"] - scores["away_score"])

    form = team_form(games, team_games, decay=form_decay)
    sides = {"home": "home_team_id", "away": "away_team_id"}
    for side, key in sides.items():
        base = base.merge(
            form.rename(columns={c: f"{c}_{side}" for c in FORM_COLUMNS}),
            left_on=["game_id", key], right_on=["game_id", "team_id"], how="left",
        ).drop(columns="team_id")  # fmt: skip
        base = base.merge(
            player_features.rename(
                columns={c: f"{c}_{side}" for c in player_features.columns if c not in
                         ("game_id", "team_id")}  # fmt: skip
            ),
            left_on=["game_id", key], right_on=["game_id", "team_id"], how="left",
        ).drop(columns="team_id")  # fmt: skip
    player_cols = [c for c in PLAYER_DIFF_COLUMNS if c in player_features.columns]
    for c in FORM_COLUMNS + player_cols:
        base[c] = (base[f"{c}_home"] - base[f"{c}_away"]).fillna(0.0)
    turnover = 1 - (base["continuity_home"].fillna(0) + base["continuity_away"].fillna(0)) / 2
    base["turnover"] = turnover
    base["elo_x_turnover"] = base["elo_strength"] * turnover
    # Centered version: same information, without the r = 0.94 overlap with elo_strength.
    base["elo_x_turnover_c"] = base["elo_strength"] * (turnover - TURNOVER_CENTER)
    return base.sort_values(["game_date", "game_id"]).reset_index(drop=True)


@dataclass(frozen=True)
class FitParams:
    link: str = "logistic"  # "logistic" (wins) or "margin" (ridge on points, normal curve)
    l2: float = 1.0  # ridge penalty on standardized coefficients
    intercept: bool = True  # without one, features are scaled but not centered

    def label(self) -> str:
        return f"{self.link}, l2={self.l2:g}" + ("" if self.intercept else ", no intercept")


@dataclass
class Fit:
    params: FitParams
    features: list[str]
    mean: np.ndarray
    sd: np.ndarray
    coef: np.ndarray  # intercept first, if there is one
    sigma: float = 1.0  # margin link only

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        x = _design(df, self.features, self.mean, self.sd, self.params.intercept)
        z = x @ self.coef
        if self.params.link == "logistic":
            return 1 / (1 + np.exp(-z))
        return np.array([0.5 * (1 + math.erf(v / (self.sigma * math.sqrt(2)))) for v in z])

    def coefficients(self) -> dict[str, float]:
        """Per standard deviation of each feature (intercept unscaled)."""
        names = (["intercept"] if self.params.intercept else []) + self.features
        return dict(zip(names, self.coef.round(4).tolist(), strict=True))


def _design(
    df: pd.DataFrame, features: list[str], mean: np.ndarray, sd: np.ndarray, intercept: bool
) -> np.ndarray:
    x = (df[features].to_numpy(dtype=float) - mean) / sd
    return np.column_stack([np.ones(len(df)), x]) if intercept else x


def fit(train: pd.DataFrame, features: list[str], params: FitParams) -> Fit:
    raw = train[features].to_numpy(dtype=float)
    mean = raw.mean(axis=0) if params.intercept else np.zeros(raw.shape[1])
    sd = raw.std(axis=0)
    sd[sd == 0] = 1.0
    x = _design(train, features, mean, sd, params.intercept)
    penalty = params.l2 * np.eye(x.shape[1])
    if params.intercept:
        penalty[0, 0] = 0.0  # the intercept isn't shrunk
    if params.link == "margin":
        y = train["margin"].to_numpy(dtype=float)
        coef = np.linalg.solve(x.T @ x + penalty, x.T @ y)
        sigma = float(np.std(y - x @ coef))
        return Fit(params, features, mean, sd, coef, sigma)
    y = train["home_win"].to_numpy(dtype=float)
    coef = np.zeros(x.shape[1])
    for _ in range(50):  # Newton's method on the penalized log-likelihood
        p = 1 / (1 + np.exp(-(x @ coef)))
        grad = x.T @ (p - y) + penalty @ coef
        hess = (x * (p * (1 - p))[:, None]).T @ x + penalty
        step = np.linalg.solve(hess, grad)
        coef -= step
        if np.abs(step).max() < 1e-8:
            break
    return Fit(params, features, mean, sd, coef)


def walk_forward(
    features: pd.DataFrame, feature_names: list[str], params: FitParams
) -> tuple[pd.DataFrame, dict[str, Fit]]:
    """Predict each season from a model fit on every earlier season (from FIRST_TRAIN_SEASON).
    Returns predictions for every season with at least two seasons of history."""
    seasons = sorted(s for s in features["season"].unique() if s >= FIRST_TRAIN_SEASON)
    out, fits = [], {}
    for i, season in enumerate(seasons[2:], start=2):
        train = features[features["season"].isin(seasons[:i])]
        model = fit(train, feature_names, params)
        target = features[features["season"] == season]
        out.append(target.assign(p_home=model.predict(target)))
        fits[season] = model
    return pd.concat(out, ignore_index=True), fits
