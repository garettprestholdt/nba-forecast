"""Team pages: season stats and ranks, the rating replay, roster, next games, full schedule."""

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from nbaforecast.models.elo import win_probability
from nbaforecast.pipeline import boxlog, projections, seasonelo
from nbaforecast.pipeline.live import Store
from nbaforecast.web import fan
from nbaforecast.web.links import game_slug, team_slug
from nbaforecast.web.players import _f

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
