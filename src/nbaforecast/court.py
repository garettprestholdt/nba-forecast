"""Shot zones and lineup stints from ESPN game summaries, for shot charts and impact ratings.

Both come from the game summary `nbaf box-sync` already downloads, so they cost no extra
requests. Two append-only files per season on the data branch:

- `shots/<season>.csv`: field goal attempts and makes per game, player and zone.
- `stints/<season>.csv`: one row per stretch with the same ten players on the floor and each
  end of it: who was on offense and defense, whether the offense was at home, points scored,
  and possessions
  (FGA + 0.44 FTA - offensive rebounds + turnovers). `impact.py` fits RAPM from these.

Zones: one zone for everything inside 14 feet, then midrange, corner threes and above-the-break
threes split left / center / right. The split between "at the rim" and "the rest of the paint"
isn't used: in 2025-26 data, a visiting team's rim share tracked the home team's (r = 0.75), so
it measures each arena's scorekeeper more than the teams.
"""

import logging
import math
from collections import defaultdict
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import requests

from nbaforecast import live
from nbaforecast.ingest import playbyplay as pbp
from nbaforecast.ingest.boxscores import parse_boxscore

log = logging.getLogger(__name__)

# ESPN's shot coordinates are in feet: x across the court (0-50, the basket at 25), y out from
# the basket (the baseline is about 5 feet behind it). They match the distance in each play's
# text to within a foot (r = 0.999 in 2025-26).
ZONES = [
    "inside", "mid_left", "mid_center", "mid_right", "corner_left", "corner_right",
    "arc_left", "arc_center", "arc_right",
]  # fmt: skip
ZONE_LABELS = {
    "inside": "Inside 14 ft", "mid_left": "Midrange left", "mid_center": "Midrange center",
    "mid_right": "Midrange right", "corner_left": "Left corner 3", "corner_right": "Right corner 3",
    "arc_left": "Left wing 3", "arc_center": "Top of the arc 3", "arc_right": "Right wing 3",
}  # fmt: skip
INSIDE_FT = 14.0
CORNER_Y = 8.95  # where the 22-foot corner line meets the 23.75-foot arc
CENTER_DEG = 30.0  # straightaway: within 30 degrees of the line through the basket

SHOT_FIELDS = ["game_id", "game_date", "team_id", "opponent_id", "player_id", "zone", "fga", "fgm"]
STINT_FIELDS = ["game_id", "game_date", "offense_id", "defense_id", "home", "offense", "defense",
                "pts", "poss"]  # fmt: skip


def previous_season(season: str) -> str:
    start = int(season[:4]) - 1
    return f"{start}-{str(start + 1)[2:]}"


def shots_path(root: Path, season: str) -> Path:
    return Path(root) / "shots" / f"{season}.csv"


def stints_path(root: Path, season: str) -> Path:
    return Path(root) / "stints" / f"{season}.csv"


def zone(x: float, y: float, three: bool) -> str | None:
    """The zone of a field goal attempt, or None for a missing or impossible location."""
    if not (0 <= x <= 50 and -6 <= y <= 94):
        return None  # ESPN uses huge sentinel values when it has no location
    dx = x - 25
    if three and y < CORNER_Y and abs(dx) >= 21:
        return "corner_left" if dx < 0 else "corner_right"
    if not three and math.hypot(dx, y) < INSIDE_FT:
        return "inside"
    angle = math.degrees(math.atan2(dx, y))  # 0 = straightaway; negative = left side
    side = "center" if abs(angle) <= CENTER_DEG else "left" if dx < 0 else "right"
    return f"{'arc' if three else 'mid'}_{side}"


def parse_shots(summary: dict[str, Any], game: dict[str, Any]) -> list[dict[str, Any]]:
    """Attempts and makes per player and zone. Free throws and unlocated shots are skipped."""
    home, away = str(game["home_team_id"]), str(game["away_team_id"])
    tally: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    for p in summary.get("plays") or []:
        if not p.get("shootingPlay") or (p.get("type") or {}).get("text", "").lower().startswith(
            "free throw"
        ):
            continue
        team = str((p.get("team") or {}).get("id", ""))
        who = [str((a.get("athlete") or {}).get("id", "")) for a in p.get("participants") or []]
        c = p.get("coordinate") or {}
        if team not in (home, away) or not who or c.get("x") is None or c.get("y") is None:
            continue
        three = p.get("pointsAttempted") == 3 or "three point" in p.get("text", "").lower()
        z = zone(float(c["x"]), float(c["y"]), three)
        if z is None:
            continue
        t = tally[(team, who[0], z)]
        t[0] += 1
        t[1] += 1 if p.get("scoringPlay") else 0
    return [
        {"game_id": str(game["game_id"]), "game_date": str(game["game_date"]), "team_id": team,
         "opponent_id": away if team == home else home, "player_id": pid, "zone": z,
         "fga": fga, "fgm": fgm}
        for (team, pid, z), (fga, fgm) in sorted(tally.items())
    ]  # fmt: skip


def parse_stints(summary: dict[str, Any], game: dict[str, Any]) -> list[dict[str, Any]]:
    """Points and possessions for each end of the floor, per set of ten players.

    Lineups come from the play-by-play helpers the box-score rebuild uses; plays while either
    team doesn't have exactly five on the floor (a dropped substitution) are left out."""
    roster = parse_boxscore(summary, game)
    if not roster:
        return []
    slim = pbp.slim(summary)["plays"]
    team_of = {r["player_id"]: r["team_id"] for r in roster}
    home, away = str(game["home_team_id"]), str(game["away_team_id"])
    on: dict[str, set[str]] = {home: set(), away: set()}
    for r in roster:
        if r["starter"]:
            on[r["team_id"]].add(r["player_id"])
    by_period: dict[int, list[dict]] = defaultdict(list)
    for p in slim:
        if p["period"]:
            by_period[p["period"]].append(p)
    acc: dict[tuple[str, frozenset, frozenset], list[float]] = defaultdict(lambda: [0.0, 0.0])
    for period in sorted(by_period):
        plays = by_period[period]
        if period > 1:
            on = pbp._period_starters(plays, team_of, on)
        for p in pbp._subs_last(plays):
            kind = pbp._kind(p)
            if kind == "sub":
                entering, leaving = pbp._sub_sides(p)
                t = team_of.get(entering or "") or team_of.get(leaving or "")
                if t in on:
                    on[t].discard(leaving or "")
                    if entering:
                        on[t].add(entering)
                continue
            if len(on[home]) != 5 or len(on[away]) != 5:
                continue
            t = p["team"] or team_of.get((p["players"] or [""])[0], "")
            if t not in (home, away):
                continue
            poss = {"fg": 1.0, "ft": 0.44, "tov": 1.0}.get(kind, 0.0)
            if kind == "reb" and "offensive" in p["type"].lower():
                poss = -1.0
            pts = pbp._points(p)
            if not (poss or pts):
                continue
            other = away if t == home else home
            a = acc[(t, frozenset(on[t]), frozenset(on[other]))]
            a[0] += pts
            a[1] += poss
    return [
        {"game_id": str(game["game_id"]), "game_date": str(game["game_date"]), "offense_id": t,
         "defense_id": away if t == home else home, "home": int(t == home),
         "offense": ";".join(sorted(o)),
         "defense": ";".join(sorted(d)), "pts": int(pts), "poss": round(poss, 2)}
        for (t, o, d), (pts, poss) in acc.items()
        if poss > 0
    ]  # fmt: skip


def write_game(root: Path, season: str, summary: dict[str, Any], game: dict[str, Any]) -> bool:
    """Append one game's shots and stints. False if the summary has no usable plays."""
    stints = parse_stints(summary, game)
    if not stints:
        return False
    live.Store.append(shots_path(root, season), SHOT_FIELDS, parse_shots(summary, game))
    live.Store.append(stints_path(root, season), STINT_FIELDS, stints)
    return True


def sync(
    store: live.Store, season: str, fetch_summary: Callable[[str], dict], through: date
) -> dict[str, int]:
    """Add shots and stints for every final game in the results log (up to `through`) that
    isn't in the season's stint log yet, so a missed morning catches up."""
    done = {r["game_id"] for r in store.read(stints_path(store.root, season))}
    todo = [r for r in store.read(store.results_path(season))
            if r["game_id"] not in done and r["game_date"] <= through.isoformat()]  # fmt: skip
    todo.sort(key=lambda r: (r["game_date"], r["tipoff_utc"], r["game_id"]))
    added = missing = 0
    for g in todo:
        try:
            summary = fetch_summary(g["game_id"])
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                raise
            summary = None
        if summary and write_game(store.root, season, summary, g):
            added += 1
        else:
            missing += 1
    return {"games_added": added, "missing": missing}


def totals(rows: list[dict[str, str]], by: str) -> dict[str, dict[str, list[int]]]:
    """key -> zone -> [fga, fgm], keyed by `team_id` (offense), `opponent_id` (shots allowed)
    or `player_id`."""
    out: dict[str, dict[str, list[int]]] = defaultdict(lambda: {z: [0, 0] for z in ZONES})
    for r in rows:
        t = out[r[by]][r["zone"]]
        t[0] += int(r["fga"])
        t[1] += int(r["fgm"])
    return dict(out)


def league(rows: list[dict[str, str]]) -> dict[str, list[int]]:
    out = {z: [0, 0] for z in ZONES}
    for r in rows:
        out[r["zone"]][0] += int(r["fga"])
        out[r["zone"]][1] += int(r["fgm"])
    return out
