"""Shadow mode for the hybrid model: live predictions logged alongside Elo, never published as
the main forecast, so it earns (or doesn't earn) promotion on games nobody had seen.

The daily job runs `nbaf shadow` right after `nbaf daily`, against the same `data` branch:

    state/hybrid_state.json            player ratings and team form, through `last_date`
    shadow/hybrid/<season>.csv         append-only; one row per game, written before tip-off
    scorecard/<season>-hybrid.json     hybrid vs. Elo (and the market) on the same games

Each run (1) ingests box scores for every game Elo has ingested since `last_date`, using the
same transforms as the dbt models, then (2) predicts today's games from start-of-day state,
reusing the exact Elo numbers in this morning's prediction log. The fitted coefficients live on
`main` (data/published/hybrid/model.json, from `nbaf hybrid-fit`), so a model change is a PR.
"""

import copy
import csv
import json
import logging
import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import requests

from nbaforecast import config, live, metrics
from nbaforecast.ingest.boxscores import parse_boxscore
from nbaforecast.models import hybrid, players
from nbaforecast.models.elo import EloState
from nbaforecast.models.hybrid import FORM_COLUMNS, Fit, FitParams, FormState
from nbaforecast.models.players import PlayerParams, PlayerState

log = logging.getLogger(__name__)

MODEL_NAME = "hybrid-realistic-v1"
MODEL_PATH = config.PUBLISHED_DIR / "hybrid" / "model.json"
SHADOW_FIELDS = [
    "game_id", "season", "season_type", "game_date", "tipoff_utc", "home_team_id",
    "away_team_id", "home_team", "away_team", "is_neutral_site", "p_home", "elo_p_home",
    "production_diff", "impact_diff", "continuity_home", "continuity_away", "model",
    "model_fit_through", "ratings_through", "predicted_at", "code_sha",
]  # fmt: skip
SummaryFetcher = Callable[[str], dict[str, Any]]


# --- the model file (on main) -------------------------------------------------------------
class Model:
    def __init__(self, fit: Fit, player_params: PlayerParams, form_decay: float, meta: dict):
        self.fit, self.player_params, self.form_decay, self.meta = (
            fit, player_params, form_decay, meta
        )  # fmt: skip

    def to_dict(self) -> dict:
        return {
            **self.meta,
            "name": MODEL_NAME,
            "features": self.fit.features,
            "regression": {
                "link": self.fit.params.link,
                "l2": self.fit.params.l2,
                "intercept": self.fit.params.intercept,
            },  # fmt: skip
            "mean": self.fit.mean.round(10).tolist(),
            "sd": self.fit.sd.round(10).tolist(),
            "coef": self.fit.coef.round(10).tolist(),
            "sigma": self.fit.sigma,
            "players": asdict(self.player_params),
            "form_decay": self.form_decay,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Model":
        fit = Fit(
            FitParams(**data["regression"]), data["features"], np.array(data["mean"]),
            np.array(data["sd"]), np.array(data["coef"]), data["sigma"],
        )  # fmt: skip
        meta = {k: data[k] for k in ("fit_seasons", "fit_through", "created_at") if k in data}
        return cls(fit, PlayerParams(**data["players"]), data["form_decay"], meta)

    @classmethod
    def load(cls, path: Path = MODEL_PATH) -> "Model":
        return cls.from_dict(json.loads(Path(path).read_text()))


def fit_model(games, team_games, player_games, chosen: dict, last_season: str) -> Model:
    """Fit the stack on every completed season (2005-06 through `last_season`) with the
    settings the backtest chose (data/published/hybrid/summary.json -> "chosen")."""
    player_params = PlayerParams(**chosen["players"])
    form_decay = chosen["form_decay"]
    feats = hybrid.build_features(
        games, team_games, players.walk(player_games, player_params), live.LIVE_PARAMS,
        form_decay,
    )  # fmt: skip
    seasons = [s for s in feats["season"].unique()
               if hybrid.FIRST_TRAIN_SEASON <= s <= last_season]  # fmt: skip
    train = feats[feats["season"].isin(seasons)]
    fit = hybrid.fit(train, hybrid.feature_set("realistic"), FitParams(**chosen["regression"]))
    meta = {
        "fit_seasons": [min(seasons), max(seasons)],
        "fit_through": str(max(train["game_date"])),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    return Model(fit, player_params, form_decay, meta)


# --- box scores, exactly as the dbt models transform them ---------------------------------
def load_aliases() -> dict[str, tuple[str, str]]:
    path = config.REPO_ROOT / "dbt" / "seeds" / "player_id_aliases.csv"
    with open(path) as f:
        return {r["alias_id"]: (r["player_id"], r["player"]) for r in csv.DictReader(f)}


def game_score(r: dict) -> float:
    """Hollinger's Game Score, as in fct_player_games."""
    return round(
        r["pts"] + 0.4 * r["fgm"] - 0.7 * r["fga"] - 0.4 * (r["fta"] - r["ftm"])
        + 0.7 * r["oreb"] + 0.3 * r["dreb"] + r["stl"] + 0.7 * r["ast"] + 0.7 * r["blk"]
        - 0.4 * r["pf"] - r["tov"],
        1,
    )  # fmt: skip


def clean_box(rows: list[dict], aliases: dict) -> dict[str, list[SimpleNamespace]]:
    """parse_boxscore rows -> per-team rows as stg_espn_player_games/fct_player_games build
    them: alias ids mapped, duplicate listings merged (the row with minutes wins), a team's
    blank box score flagged stats_missing with stats nulled, Game Score added."""
    best: dict[tuple[str, str], dict] = {}
    for r in rows:
        r = dict(r)
        if r["player_id"] in aliases:
            r["player_id"], r["player"] = aliases[r["player_id"]]
        key = (r["team_id"], r["player_id"])
        if key not in best or (r["minutes"], r["pts"]) > (best[key]["minutes"], best[key]["pts"]):
            best[key] = r
    teams: dict[str, list[dict]] = defaultdict(list)
    for r in best.values():
        teams[r["team_id"]].append(r)
    out = {}
    for team, team_rows in teams.items():
        blank = (
            max(r["minutes"] + r["pts"] + r["fga"] + r["reb"] + r["ast"] for r in team_rows) == 0
        )
        out[team] = []
        for r in team_rows:
            row = {**r, "stats_missing": blank}
            if blank:
                row.update(played=not r["did_not_play"], game_score=None, minutes=None,
                           plus_minus=None)  # fmt: skip
            else:
                row.update(played=r["minutes"] > 0 and not r["did_not_play"],
                           game_score=game_score(r))  # fmt: skip
            out[team].append(SimpleNamespace(**row))
    return out


def form_values(team: list, opp: list, pts: int, opp_pts: int) -> np.ndarray | None:
    """One team's FORM_COLUMNS for a game, as fct_team_games + warehouse.load_team_games
    compute them. None if either box score has no stats (fct_team_games skips those games)."""
    if any(r.stats_missing for r in team + opp):
        return None
    t = {k: sum(getattr(r, k) for r in team) for k in
         ("fgm", "fga", "fg3m", "fta", "ftm", "oreb", "dreb", "tov")}  # fmt: skip
    o = {k: sum(getattr(r, k) for r in opp) for k in t}
    poss = ((t["fga"] + 0.44 * t["fta"] - t["oreb"] + t["tov"])
            + (o["fga"] + 0.44 * o["fta"] - o["oreb"] + o["tov"])) / 2  # fmt: skip
    if poss <= 0 or not t["fga"] or not o["fga"]:
        return None

    def efg(x):
        return round((x["fgm"] + 0.5 * x["fg3m"]) / x["fga"], 4)

    def tov_rate(x):
        return round(x["tov"] / (x["fga"] + 0.44 * x["fta"] + x["tov"]), 4)

    def oreb_rate(x, other):
        return round(x["oreb"] / (x["oreb"] + other["dreb"]), 4)

    return np.array([
        round(100 * (pts - opp_pts) / poss, 1),
        efg(t) - efg(o),
        tov_rate(o) - tov_rate(t),
        oreb_rate(t, o) - oreb_rate(o, t),
        round(t["ftm"] / t["fga"], 4) - round(o["ftm"] / o["fga"], 4),
    ])  # fmt: skip


# --- state on the data branch -------------------------------------------------------------
class ShadowState:
    def __init__(self, players_state: PlayerState, form: FormState, last_date: date | None):
        self.players, self.form, self.last_date = players_state, form, last_date

    def to_dict(self) -> dict:
        return {
            "model": MODEL_NAME,
            "last_date": self.last_date.isoformat() if self.last_date else None,
            "form": self.form.to_dict(),
            "players": self.players.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ShadowState":
        return cls(
            PlayerState.from_dict(data["players"]),
            FormState.from_dict(data["form"]),
            date.fromisoformat(data["last_date"]) if data["last_date"] else None,
        )


def state_path(root: Path) -> Path:
    return Path(root) / "state" / "hybrid_state.json"


def log_path(root: Path, season: str) -> Path:
    return Path(root) / "shadow" / "hybrid" / f"{season}.csv"


def scorecard_path(root: Path, season: str) -> Path:
    return Path(root) / "scorecard" / f"{season}-hybrid.json"


def save_state(root: Path, state: ShadowState) -> None:
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), separators=(",", ":"), sort_keys=True) + "\n")


def load_state(root: Path) -> ShadowState:
    path = state_path(root)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found: run `nbaf shadow-bootstrap` first")
    return ShadowState.from_dict(json.loads(path.read_text()))


def bootstrap(
    root: Path, model: Model, games, team_games, player_games, through: date
) -> ShadowState:
    """First state: walk the local history up to `through` (inclusive), as the backtest does."""
    if state_path(root).exists():
        raise FileExistsError(f"{state_path(root)} already exists; refusing to overwrite it")
    state = ShadowState(PlayerState(model.player_params), FormState(model.form_decay), through)
    players.walk(player_games[player_games["game_date"] <= through], state=state.players)
    hybrid.team_form(
        games[games["game_date"] <= through], team_games[team_games["game_date"] <= through],
        decay=model.form_decay, state=state.form,
    )  # fmt: skip
    save_state(root, state)
    return state


# --- the daily run ------------------------------------------------------------------------
def ingest(
    store: live.Store, state: ShadowState, fetch_summary: SummaryFetcher, through: date
) -> dict[str, int]:
    """Apply box scores for every game in the results log after state.last_date, up to
    `through` (Elo's ratings_through), date by date like the backtest."""
    aliases = load_aliases()
    seasons = {live.season_for(state.last_date), live.season_for(through)}
    results = [r for s in sorted(seasons) for r in store.read(store.results_path(s))
               if state.last_date.isoformat() < r["game_date"] <= through.isoformat()]  # fmt: skip
    results.sort(key=lambda r: (r["game_date"], r["tipoff_utc"], r["game_id"]))
    counts = {"games": 0, "box_scores": 0, "missing": 0}
    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_date[r["game_date"]].append(r)
    for day, day_games in by_date.items():
        season = day_games[0]["season"]
        state.players.start_season(season)
        state.form.start_season(season)
        boxes = []
        for g in day_games:
            counts["games"] += 1
            try:
                summary = fetch_summary(g["game_id"])
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code != 404:
                    raise
                summary = None
            game = {**g, "game_date": date.fromisoformat(day)}
            box = clean_box(parse_boxscore(summary, game), aliases) if summary else {}
            if len(box) != 2:
                counts["missing"] += 1
                log.warning("%s: no usable box score; ratings unchanged by this game", g["game_id"])
                continue
            counts["box_scores"] += 1
            boxes.append((g, box))
        for g, box in boxes:  # same order as the backtest: every game's players, then form
            for team in (g["home_team_id"], g["away_team_id"]):
                state.players.record(team, box[team])
        for g, box in boxes:
            home, away = g["home_team_id"], g["away_team_id"]
            hs, as_ = int(g["home_score"]), int(g["away_score"])
            for team, opp, pts, opp_pts in ((home, away, hs, as_), (away, home, as_, hs)):
                values = form_values(box[team], box[opp], pts, opp_pts)
                if values is not None:
                    state.form.update(team, values)
    state.last_date = through
    return counts


def features_for(
    pstate: PlayerState, form: FormState, elo_row: dict, home_b2b: bool, away_b2b: bool
) -> dict[str, float]:
    """The realistic hybrid's features for one game, built as hybrid.build_features does."""
    home, away = elo_row["home_team_id"], elo_row["away_team_id"]
    ph, pa = pstate.realistic(home), pstate.realistic(away)
    form_diff = form.value(home) - form.value(away)
    strength = (float(elo_row["home_elo"]) - float(elo_row["away_elo"])) * hybrid.ELO_TO_LOGIT
    turnover = 1 - (ph["continuity"] + pa["continuity"]) / 2
    return {
        "elo_strength": strength,
        "elo_home": float(elo_row["home_court"]) * hybrid.ELO_TO_LOGIT,
        "home_b2b": float(home_b2b),
        "away_b2b": float(away_b2b),
        **dict(zip(FORM_COLUMNS, form_diff.tolist(), strict=True)),
        "production_real": ph["production"] - pa["production"],
        "impact_real": ph["impact"] - pa["impact"],
        "elo_x_turnover": strength * turnover,
        "continuity_home": ph["continuity"],
        "continuity_away": pa["continuity"],
    }


def predict(
    store: live.Store,
    state: ShadowState,
    model: Model,
    elo_state: EloState,
    today: date,
    now: datetime,
    code_sha: str,
) -> tuple[list[dict], list[str]]:
    """Shadow predictions for today's games in Elo's log that haven't tipped off yet."""
    season = live.season_for(today)
    elo_rows = [r for r in store.read(store.predictions_path(season))
                if r["game_date"] == today.isoformat()]  # fmt: skip
    already = {r["game_id"] for r in store.read(log_path(store.root, season))}
    pstate, form = copy.deepcopy(state.players), copy.deepcopy(state.form)
    pstate.start_season(season)  # start-of-day state; the saved state isn't advanced
    form.start_season(season)
    rows, started = [], []
    for r in elo_rows:
        if r["game_id"] in already:
            continue
        if datetime.fromisoformat(r["tipoff_utc"]) <= now:
            started.append(r["game_id"])
            continue
        f = features_for(
            pstate, form, r,
            elo_state.back_to_back(r["home_team_id"], today),
            elo_state.back_to_back(r["away_team_id"], today),
        )  # fmt: skip
        p = float(model.fit.predict(pd.DataFrame([f]))[0])
        rows.append({
            **{k: r[k] for k in ("game_id", "season", "season_type", "game_date", "tipoff_utc",
                                 "home_team_id", "away_team_id", "home_team", "away_team",
                                 "is_neutral_site")},
            "p_home": round(p, 6),
            "elo_p_home": r["p_home"],
            "production_diff": round(f["production_real"], 3),
            "impact_diff": round(f["impact_real"], 3),
            "continuity_home": round(f["continuity_home"], 3),
            "continuity_away": round(f["continuity_away"], 3),
            "model": MODEL_NAME,
            "model_fit_through": model.meta.get("fit_through", ""),
            "ratings_through": state.last_date.isoformat() if state.last_date else "",
            "predicted_at": now.isoformat(timespec="seconds"),
            "code_sha": code_sha,
        })  # fmt: skip
    live.Store.append(log_path(store.root, season), SHADOW_FIELDS, rows)
    return rows, started


def _score(probs: list[float], outcomes: list[int]) -> dict[str, float] | None:
    if not probs:
        return None
    return {
        "log_loss": round(metrics.log_loss(probs, outcomes), 5),
        "brier": round(metrics.brier_score(probs, outcomes), 5),
        "accuracy": round(metrics.accuracy(probs, outcomes), 5),
    }


# Pre-registered on 2026-10-27, before any live shadow prediction: docs/promotion-bar-hybrid.md.
# Don't change these to fit results; a different bar needs a new model version and window.
PROMOTION = {
    "margin": 0.005,  # promote only if the 95% CI's lower end is above -margin
    "coverage": 0.90,  # share of the published model's regular-season games the hybrid predicted
    "reliability_gap": 0.003,  # hybrid reliability may exceed the published model's by this much
    "window": ["2026-27"],  # regular season only
    "extension": ["2026-27", "2027-28"],  # pooled, if the first window is inconclusive
}


def _log_loss_one(p: float, y: int) -> float:
    p = min(max(p, metrics.EPS), 1 - metrics.EPS)
    return -math.log(p if y == 1 else 1 - p)


def promotion_check(
    store: live.Store,
    seasons: list[str],
    log: Callable[[Path, str], Path] | None = None,
    promotion: dict | None = None,
    rule: str = "docs/promotion-bar-hybrid.md",
) -> dict:
    """The pre-registered promotion rule, evaluated on the regular seasons in `seasons`.
    Status stays "in progress" until the last season's regular season is over (its first
    play-in or playoff result has been ingested). `log`, `promotion` and `rule` default to the
    v1 hybrid's; other shadow models pass their own."""
    log = log or log_path
    promotion = promotion or PROMOTION
    pairs, published_games = [], 0
    over = False
    for season in seasons:
        results = {r["game_id"]: r for r in store.read(store.results_path(season))}
        over = any(r["season_type"] in ("playin", "playoffs") for r in results.values())
        shadow_rows = {r["game_id"]: r for r in store.read(log(store.root, season))
                       if r["season_type"] == "regular_season"}  # fmt: skip
        if not shadow_rows:
            continue
        start = min(r["game_date"] for r in shadow_rows.values())
        for e in store.read(store.predictions_path(season)):
            r = results.get(e["game_id"])
            if e["season_type"] != "regular_season" or e["game_date"] < start or r is None:
                continue
            published_games += 1
            s = shadow_rows.get(e["game_id"])
            if s is not None:
                y = int(int(r["home_score"]) > int(r["away_score"]))
                pairs.append((float(e["p_home"]), float(s["p_home"]), y))
    check: dict[str, Any] = {"seasons": seasons, "rule": rule, "games": len(pairs),
                             "status": "not started"}  # fmt: skip
    if len(pairs) < 2:
        return check
    diffs = [_log_loss_one(pub, y) - _log_loss_one(hyb, y) for pub, hyb, y in pairs]
    n = len(diffs)
    mean = sum(diffs) / n
    sd = math.sqrt(sum((d - mean) ** 2 for d in diffs) / (n - 1))
    lo, hi = mean - 1.96 * sd / math.sqrt(n), mean + 1.96 * sd / math.sqrt(n)
    y = [x[2] for x in pairs]
    rel_pub = metrics.brier_decomposition([x[0] for x in pairs], y)["reliability"]
    rel_hyb = metrics.brier_decomposition([x[1] for x in pairs], y)["reliability"]
    coverage = n / published_games if published_games else 0.0
    passes = {
        "better_on_average": mean > 0,
        "not_meaningfully_worse": lo > -promotion["margin"],
        "coverage": coverage >= promotion["coverage"],
        "calibration": rel_hyb - rel_pub <= promotion["reliability_gap"],
    }
    if not over:
        status = "in progress"
    elif all(passes.values()):
        status = "promote"
    elif hi < 0:
        status = "retire"
    elif seasons == promotion["window"]:
        status = "extend"
    else:
        status = "not promoted"
    return {
        **check,
        "status": status,
        "regular_season_over": over,
        "mean_difference": round(mean, 5),  # published minus hybrid; positive = hybrid better
        "ci95": [round(lo, 5), round(hi, 5)],
        "coverage": round(coverage, 4),
        "reliability": {"hybrid": round(rel_hyb, 5), "published": round(rel_pub, 5)},
        "conditions": passes,
    }


def _promotion_seasons(season: str, promotion: dict | None = None) -> list[str] | None:
    promotion = promotion or PROMOTION
    for key in ("window", "extension"):
        if promotion[key][-1] == season:
            return promotion[key]
    return None


def scorecard(
    store: live.Store,
    season: str,
    now: datetime,
    log: Callable[[Path, str], Path] | None = None,
    promotion: dict | None = None,
    rule: str = "docs/promotion-bar-hybrid.md",
    model_name: str = MODEL_NAME,
) -> dict:
    """Hybrid vs. Elo on every game both predicted and that has a result; vs. the market on
    the games that also had a line. Other shadow models pass their own log, bar and name."""
    log = log or log_path
    results = {r["game_id"]: r for r in store.read(store.results_path(season))}
    elo_rows = {r["game_id"]: r for r in store.read(store.predictions_path(season))}
    shadow_rows = store.read(log(store.root, season))
    scored = []
    for s in shadow_rows:
        r, e = results.get(s["game_id"]), elo_rows.get(s["game_id"])
        if r is not None and e is not None:
            scored.append((float(s["p_home"]), float(e["p_home"]), e["market_p_home"],
                           int(int(r["home_score"]) > int(r["away_score"]))))  # fmt: skip
    y = [x[3] for x in scored]
    market = [x for x in scored if x[2] != ""]
    return {
        "season": season,
        "model": model_name,
        "updated_at": now.isoformat(timespec="seconds"),
        "games_predicted": len(shadow_rows),
        "games_scored": len(scored),
        "hybrid": _score([x[0] for x in scored], y),
        "elo": _score([x[1] for x in scored], y),
        "head_to_head_with_market": {
            "games": len(market),
            "hybrid": _score([x[0] for x in market], [x[3] for x in market]),
            "elo": _score([x[1] for x in market], [x[3] for x in market]),
            "market": _score([float(x[2]) for x in market], [x[3] for x in market]),
        },
        "promotion_check": (
            promotion_check(store, window, log, promotion, rule)
            if (window := _promotion_seasons(season, promotion))
            else None
        ),
    }


def run(
    root: Path,
    *,
    today: date,
    now: datetime,
    fetch_summary: SummaryFetcher,
    model: Model,
    code_sha: str,
) -> dict:
    """One daily shadow run, after `nbaf daily` has updated Elo and logged today's games."""
    store = live.Store(root)
    elo_state = store.load_state()
    state = load_state(root)
    counts = {"games": 0, "box_scores": 0, "missing": 0}
    if elo_state.last_date and elo_state.last_date > state.last_date:
        counts = ingest(store, state, fetch_summary, elo_state.last_date)
    rows, started = predict(store, state, model, elo_state, today, now, code_sha)
    save_state(root, state)
    season = live.season_for(today)
    card = scorecard(store, season, now)
    scorecard_path(root, season).parent.mkdir(parents=True, exist_ok=True)
    scorecard_path(root, season).write_text(json.dumps(card, indent=2) + "\n")
    return {
        **{f"ingested_{k}": v for k, v in counts.items()},
        "ratings_through": state.last_date.isoformat(),
        "predicted": len(rows),
        "already_started": len(started),
        "scorecard": card,
    }
