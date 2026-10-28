"""Hybrid v2 in shadow mode: the hybrid with RAPM and who's missing, using the injury report.

Chosen in `docs/findings/impact-challenger.md` (feature selection agreed 2026-10-28):
team history (Elo strength, adaptive home court, back-to-backs, all five form features), a
centered Elo x roster-turnover interaction, and tonight's lineup: RAPM offense and defense,
Game Score production, and the value above replacement of the expected minutes that are
missing. Logistic stack, no intercept, ridge l2 = 10, fit on 2018-19 to 2025-26.

Live, "tonight's lineup" comes from the morning's injury report: ESPN status Out = not
playing; Day-To-Day = the injury watch's chance of playing (the agent's, else the keyword
rule's, else a coin flip). Players are weighted by their usual minutes times that chance.
RAPM is the site's daily fit (`players/impact.json`, the same window the backtest refits on).

Predictions go to `shadow/hybrid-v2/<season>.csv` (append-only, checked by `nbaf verify`) and
are judged by their own pre-registered bar: `docs/promotion-bar-hybrid-v2.md`.
"""

import copy
import json
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from nbaforecast import config
from nbaforecast.ingest import rosters
from nbaforecast.models import hybrid, impact
from nbaforecast.models.hybrid import FORM_COLUMNS, Fit, FitParams
from nbaforecast.models.players import PlayerState
from nbaforecast.pipeline import live, shadow

MODEL_NAME = "hybrid-lineup-v2"
MODEL_PATH = config.PUBLISHED_DIR / "hybrid_v2" / "model.json"
RULE = "docs/promotion-bar-hybrid-v2.md"
FEATURES = [*hybrid.TEAM_FEATURES, "rapm_o_lineup", "rapm_d_lineup", "production_lineup",
            "absent_rapm", "elo_x_turnover_c"]  # fmt: skip
FIT_FIRST, FIT_LAST = "2018-19", "2025-26"
MISSING_MINUTES = 15.0  # list absent players who usually play at least this much
# Pre-registered on 2026-10-28, before any live v2 prediction: docs/promotion-bar-hybrid-v2.md.
PROMOTION = {
    "margin": 0.005,
    "coverage": 0.90,
    "reliability_gap": 0.003,
    "window": ["2026-27"],
    "extension": ["2026-27", "2027-28"],
}
FIELDS = [
    "game_id", "season", "season_type", "game_date", "tipoff_utc", "home_team_id",
    "away_team_id", "home_team", "away_team", "is_neutral_site", "p_home", "elo_p_home",
    "rapm_o_diff", "rapm_d_diff", "production_diff", "absent_diff", "missing_home",
    "missing_away", "model", "model_fit_through", "ratings_through", "impact_through",
    "predicted_at", "code_sha",
]  # fmt: skip


def log_path(root: Path, season: str) -> Path:
    return Path(root) / "shadow" / "hybrid-v2" / f"{season}.csv"


def scorecard_path(root: Path, season: str) -> Path:
    return Path(root) / "scorecard" / f"{season}-hybrid-v2.json"


# --- the model file (on main) -------------------------------------------------------------
def fit_model(feats: pd.DataFrame, chosen: dict) -> dict[str, Any]:
    """Fit on every game from FIT_FIRST through FIT_LAST and return the model file."""
    train = feats[(feats["season"] >= FIT_FIRST) & (feats["season"] <= FIT_LAST)]
    fit = hybrid.fit(train, FEATURES, chosen["fit"])
    return {
        "name": MODEL_NAME,
        "features": FEATURES,
        "regression": asdict(chosen["fit"]),
        "mean": fit.mean.round(10).tolist(),
        "sd": fit.sd.round(10).tolist(),
        "coef": fit.coef.round(10).tolist(),
        "sigma": fit.sigma,
        "players": asdict(chosen["players"]),
        "form_decay": chosen["form_decay"],
        "turnover_center": hybrid.TURNOVER_CENTER,
        "unknown_rating": list(impact.UNKNOWN),
        "fit_seasons": [FIT_FIRST, FIT_LAST],
        "fit_through": str(max(train["game_date"])),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "coefficients": fit.coefficients(),
    }


def load_model(path: Path = MODEL_PATH) -> tuple[Fit, dict]:
    m = json.loads(Path(path).read_text())
    fit = Fit(FitParams(**m["regression"]), m["features"], np.array(m["mean"]),
              np.array(m["sd"]), np.array(m["coef"]), m["sigma"])  # fmt: skip
    return fit, m


# --- tonight's lineup ---------------------------------------------------------------------
def chances(directory: dict[str, dict], calls: dict[str, float], team: str) -> dict[str, float]:
    """Chance each player on the team's current roster plays tonight."""
    out = {}
    for pid, info in directory.items():
        if info["team_id"] != team:
            continue
        status = info.get("status", "")
        out[pid] = (
            0.0 if status == "Out" else calls.get(pid, 0.5) if status == "Day-To-Day" else 1.0
        )
    return out


def team_lineup(
    pstate: PlayerState, team: str, chance: dict[str, float], ratings: dict, unknown: tuple
) -> dict[str, Any]:
    """The v2 team features from the injury report, as `PlayerState.rapm_features` builds them
    from box scores in the backtest, with each player weighted by his chance of playing."""
    # whole roster: players who played for the team and are still on it, by availability
    real = {p: pstate.availability(p) for p in pstate.rosters[team] if p in chance}
    lineup = {p: pstate.role(p) * c for p, c in chance.items() if c > 0}
    o, d = PlayerState._rapm(lineup, ratings, unknown)
    floor = sum(unknown)
    lost = sum((sum(ratings.get(p, unknown)) - floor) * m * (1 - chance[p])
               for p, m in real.items()) / 48  # fmt: skip
    missing = sorted((p for p in real if chance[p] < 0.5 and pstate.role(p) >= MISSING_MINUTES),
                     key=lambda p: -pstate.role(p))  # fmt: skip
    return {
        "rapm_o_lineup": o,
        "rapm_d_lineup": d,
        "production_lineup": pstate._strength(lineup, team)["production"],
        "absent_rapm": lost,
        "continuity": pstate.realistic(team)["continuity"],
        "missing": missing,
    }


def features_for(
    pstate, form, elo_row: dict, home_b2b: bool, away_b2b: bool, home: dict, away: dict,
    center: float,
) -> dict[str, float]:  # fmt: skip
    form_diff = form.value(elo_row["home_team_id"]) - form.value(elo_row["away_team_id"])
    strength = (float(elo_row["home_elo"]) - float(elo_row["away_elo"])) * hybrid.ELO_TO_LOGIT
    turnover = 1 - (home["continuity"] + away["continuity"]) / 2
    return {
        "elo_strength": strength,
        "elo_home": float(elo_row["home_court"]) * hybrid.ELO_TO_LOGIT,
        "home_b2b": float(home_b2b),
        "away_b2b": float(away_b2b),
        **dict(zip(FORM_COLUMNS, form_diff.tolist(), strict=True)),
        **{
            k: home[k] - away[k]
            for k in ("rapm_o_lineup", "rapm_d_lineup", "production_lineup", "absent_rapm")
        },  # fmt: skip
        "elo_x_turnover_c": strength * (turnover - center),
    }


def _stale(updated_at: str | None, today: date) -> bool:
    """True unless the file was written today or yesterday (UTC dates; the daily job runs at
    14:00 UTC, so today's run writes today's date)."""
    if not updated_at:
        return True
    return (today - date.fromisoformat(updated_at[:10])).days > 1


def _directory_date(root: Path) -> str | None:
    p = rosters.directory_path(root)
    return json.loads(p.read_text()).get("updated_at") if p.exists() else None


def _calls(store: live.Store, season: str, today: date) -> dict[str, float]:
    log = store.availability_path(season)
    out = {}
    for r in store.read(log):
        if r["game_date"] == today.isoformat():
            p = r.get("agent_p") or r.get("baseline_p")
            if p not in (None, ""):
                out[r["player_id"]] = float(p)
    return out


def predict(
    root: Path, today: date, now: datetime, code_sha: str, model_path: Path = MODEL_PATH
) -> dict[str, Any]:
    """Log v2 predictions for today's games in Elo's log that haven't tipped off yet, then
    rewrite the v2 scorecard."""
    store = live.Store(root)
    elo_state = store.load_state()
    season = live.season_for(today)
    fit, m = load_model(model_path)
    unknown = tuple(m["unknown_rating"])
    state = shadow.load_state(root)
    pstate, form = copy.deepcopy(state.players), copy.deepcopy(state.form)
    pstate.start_season(season)
    form.start_season(season)
    imp = impact.load(root) or {}
    ratings = {p: impact.blend(r) for p, r in imp.get("players", {}).items()}
    directory = rosters.load_directory(root)
    stale = _stale(imp.get("updated_at"), today) or _stale(_directory_date(root), today)
    if not ratings or not directory or stale:
        # Pre-registered: no prediction rather than one built on missing or stale inputs.
        return {"predicted": 0, "already_started": 0, "rows": [],
                "skipped": "inputs missing or stale", "scorecard": None}  # fmt: skip
    names = {p: info["name"] for p, info in directory.items()}
    calls = _calls(store, season, today)
    already = {r["game_id"] for r in store.read(log_path(root, season))}
    rows, started = [], []
    for r in store.read(store.predictions_path(season)):
        if r["game_date"] != today.isoformat() or r["game_id"] in already:
            continue
        if datetime.fromisoformat(r["tipoff_utc"]) <= now:
            started.append(r["game_id"])
            continue
        sides = {t: team_lineup(pstate, t, chances(directory, calls, t), ratings, unknown)
                 for t in (r["home_team_id"], r["away_team_id"])}  # fmt: skip
        h, a = sides[r["home_team_id"]], sides[r["away_team_id"]]
        f = features_for(pstate, form, r, elo_state.back_to_back(r["home_team_id"], today),
                         elo_state.back_to_back(r["away_team_id"], today), h, a,
                         m["turnover_center"])  # fmt: skip
        p = float(fit.predict(pd.DataFrame([f]))[0])
        rows.append({
            **{k: r[k] for k in ("game_id", "season", "season_type", "game_date", "tipoff_utc",
                                 "home_team_id", "away_team_id", "home_team", "away_team",
                                 "is_neutral_site")},
            "p_home": round(p, 6), "elo_p_home": r["p_home"],
            "rapm_o_diff": round(f["rapm_o_lineup"], 3),
            "rapm_d_diff": round(f["rapm_d_lineup"], 3),
            "production_diff": round(f["production_lineup"], 3),
            "absent_diff": round(f["absent_rapm"], 3),
            "missing_home": "; ".join(names.get(x, x) for x in h["missing"]),
            "missing_away": "; ".join(names.get(x, x) for x in a["missing"]),
            "model": MODEL_NAME, "model_fit_through": m.get("fit_through", ""),
            "ratings_through": state.last_date.isoformat() if state.last_date else "",
            "impact_through": imp.get("updated_at", ""),
            "predicted_at": now.isoformat(timespec="seconds"), "code_sha": code_sha,
        })  # fmt: skip
    live.Store.append(log_path(root, season), FIELDS, rows)
    card = shadow.scorecard(store, season, now, log_path, PROMOTION, RULE, MODEL_NAME)
    scorecard_path(root, season).parent.mkdir(parents=True, exist_ok=True)
    scorecard_path(root, season).write_text(json.dumps(card, indent=2) + "\n")
    return {"predicted": len(rows), "already_started": len(started), "rows": rows,
            "scorecard": card}  # fmt: skip


def load_log(root: Path, season: str) -> dict[str, dict[str, str]]:
    return {r["game_id"]: r for r in live.Store.read(log_path(root, season))}
