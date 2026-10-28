"""Data for the player pop-up, team pages, prediction history, and The Rotation (the game).

Everything here is computed at build time from the data branch (plus small published files on
main); the pages make no API calls. Player pop-ups load `players/<id>.json` on click.
"""

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from nbaforecast import availability, boxlog, metrics, projections, seasonelo, shadow
from nbaforecast.ingest import rosters
from nbaforecast.live import Store
from nbaforecast.models.elo import win_probability
from nbaforecast.web import fan

HEADSHOT_BIG = "&w=260&h=190"  # pop-up portrait size on ESPN's resizer


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _abbr(team_map: dict[str, fan.Team], tid: str) -> str:
    return team_map[tid].abbr if tid in team_map else tid


# --- players ------------------------------------------------------------------------------
GAME_SCORE_PARTS = [  # (label, weight, stat) — Hollinger's Game Score, term by term
    ("Points", 1.0, "pts"), ("Field goals made", 0.4, "fgm"), ("Field goal attempts", -0.7, "fga"),
    ("Missed free throws", -0.4, "ftmiss"), ("Offensive rebounds", 0.7, "oreb"),
    ("Defensive rebounds", 0.3, "dreb"), ("Steals", 1.0, "stl"), ("Assists", 0.7, "ast"),
    ("Blocks", 0.7, "blk"), ("Personal fouls", -0.4, "pf"), ("Turnovers", -1.0, "tov"),
]  # fmt: skip


def player_payloads(
    root: Path, season: str, team_map: dict[str, fan.Team]
) -> dict[str, dict[str, Any]]:
    """One JSON payload per player on a current roster: profile, tonight's projection, this
    season's game log and stat line, career lines, and their average Game Score broken into
    its parts."""
    directory = rosters.load_directory(root)
    if not directory:
        return {}
    careers = boxlog.load_careers(root)
    rows = Store.read(boxlog.path(root, season))
    totals = boxlog.team_totals(rows)
    results = {r["game_id"]: r for r in Store.read(Store(root).results_path(season))}
    by_player: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_player[r["player_id"]].append(r)
    try:
        state = shadow.load_state(root).players
    except FileNotFoundError:
        state = None

    out = {}
    for pid, info in directory.items():
        team = team_map.get(info["team_id"])
        games = sorted(by_player.get(pid, []), key=lambda r: r["game_date"])
        log = []
        for r in games:
            res = results.get(r["game_id"])
            home = r["is_home"] == "True"
            if res:
                us, them = (res["home_score"], res["away_score"]) if home else (
                    res["away_score"], res["home_score"])  # fmt: skip
                result = f"{'W' if int(us) > int(them) else 'L'} {us}-{them}"
            else:
                result = ""
            played = r["stats_missing"] != "True" and _f(r["minutes"]) > 0
            log.append({
                "date": r["game_date"], "opp": _abbr(team_map, r["opponent_id"]), "home": home,
                "result": result, "played": played,
                "dnp": r["dnp_reason"] if not played else "",
                **({k: int(_f(r[k])) for k in ("minutes", "pts", "reb", "ast", "stl", "blk",
                                                "tov", "fgm", "fga", "fg3m", "fg3a", "ftm",
                                                "fta")} if played else {}),
                "pm": r["plus_minus"] if played else "",
                "gmsc": _f(r["game_score"]) if played else None,
            })  # fmt: skip
        tt = {r["game_id"]: totals[(r["game_id"], r["team_id"])] for r in games}
        line = boxlog.season_line(games, tt)
        history = list(careers.get(pid, []))
        if line.get("g"):
            history.append({"season": season, "team": team.abbr if team else "", **line})
        known = state is not None and pid in state.players
        minutes = state.role(pid) if known else None
        out[pid] = {
            "id": pid,
            "name": info["name"],
            "position": info["position"],
            "jersey": info["jersey"],
            "status": info["status"],
            "team": {"abbr": team.abbr, "name": team.name, "id": team.team_id} if team else None,
            "headshot": (fan.headshot_url(pid) or "").replace("&w=96&h=70", HEADSHOT_BIG) or None,
            "projection": (
                {
                    "minutes": round(minutes, 1),
                    "score": round(state.production(pid) * minutes, 1),
                    "per36": round(state.production(pid) * 36, 1),
                }  # fmt: skip
                if known
                else None
            ),
            "season": season,
            "line": line,
            "games": log,
            "career": history,
            "gmsc_parts": _gmsc_parts(games),
        }
    return out


def _gmsc_parts(games: list[dict]) -> list[dict] | None:
    played = [r for r in games if r["stats_missing"] != "True" and _f(r["minutes"]) > 0]
    if not played:
        return None
    n = len(played)
    avg = {
        k: sum(_f(r[k]) for r in played) / n
        for k in ("pts", "fgm", "fga", "oreb", "dreb", "stl", "ast", "blk", "pf", "tov")
    }
    avg["ftmiss"] = sum(_f(r["fta"]) - _f(r["ftm"]) for r in played) / n  # fmt: skip
    return [{"label": label, "weight": w, "per_game": round(avg[k], 1),
             "points": round(w * avg[k], 1)} for label, w, k in GAME_SCORE_PARTS]  # fmt: skip


def write_player_payloads(out_dir: Path, payloads: dict[str, dict]) -> int:
    folder = out_dir / "players"
    folder.mkdir(parents=True, exist_ok=True)
    for pid, payload in payloads.items():
        (folder / f"{pid}.json").write_text(json.dumps(payload, separators=(",", ":")))
    return len(payloads)


# --- team stats ---------------------------------------------------------------------------
# (key, label, side, higher_is_better, format)
TEAM_METRICS = [
    ("ortg", "Points per 100 possessions", "offense", True, "num1"),
    ("efg", "Shooting (effective FG%)", "offense", True, "pct1"),
    ("tov", "Ball security (turnover rate)", "offense", False, "pct1"),
    ("oreb", "Offensive rebounding", "offense", True, "pct1"),
    ("ftr", "Getting to the line (FT per shot)", "offense", True, "num3"),
    ("three_rate", "Three-point volume (3PA share)", "offense", True, "pct1"),
    ("drtg", "Points allowed per 100 possessions", "defense", False, "num1"),
    ("opp_efg", "Shooting allowed (opp. eFG%)", "defense", False, "pct1"),
    ("opp_tov", "Turnovers forced", "defense", True, "pct1"),
    ("dreb", "Defensive rebounding", "defense", True, "pct1"),
    ("opp_ftr", "Fouling (opp. FT per shot)", "defense", False, "num3"),
    ("pace", "Pace (possessions per 48)", "style", True, "num1"),
]


def team_stats(root: Path, season: str) -> dict[str, dict[str, float]]:
    """Season-to-date offense, defense and pace for every team, from the box-score log."""
    rows = Store.read(boxlog.path(root, season))
    tot: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    blank = set()
    for r in rows:
        key = (r["game_id"], r["team_id"])
        if r["stats_missing"] == "True":
            blank.add(r["game_id"])
            continue
        for k in ("pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "tov",
                  "minutes"):  # fmt: skip
            tot[key][k] += _f(r[k])
        tot[key]["opp"] = r["opponent_id"]
    season_tot: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for (gid, team), t in tot.items():
        if gid in blank or (gid, t["opp"]) not in tot:
            continue
        o = tot[(gid, t["opp"])]
        poss = ((t["fga"] + 0.44 * t["fta"] - t["oreb"] + t["tov"])
                + (o["fga"] + 0.44 * o["fta"] - o["oreb"] + o["tov"])) / 2  # fmt: skip
        s = season_tot[team]
        s["games"] += 1
        s["poss"] += poss
        s["minutes"] += t["minutes"]
        for k in ("pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "tov"):
            s[k] += t[k]
            s["o_" + k] += o[k]
    out = {}
    for team, s in season_tot.items():
        if not s["games"] or not s["fga"] or not s["o_fga"]:
            continue
        out[team] = {
            "games": s["games"],
            "ortg": 100 * s["pts"] / s["poss"],
            "drtg": 100 * s["o_pts"] / s["poss"],
            "efg": (s["fgm"] + 0.5 * s["fg3m"]) / s["fga"],
            "opp_efg": (s["o_fgm"] + 0.5 * s["o_fg3m"]) / s["o_fga"],
            "tov": s["tov"] / (s["fga"] + 0.44 * s["fta"] + s["tov"]),
            "opp_tov": s["o_tov"] / (s["o_fga"] + 0.44 * s["o_fta"] + s["o_tov"]),
            "oreb": s["oreb"] / (s["oreb"] + s["o_dreb"]),
            "dreb": s["dreb"] / (s["dreb"] + s["o_oreb"]),
            "ftr": s["ftm"] / s["fga"],
            "opp_ftr": s["o_ftm"] / s["o_fga"],
            "three_rate": s["fg3a"] / s["fga"],
            "pace": 48 * 5 * s["poss"] / s["minutes"] if s["minutes"] else 0.0,
        }
    return out


def ranks(stats: dict[str, dict[str, float]]) -> dict[str, dict[str, int]]:
    """Each team's league rank (1 = best) in every metric."""
    out: dict[str, dict[str, int]] = defaultdict(dict)
    for key, _, _, higher, _ in TEAM_METRICS:
        ordered = sorted(stats, key=lambda t: -stats[t][key] if higher else stats[t][key])
        for i, t in enumerate(ordered, start=1):
            out[t][key] = i
    return out


@dataclass
class TeamPage:
    team: fan.Team
    rank: int
    start_rating: float | None
    games: list[dict] = field(default_factory=list)
    stats: dict[str, float] | None = None
    ranks: dict[str, int] | None = None
    strengths: list[dict] = field(default_factory=list)
    weaknesses: list[dict] = field(default_factory=list)
    roster: list[dict] = field(default_factory=list)
    upcoming: list[dict] = field(default_factory=list)
    projection: dict | None = None
    summary: str = ""

    @property
    def slug(self) -> str:
        return team_slug(self.team)


def team_slug(team: fan.Team) -> str:
    return f"team-{team.abbr.lower()}.html"


def _metric_item(key: str, value: float, rank: int) -> dict:
    meta = next(m for m in TEAM_METRICS if m[0] == key)
    return {"key": key, "label": meta[1], "side": meta[2], "value": value, "rank": rank,
            "format": meta[4]}  # fmt: skip


def team_pages(
    root: Path,
    season: str,
    team_map: dict[str, fan.Team],
    live_state,
    payloads: dict[str, dict],
) -> dict[str, TeamPage]:
    timeline = seasonelo.replay(Store(root), season)
    stats = team_stats(root, season)
    rk = ranks(stats) if len(stats) == len(team_map) else {}
    proj = (fan.load_projections(root) or {}).get("teams", {})
    schedule = Store.read(projections.schedule_path(root, season))
    by_rating = sorted(
        team_map.values(), key=lambda t: -(t.rating if t.rating is not None else -99)
    )
    mean = live_state.params.mean
    hca = live_state.home_court()
    pages = {}
    for tid, team in team_map.items():
        games = []
        for g in (timeline or {}).get("games", {}).get(tid, []):
            opp = team_map[g["opponent"]]
            per_point = projections.ELO_PER_POINT
            games.append({**g, "opp": opp, "change_pts": g["change"] / per_point,
                          "opp_rating": (g["opp_pre"] - mean) / per_point})  # fmt: skip
        start = ((timeline["start"][tid] - mean) / projections.ELO_PER_POINT) if timeline else None
        page = TeamPage(team, by_rating.index(team) + 1, start, games, stats.get(tid), rk.get(tid))
        if page.ranks:
            items = [_metric_item(k, page.stats[k], page.ranks[k])
                     for k, *_ in TEAM_METRICS if k != "pace"]  # fmt: skip
            page.strengths = sorted(items, key=lambda i: i["rank"])[:3]
            page.weaknesses = sorted(items, key=lambda i: -i["rank"])[:3]
        page.roster = sorted(
            (p for p in payloads.values() if p["team"] and p["team"]["id"] == tid),
            key=lambda p: (p["status"] == "Out", -((p["projection"] or {}).get("minutes") or 0)),
        )
        upcoming = [g for g in schedule if g["completed"] != "True"
                    and tid in (g["home_team_id"], g["away_team_id"])][:5]  # fmt: skip
        for g in upcoming:
            home = g["home_team_id"] == tid
            opp_id = g["away_team_id"] if home else g["home_team_id"]
            diff = live_state.ratings[tid] - live_state.ratings[opp_id]
            court = 0.0 if g["neutral"] == "True" else (hca if home else -hca)
            page.upcoming.append({
                "date": g["tipoff_utc"], "opp": team_map[opp_id], "home": home,
                "p_win": win_probability(diff + court),
            })  # fmt: skip
        page.projection = proj.get(tid)
        page.summary = _summary(page)
        pages[tid] = page
    return pages


def _summary(page: TeamPage) -> str:
    """One plain-English sentence on where the rating came from."""
    t = page.team
    if page.start_rating is None or t.rating is None:
        return ""
    moved = t.rating - page.start_rating
    wins = sum(1 for g in page.games if g["won"])
    direction = "up" if moved > 0.05 else "down" if moved < -0.05 else "about level"
    first = (f"{t.short} started the season at {page.start_rating:+.1f}: last season's rating, "
             f"pulled 30% back toward average over the summer.")  # fmt: skip
    if not page.games:
        return first
    second = (f" After {len(page.games)} games ({wins}-{len(page.games) - wins}) they're "
              f"{direction}" + (f" {abs(moved):.1f} points" if direction != "about level" else "")
              + f", at {t.rating:+.1f}.")  # fmt: skip
    return first + second


# --- prediction history -------------------------------------------------------------------
def history(store: Store, season: str, team_map: dict[str, fan.Team]) -> dict[str, Any]:
    results = {r["game_id"]: r for r in store.read(store.results_path(season))}
    hybrid = {r["game_id"]: float(r["p_home"])
              for r in store.read(shadow.log_path(store.root, season))}  # fmt: skip
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
            "hybrid": hybrid.get(p["game_id"]), "home_won": home_won,
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


# --- The Rotation (the game) --------------------------------------------------------------
ROSTER_SIZE = 9
MAX_MINUTES = 40


def rotation_puzzle(
    store: Store, season: str, day: str | None, team_map: dict[str, fan.Team], published: Path
) -> dict[str, Any] | None:
    """Tonight's puzzle: the biggest underdog on the slate, their top nine players by usual
    minutes, and the model's sensitivity of the win probability to player production."""
    if day is None:
        return None
    preds = [p for p in store.read(store.predictions_path(season)) if p["game_date"] == day]
    if not preds:
        return None
    try:
        state = shadow.load_state(store.root).players
        model = json.loads((published / "hybrid" / "model.json").read_text())
    except FileNotFoundError:
        return None
    directory = rosters.load_directory(store.root)
    p = min(preds, key=lambda r: min(float(r["p_home"]), 1 - float(r["p_home"])))
    dog_home = float(p["p_home"]) < 0.5
    dog_id = p["home_team_id"] if dog_home else p["away_team_id"]
    fav_id = p["away_team_id"] if dog_home else p["home_team_id"]
    # Game-time decisions: tonight's chance of playing from the injury watch (the agent's read,
    # else the keyword rule), or a coin flip if the player wasn't in it.
    calls = {}
    log_path, _ = availability.paths(store, season)
    for r in store.read(log_path):
        if r["game_date"] == day:
            calls[r["player_id"]] = float(r["agent_p"] or r["baseline_p"])
    players = []
    for pid, info in directory.items():
        if info["team_id"] != dog_id or info["status"] == "Out" or pid not in state.players:
            continue
        gtd = info["status"] == "Day-To-Day"
        players.append({"id": pid, "name": info["name"], "short": info["short"],
                        "position": info["position"], "headshot": fan.headshot_url(pid),
                        "prod": round(state.production(pid), 4),
                        "usual": round(min(state.role(pid), MAX_MINUTES), 1),
                        "p_play": round(calls.get(pid, 0.5), 2) if gtd else 1.0})  # fmt: skip
    players = sorted(players, key=lambda x: -x["usual"])[:ROSTER_SIZE]
    if len(players) < 5:
        return None
    i = model["features"].index("production_real")
    beta = model["coef"][i] / model["sd"][i]  # logit per point of team production
    p_dog = 1 - float(p["p_home"]) if not dog_home else float(p["p_home"])
    return {
        "date": day,
        "underdog": {
            "abbr": team_map[dog_id].abbr,
            "name": team_map[dog_id].name,
            "short": team_map[dog_id].short,
            "color": team_map[dog_id].color_d,
            "logo": team_map[dog_id].logo_dark,
        },  # fmt: skip
        "favorite": {
            "abbr": team_map[fav_id].abbr,
            "name": team_map[fav_id].name,
            "short": team_map[fav_id].short,
            "color": team_map[fav_id].color_d,
            "logo": team_map[fav_id].logo_dark,
        },  # fmt: skip
        "dog_home": dog_home,
        "p_dog": round(p_dog, 4),
        "logit_dog": round(math.log(p_dog / (1 - p_dog)), 5),
        "beta": round(beta, 6),
        "max_minutes": MAX_MINUTES,
        "players": players,
    }


def today_label(day: str | None) -> str:
    return datetime.fromisoformat(day).strftime("%b %-d") if day else ""
