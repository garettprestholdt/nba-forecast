"""Player pop-up data (players/<id>.json): bio, impact ratings, defensive role, shot chart,
game log, career lines and the Game Score breakdown. Also the shot-zone and impact data the
game pages share (`Court`)."""

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from nbaforecast.ingest import rosters
from nbaforecast.models import impact
from nbaforecast.pipeline import boxlog, court, shadow
from nbaforecast.pipeline.live import Store
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
