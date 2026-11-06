"""Scorecard page: this season's record for each forecast (Elo, the challenger, the betting
market), night by night, and what goes into each one.

The cards are rendered at build time; the night picker, the model pop-up and its filters
(`static/scorecard.js`) read the same finished games from JSON embedded in the page.
"""

import json
import math
from datetime import date
from pathlib import Path
from typing import Any

from nbaforecast import metrics
from nbaforecast.pipeline import live, projections, shadow_v2
from nbaforecast.pipeline.live import Store
from nbaforecast.web import fan

# key in each game row, name, role, color series
MODELS = [
    ("elo", "Elo", "Published", "s1"),
    ("v2", "Player + injury model", "Challenger", "s3"),
    ("mkt", "Betting market", "Benchmark", "s2"),
]

# Hybrid v2's inputs in plain words: (label, note, group). Every feature in its model.json
# needs an entry (tests/test_web.py checks).
V2_FEATURES = {
    "elo_strength": ("Elo rating gap", "Team strength from results", "Team"),
    "elo_home": ("Home court", "The same estimate Elo uses", "Team"),
    "net_rating": ("Recent net rating", "Per 100 possessions, recent games weighted most", "Team"),
    "production_lineup": ("Tonight's box-score production",
                          "Available players' Game Score per minute", "Players tonight"),
    "rapm_d_lineup": ("Tonight's defense (RAPM)", "Available players, by expected minutes",
                      "Players tonight"),
    "rapm_o_lineup": ("Tonight's offense (RAPM)", "Available players, by expected minutes",
                      "Players tonight"),
    "absent_rapm": ("Value of who's missing", "Injured regulars' impact, by their usual minutes",
                    "Players tonight"),
    "tov_diff": ("Turnovers", "Recent games", "Recent form"),
    "efg_diff": ("Shooting (eFG%)", "Recent games", "Recent form"),
    "oreb_diff": ("Offensive rebounding", "Recent games", "Recent form"),
    "ft_diff": ("Free throw rate", "Recent games", "Recent form"),
    "away_b2b": ("Away team on a back-to-back", "", "Schedule and roster"),
    "home_b2b": ("Home team on a back-to-back", "", "Schedule and roster"),
    "elo_x_turnover_c": ("Elo × roster turnover", "Trusts Elo less after big roster changes",
                         "Schedule and roster"),
}  # fmt: skip


def finished_games(store: Store, season: str, team_map: dict[str, fan.Team]) -> list[dict]:
    """Every finished game with a logged Elo forecast, and the challenger's and market's where
    they exist. Compact keys: this list is embedded in the page."""
    results = {r["game_id"]: r for r in store.read(store.results_path(season))}
    v2 = shadow_v2.load_log(store.root, season)
    rows = []
    for p in store.read(store.predictions_path(season)):
        r = results.get(p["game_id"])
        if r is None:
            continue
        c = v2.get(p["game_id"])
        rows.append({
            "date": p["game_date"],
            "home": team_map[p["home_team_id"]].abbr, "away": team_map[p["away_team_id"]].abbr,
            "hs": int(r["home_score"]), "as": int(r["away_score"]),
            "elo": round(float(p["p_home"]), 4),
            "v2": round(float(c["p_home"]), 4) if c else None,
            "mkt": round(float(p["market_p_home"]), 4) if p["market_p_home"] else None,
            "books": int(p["market_books"] or 0),
            "gap": round(float(p["home_elo"]) - float(p["away_elo"]), 1),
        })  # fmt: skip
    rows.sort(key=lambda g: g["date"], reverse=True)
    return rows


def record(rows: list[dict], key: str) -> dict[str, Any] | None:
    """Games, picks right, log-loss and Brier score for one forecast over `rows`."""
    rows = [g for g in rows if g[key] is not None]
    if not rows:
        return None
    probs = [g[key] for g in rows]
    ys = [int(g["hs"] > g["as"]) for g in rows]
    ll = metrics.log_loss(probs, ys)
    return {
        "games": len(rows),
        "right": sum((p >= 0.5) == bool(y) for p, y in zip(probs, ys, strict=True)),
        "log_loss": ll,
        "brier": metrics.brier_score(probs, ys),
        "skill": max(0.0, 1 - ll / math.log(2)),  # share of a coin flip's log-loss saved
    }


def _v2_inputs(published: Path) -> list[dict] | None:
    path = published / "hybrid_v2" / "model.json"
    if not path.exists():
        return None
    model = json.loads(path.read_text())
    coef = dict(zip(model["features"], model["coef"], strict=True))
    total = sum(abs(v) for v in coef.values())
    groups: dict[str, dict] = {}
    for feature, (label, note, group) in V2_FEATURES.items():
        g = groups.setdefault(group, {"group": group, "share": 0.0, "inputs": []})
        g["inputs"].append({"label": label, "note": note, "weight": round(coef[feature], 4)})
        g["share"] += abs(coef[feature]) / total
    return [{**g, "fit": model["fit_seasons"], "l2": model["regression"]["l2"]}
            for g in groups.values()]  # fmt: skip


def scorecard(
    store: Store, season: str, team_map: dict[str, fan.Team], published: Path
) -> dict[str, Any] | None:
    games = finished_games(store, season, team_map)
    if not games:
        return None
    common = [g for g in games if all(g[k] is not None for k, *_ in MODELS)]
    latest = games[0]["date"]
    last_night = [g for g in games if g["date"] == latest]
    cards = []
    for key, name, role, cls in MODELS:
        card = record(common, key)
        if card is None:
            continue
        cards.append({"key": key, "name": name, "role": role, "cls": cls, **card,
                      "last": record(last_night, key)})  # fmt: skip
    best = min((c["log_loss"] for c in cards), default=None)
    for c in cards:
        c["leads"] = len(cards) > 1 and c["log_loss"] == best
    params = live.LIVE_PARAMS
    books = [g["books"] for g in games if g["mkt"] is not None]
    state = store.load_state()
    return {
        "season": season,
        "games": games,
        "common": len(common),
        "cards": cards,
        "latest": latest,
        "last_label": f"{date.fromisoformat(latest):%a} night",
        "logos": {t.abbr: t.logo for t in team_map.values()},
        "how": {
            "elo": {
                "avg_gap": sum(abs(g["gap"]) for g in games) / len(games),
                "home_court": state.home_court(),
                "window": params.hca_window,
                "k": params.k,
                "regress": round(1 - params.carryover, 2),
                "per_point": projections.ELO_PER_POINT,
            },
            "v2": _v2_inputs(published),
            "mkt": {"min": min(books), "max": max(books), "avg": sum(books) / len(books)}
            if books
            else None,
        },  # fmt: skip
        "rule": shadow_v2.RULE,
    }
