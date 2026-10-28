"""Player box scores for the current season on the data branch, and per-season stat lines.

`boxscores/<season>.csv` (append-only) holds one row per player per game, in the same shape as
the warehouse's fct_player_games. `nbaf box-sync` adds every final game that isn't in it yet, so a
missed morning catches up the next day. The site builds each player's game log and season line
from it; `players/careers.json` (written once, locally, from the warehouse) holds earlier seasons.

`season_line` is the one place stat lines are computed, for the warehouse history and the live
season alike, so both use the same definitions.
"""

import json
import logging
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import date
from pathlib import Path
from typing import Any

import requests

from nbaforecast.ingest.boxscores import parse_boxscore
from nbaforecast.pipeline import live, shadow

log = logging.getLogger(__name__)

FIELDS = [
    "game_id", "game_date", "team_id", "opponent_id", "is_home", "player_id", "player",
    "position", "starter", "did_not_play", "dnp_reason", "stats_missing", "minutes", "pts",
    "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "reb", "ast", "stl", "blk", "tov",
    "pf", "plus_minus", "game_score",
]  # fmt: skip
COUNTING = ["minutes", "pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "reb",
            "ast", "stl", "blk", "tov", "pf"]  # fmt: skip


def path(root: Path, season: str) -> Path:
    return Path(root) / "boxscores" / f"{season}.csv"


def careers_path(root: Path) -> Path:
    return Path(root) / "players" / "careers.json"


def sync(
    store: live.Store, season: str, fetch_summary: Callable[[str], dict], through: date
) -> dict[str, int]:
    """Append box scores for every final game in the results log (up to `through`) that isn't
    in the season's box-score log yet."""
    existing = {r["game_id"] for r in store.read(path(store.root, season))}
    todo = [r for r in store.read(store.results_path(season))
            if r["game_id"] not in existing and r["game_date"] <= through.isoformat()]  # fmt: skip
    todo.sort(key=lambda r: (r["game_date"], r["tipoff_utc"], r["game_id"]))
    aliases = shadow.load_aliases()
    added, missing = 0, 0
    for g in todo:
        try:
            summary = fetch_summary(g["game_id"])
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                raise
            summary = None
        rows = parse_boxscore(summary, {**g, "game_date": g["game_date"]}) if summary else []
        box = shadow.clean_box(rows, aliases) if rows else {}
        if len(box) != 2:
            missing += 1
            continue
        out = []
        for team_rows in box.values():
            for r in team_rows:
                out.append({k: _csv_value(getattr(r, k, "")) for k in FIELDS})
        live.Store.append(path(store.root, season), FIELDS, out)
        added += 1
    return {"games_added": added, "missing": missing}


def _csv_value(v: Any) -> Any:
    return "" if v is None else v


# --- stat lines ---------------------------------------------------------------------------
def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _truthy(v: Any) -> bool:
    return v is True or str(v) == "True"


def season_line(rows: Iterable[dict[str, Any]], team_totals: dict[str, dict[str, float]]) -> dict:
    """A season stat line from one player's game rows (dicts with FIELDS keys).

    `team_totals` maps game_id -> the player's team totals that game (minutes, fga, fta, tov), for
    usage rate. Only games the player actually played (with stats) count."""
    games = [r for r in rows if not _truthy(r["stats_missing"]) and _num(r["minutes"]) > 0]
    n = len(games)
    if not n:
        return {"g": 0}
    s = {k: sum(_num(r[k]) for r in games) for k in COUNTING}
    pm = [_num(r["plus_minus"]) for r in games if str(r["plus_minus"]) not in ("", "None", "nan")]
    gs = sum(_num(r["game_score"]) for r in games)
    tm = defaultdict(float)
    for r in games:
        for k, v in team_totals.get(r["game_id"], {}).items():
            tm[k] += v
    shots = s["fga"] + 0.44 * s["fta"]
    usage_den = s["minutes"] * (tm["fga"] + 0.44 * tm["fta"] + tm["tov"])

    def pct(a: float, b: float) -> float | None:
        return round(a / b, 3) if b else None

    return {
        "g": n,
        "gs": sum(1 for r in games if _truthy(r["starter"])),
        "min": round(s["minutes"] / n, 1),
        "pts": round(s["pts"] / n, 1),
        "reb": round(s["reb"] / n, 1),
        "ast": round(s["ast"] / n, 1),
        "stl": round(s["stl"] / n, 1),
        "blk": round(s["blk"] / n, 1),
        "tov": round(s["tov"] / n, 1),
        "fg_pct": pct(s["fgm"], s["fga"]),
        "fg3_pct": pct(s["fg3m"], s["fg3a"]),
        "ft_pct": pct(s["ftm"], s["fta"]),
        "ts_pct": pct(s["pts"], 2 * shots),  # true shooting: points per shooting possession
        "efg_pct": pct(s["fgm"] + 0.5 * s["fg3m"], s["fga"]),
        "usg_pct": (
            round(100 * (shots + s["tov"]) * (tm["minutes"] / 5) / usage_den, 1)
            if usage_den
            else None
        ),  # share of team possessions used while on the floor
        "ast_tov": round(s["ast"] / s["tov"], 2) if s["tov"] else None,
        "pts36": round(36 * s["pts"] / s["minutes"], 1) if s["minutes"] else None,
        "pm": round(sum(pm) / len(pm), 1) if pm else None,
        "gmsc": round(gs / n, 1),
    }


def team_totals(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, str], dict[str, float]]:
    """(game_id, team_id) -> team totals used by usage rate."""
    out: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if _truthy(r["stats_missing"]):
            continue
        t = out[(r["game_id"], r["team_id"])]
        for k in ("minutes", "fga", "fta", "tov"):
            t[k] += _num(r[k])
    return out


def load_careers(root: Path) -> dict[str, list[dict]]:
    p = careers_path(root)
    return json.loads(p.read_text())["players"] if p.exists() else {}


def build_careers(player_rows, keep: set[str]) -> dict[str, list[dict]]:
    """player_id -> one stat line per season (regular season), from warehouse rows
    (warehouse.load_player_games-style DataFrame with box columns)."""
    rows = player_rows.to_dict("records")
    totals = team_totals(rows)
    by_player: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    team_of: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        if r["player_id"] in keep:
            by_player[r["player_id"]][r["season"]].append(r)
            team_of[(r["player_id"], r["season"])][r["team_abbr"]] += 1
    out = {}
    for pid, seasons in by_player.items():
        lines = []
        for season in sorted(seasons):
            games = seasons[season]
            tt = {r["game_id"]: totals[(r["game_id"], r["team_id"])] for r in games}
            line = season_line(games, tt)
            if line["g"]:
                teams = team_of[(pid, season)]
                lines.append({"season": season,
                              "team": "/".join(sorted(teams, key=lambda t: -teams[t])),
                              **line})  # fmt: skip
        out[pid] = lines
    return out
