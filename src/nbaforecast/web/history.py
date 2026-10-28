"""Prediction history page: every logged forecast, its result, and accuracy by bucket and team."""

from typing import Any

from nbaforecast import metrics
from nbaforecast.pipeline import shadow_v2
from nbaforecast.pipeline.live import Store
from nbaforecast.web import fan


# --- prediction history -------------------------------------------------------------------
def history(store: Store, season: str, team_map: dict[str, fan.Team]) -> dict[str, Any]:
    results = {r["game_id"]: r for r in store.read(store.results_path(season))}
    challenger = {
        gid: float(r["p_home"]) for gid, r in shadow_v2.load_log(store.root, season).items()
    }
    games = []
    for p in store.read(store.predictions_path(season)):
        r = results.get(p["game_id"])
        if r is None:
            continue
        home, away = team_map[p["home_team_id"]], team_map[p["away_team_id"]]
        ph = float(p["p_home"])
        home_won = int(r["home_score"]) > int(r["away_score"])
        pick = home if ph >= 0.5 else away
        games.append({
            "date": p["game_date"], "home": home, "away": away, "p_home": ph,
            "pick": pick, "pick_p": max(ph, 1 - ph), "correct": (ph >= 0.5) == home_won,
            "home_score": int(r["home_score"]), "away_score": int(r["away_score"]),
            "market": float(p["market_p_home"]) if p["market_p_home"] else None,
            "challenger": challenger.get(p["game_id"]), "home_won": home_won,
        })  # fmt: skip
    games.sort(key=lambda g: g["date"], reverse=True)
    n = len(games)
    summary = None
    if n:
        probs = [g["p_home"] for g in games]
        ys = [int(g["home_won"]) for g in games]
        summary = {
            "games": n,
            "correct": sum(g["correct"] for g in games),
            "accuracy": sum(g["correct"] for g in games) / n,
            "log_loss": metrics.log_loss(probs, ys),
            "brier": metrics.brier_score(probs, ys),
        }
    buckets = []
    for lo, hi, label in ((0.5, 0.6, "50–60%"), (0.6, 0.7, "60–70%"), (0.7, 0.8, "70–80%"),
                          (0.8, 1.01, "80%+")):  # fmt: skip
        sel = [g for g in games if lo <= g["pick_p"] < hi]
        if sel:
            buckets.append({"label": label, "games": len(sel),
                            "said": sum(g["pick_p"] for g in sel) / len(sel),
                            "right": sum(g["correct"] for g in sel) / len(sel)})  # fmt: skip
    teams = []
    for tid, team in team_map.items():
        sel = [g for g in games if tid in (g["home"].team_id, g["away"].team_id)]
        if not sel:
            continue
        teams.append({"team": team, "games": len(sel), "correct": sum(g["correct"] for g in sel),
                      "accuracy": sum(g["correct"] for g in sel) / len(sel),
                      "upsets": sum(1 for g in sel if not g["correct"])})  # fmt: skip
    teams.sort(key=lambda t: (-t["accuracy"], -t["games"]))
    return {"games": games, "summary": summary, "buckets": buckets, "teams": teams}
