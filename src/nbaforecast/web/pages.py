"""Data for the player pop-up, team pages, game pages, prediction history, and Cold Case (the game).

Everything here is computed at build time from the data branch (plus small published files on
main); the pages make no API calls. Player pop-ups load `players/<id>.json` on click.
"""

import json
import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from nbaforecast import boxlog, court, impact, metrics, projections, seasonelo, shadow
from nbaforecast import coldcase as cc
from nbaforecast.ingest import rosters, venues
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


# --- shot zones and impact ratings (shared by player pop-ups and game pages) -------------
MIN_CHART_SHOTS = 60  # a player's shot chart needs this many attempts in a season
REGULAR_POSS = 3000  # impact percentiles compare players with at least this many possessions


@dataclass
class Court:
    """Shot-zone totals for this season and last, and the impact ratings."""

    seasons: list[str]  # [last, this]
    players: dict[str, dict[str, dict]]  # season -> player -> zone -> [fga, fgm]
    offense: dict[str, dict[str, dict]]  # season -> team -> zone -> [fga, fgm]
    defense: dict[str, dict[str, dict]]  # season -> team -> zone -> [fga, fgm] allowed
    league: dict[str, dict[str, list[int]]]  # season -> zone -> [fga, fgm]
    impact: dict[str, dict[str, float]]  # player -> ratings (impact.fit)
    d_sorted: list[float] = field(default_factory=list)  # regulars' blended defense
    o_sorted: list[float] = field(default_factory=list)

    def percentile(self, value: float, side: str) -> int | None:
        pool = self.d_sorted if side == "d" else self.o_sorted
        if not pool:
            return None
        below = sum(1 for v in pool if v < value)
        return round(100 * below / len(pool))


def load_court(root: Path, season: str) -> Court:
    seasons = [court.previous_season(season), season]
    players, offense, defense, league = {}, {}, {}, {}
    for s in seasons:
        rows = Store.read(court.shots_path(root, s))
        players[s] = court.totals(rows, "player_id")
        offense[s] = court.totals(rows, "team_id")
        defense[s] = court.totals(rows, "opponent_id")
        league[s] = court.league(rows)
    ratings = (impact.load(root) or {}).get("players", {})
    regulars = [impact.blend(r) for r in ratings.values() if r["poss"] >= REGULAR_POSS]
    return Court(seasons, players, offense, defense, league, ratings,
                 sorted(d for _, d in regulars), sorted(o for o, _ in regulars))  # fmt: skip


def zone_payload(zones: dict[str, list[int]] | None) -> dict[str, list[int]] | None:
    if not zones or not sum(v[0] for v in zones.values()):
        return None
    return {z: zones[z] for z in court.ZONES}


def defense_tags(height_in: int | None, blk36: float, stl36: float, d: float | None) -> list[str]:
    """What kind of defender a player is, from size and box-score rates, but only when their
    impact rating agrees they're good at it: steals alone don't make a stopper (in 2024-26 data
    a 6'8" star with plenty of steals rated league-average on defense)."""
    if not height_in:
        return []
    good = d is None or d >= 0.5
    tags = []
    if height_in >= 82 and blk36 >= 1.5 and good:
        tags.append("Rim protector")
    if height_in <= 77 and stl36 >= 1.4 and good:
        tags.append("Point-of-attack defender")
    if 77 <= height_in <= 81 and stl36 + blk36 >= 2.0 and good:
        tags.append("Wing stopper")
    if 78 <= height_in <= 82 and stl36 >= 1.0 and blk36 >= 0.8 and (d is None or d >= 0):
        tags.append("Switchable")
    return tags


def _age(dob: str, on: str) -> int | None:
    try:
        born, day = datetime.fromisoformat(dob).date(), datetime.fromisoformat(on).date()
    except ValueError:
        return None
    return day.year - born.year - ((day.month, day.day) < (born.month, born.day))


def _height(inches: str) -> str:
    return f"{int(inches) // 12}' {int(inches) % 12}\"" if inches else ""


def _profile(pid: str, info: dict, lines: list[dict], cd: Court, today: str) -> dict[str, Any]:
    """Bio, impact ratings, defensive role tags and shot chart for one player's pop-up."""
    g = sum(s["g"] for s in lines)
    mins = sum(s["min"] * s["g"] for s in lines)
    rate = (lambda k: 36 * sum(s[k] * s["g"] for s in lines) / mins) if mins else (lambda k: 0.0)
    r = cd.impact.get(pid)
    o, d = impact.blend(r) if r else (None, None)
    small = r is not None and r["poss"] < REGULAR_POSS
    height = int(info["height_in"]) if info.get("height_in") else None
    charts = {}
    for s in cd.seasons:
        zones = cd.players.get(s, {}).get(pid)
        if zones and sum(v[0] for v in zones.values()) >= MIN_CHART_SHOTS:
            charts[s] = zone_payload(zones)
    return {
        "bio": {
            "age": _age(info["dob"], today) if info.get("dob") else None,
            "height": _height(info.get("height_in", "")),
            "weight": int(info["weight_lb"]) if info.get("weight_lb") else None,
        },
        "impact": (
            {
                "o": o,
                "d": d,
                "total": round(o + d, 1),
                "poss": r["poss"],
                "small_sample": small,
                "o_pct": cd.percentile(o, "o"),
                "d_pct": cd.percentile(d, "d"),
                "fits": {"plain": [r["o"], r["d"]], "box_prior": [r["o_prior"], r["d_prior"]]},
            }
            if r
            else None
        ),  # fmt: skip
        "tags": defense_tags(height, rate("blk"), rate("stl"), d) if g else [],
        "shots": charts or None,
        "league": {s: zone_payload(cd.league[s]) for s in charts},
    }


# --- players ------------------------------------------------------------------------------
GAME_SCORE_PARTS = [  # (label, weight, stat) — Hollinger's Game Score, term by term
    ("Points", 1.0, "pts"), ("Field goals made", 0.4, "fgm"), ("Field goal attempts", -0.7, "fga"),
    ("Missed free throws", -0.4, "ftmiss"), ("Offensive rebounds", 0.7, "oreb"),
    ("Defensive rebounds", 0.3, "dreb"), ("Steals", 1.0, "stl"), ("Assists", 0.7, "ast"),
    ("Blocks", 0.7, "blk"), ("Personal fouls", -0.4, "pf"), ("Turnovers", -1.0, "tov"),
]  # fmt: skip


def player_payloads(
    root: Path,
    season: str,
    team_map: dict[str, fan.Team],
    cd: Court | None = None,
    today: str = "",
) -> dict[str, dict[str, Any]]:
    """One JSON payload per player on a current roster: bio, tonight's projection, impact
    ratings, defensive role, shot chart, this season's game log and stat line, career lines,
    and their average Game Score broken into its parts."""
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
        if cd is not None:
            recent = [s for s in history if s["season"] in cd.seasons and s.get("g")]
            out[pid].update(_profile(pid, info, recent, cd, today or season[:4] + "-12-01"))
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
    schedule: list[dict] = field(default_factory=list)  # all 82 games, played and to come
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
    slate_ids: set[str] | None = None,
) -> dict[str, TeamPage]:
    timeline = seasonelo.replay(Store(root), season)
    stats = team_stats(root, season)
    rk = ranks(stats) if len(stats) == len(team_map) else {}
    proj = (fan.load_projections(root) or {}).get("teams", {})
    schedule = Store.read(projections.schedule_path(root, season))
    results = {r["game_id"]: r for r in Store.read(Store(root).results_path(season))}
    forecasts = {r["game_id"]: float(r["p_home"])
                 for r in Store.read(Store(root).predictions_path(season))}  # fmt: skip
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
        replayed = {g["game_id"]: g["p_win"] for g in page.games}
        page.schedule = _schedule(tid, schedule, results, forecasts, team_map, live_state, hca,
                                  slate_ids or set(), replayed)  # fmt: skip
        page.projection = proj.get(tid)
        page.summary = _summary(page)
        pages[tid] = page
    return pages


def _schedule(
    tid, schedule, results, forecasts, team_map, live_state, hca, slate_ids, replayed
) -> list:
    """Every game on the team's schedule. Played: result and the forecast we published before
    tip-off (or, for games before the log began, Elo's pre-game number from the season replay,
    flagged `replayed`). To come: the chance to win from today's ratings, home court included."""
    out = []
    for g in schedule:
        if tid not in (g["home_team_id"], g["away_team_id"]):
            continue
        home = g["home_team_id"] == tid
        opp_id = g["away_team_id"] if home else g["home_team_id"]
        if opp_id not in team_map:
            continue
        row = {"game_id": g["game_id"], "date": g["tipoff_utc"], "opp": team_map[opp_id],
               "home": home, "neutral": g["neutral"] == "True",
               "link": game_slug(g["game_id"]) if g["game_id"] in slate_ids else None}  # fmt: skip
        r = results.get(g["game_id"])
        if r and r["home_score"] not in ("", None):
            us, them = (int(r["home_score"]), int(r["away_score"]))
            us, them = (us, them) if home else (them, us)
            p = forecasts.get(g["game_id"])
            p_win = None if p is None else (p if home else 1 - p)
            row.update(final=True, won=us > them, score=f"{us}–{them}", p_win=p_win,
                       replayed=p_win is None and g["game_id"] in replayed)  # fmt: skip
            if row["replayed"]:
                row["p_win"] = replayed[g["game_id"]]
        else:
            diff = live_state.ratings[tid] - live_state.ratings[opp_id]
            court = 0.0 if row["neutral"] else (hca if home else -hca)
            row.update(final=False, p_win=win_probability(diff + court))
        out.append(row)
    return sorted(out, key=lambda r: (r["date"], r["game_id"]))


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


# --- Cold Case (the game) ----------------------------------------------------------------
def coldcase(published: Path, out_dir: Path | None, today: date) -> dict[str, Any] | None:
    """Publish the cases that are open (through tomorrow's, so the page can switch at midnight
    Central before the next build) and return what the Play page needs. Later cases are never
    copied to the site."""
    index_path = cc.bank_dir(published) / "index.json"
    if not index_path.exists():
        return None
    index = json.loads(index_path.read_text())
    last = min(cc.case_number(today) + 1, len(index["cases"]))
    if last < 1:
        return None
    if out_dir is not None:
        folder = out_dir / "coldcase"
        folder.mkdir(parents=True, exist_ok=True)
        for n in range(1, last + 1):
            shutil.copyfile(cc.case_path(published, n), folder / cc.case_path(published, n).name)
    return {"start": index["start"], "today": max(1, min(cc.case_number(today), last)),
            "cases": index["cases"][:last]}  # fmt: skip


def today_label(day: str | None) -> str:
    return datetime.fromisoformat(day).strftime("%b %-d") if day else ""


# --- game pages ---------------------------------------------------------------------------
TAPE = ["ortg", "efg", "tov", "oreb", "ftr", "three_rate",  # offense
        "drtg", "opp_efg", "opp_tov", "dreb", "opp_ftr",  # defense
        "pace"]  # fmt: skip


def game_slug(game_id: str) -> str:
    return f"game-{game_id}.html"


def _venue(game_id: str, home_id: str, known: dict[str, dict]) -> dict | None:
    """Where a game is played: ESPN's listing for it, else the home team's latest home arena."""
    if game_id in known:
        return known[game_id]
    usual = [v for v in known.values() if v.get("home_team_id") == home_id and not v["neutral"]]
    return {**usual[-1], "neutral": False, "assumed": True} if usual else None


def game_pages(
    root: Path, season: str, games: list, team_map: dict[str, fan.Team], cd: Court
) -> list[dict[str, Any]]:
    """One preview page per game on the slate: where it's played, the two teams' season stats
    side by side, and each team's shot zones (their shots, and the shots they allow) this
    season and last."""
    stats = team_stats(root, season)
    rk = ranks(stats) if len(stats) == len(team_map) else {}
    known = venues.load(root)
    meta = {m[0]: m for m in TEAM_METRICS}
    out = []
    for m in games:
        away, home = m.away.team_id, m.home.team_id
        tape = []
        for key in TAPE:
            if away not in stats or home not in stats:
                break
            _, label, side, higher, fmt = meta[key]
            a, h = stats[away][key], stats[home][key]
            better = None if key == "pace" or a == h else (
                "away" if (a > h) == higher else "home")  # fmt: skip
            tape.append({"key": key, "label": label, "side": side, "format": fmt, "away": a,
                         "home": h, "away_rank": rk.get(away, {}).get(key),
                         "home_rank": rk.get(home, {}).get(key), "better": better})  # fmt: skip
        chart = {"seasons": [s for s in cd.seasons if cd.league[s] and sum(
                    v[0] for v in cd.league[s].values())], "league": {}, "teams": {}}  # fmt: skip
        for s in chart["seasons"]:
            chart["league"][s] = zone_payload(cd.league[s])
        for tid in (away, home):
            chart["teams"][tid] = {
                s: {"off": zone_payload(cd.offense[s].get(tid)),
                    "def": zone_payload(cd.defense[s].get(tid))}
                for s in chart["seasons"]
            }  # fmt: skip
        out.append({
            "m": m, "slug": game_slug(m.game_id), "venue": _venue(m.game_id, home, known),
            "games_played": {tid: int(stats[tid]["games"]) if tid in stats else 0
                             for tid in (away, home)},
            "tape": tape, "chart": chart,
        })  # fmt: skip
    return out
