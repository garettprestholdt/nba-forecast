"""Season projections: every team's projected record and playoff and title odds, from
simulating the rest of the season 10,000 times with the published Elo model.

Each simulation plays every remaining regular-season game with Elo's win probability (home
court included), updating ratings after every simulated game ("hot" simulation, so a team on a
simulated run carries it forward and the spread of outcomes grows realistically). Then it seeds
each conference (1-6 straight in; 7-10 play-in), plays the play-in, and plays four best-of-seven
rounds with 2-2-1-1-1 home court for the better record.

Inputs, refreshed every morning by `nbaf project`:
- the official standings from ESPN (they leave out the NBA Cup final, which doesn't count);
- every team's remaining schedule from ESPN (saved to `schedule/<season>.csv`); games not on the
  schedule yet (December Cup fill-ins) are played against an average opponent so every team
  finishes at 82.

Simplifications, stated on the site: ties in the standings are broken at random rather than by
the NBA's tiebreakers, and simulated games update ratings by win/loss only (no margin).
"""

import csv
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import requests

from nbaforecast import config, live
from nbaforecast.ingest.espn import USER_AGENT
from nbaforecast.models.elo import EloState

log = logging.getLogger(__name__)

SIMS = 10_000
GAMES = 82
ELO_PER_POINT = 25.2  # one point of margin is worth 25.2 Elo points (fit on 2010-26 games)
TEAM_SCHEDULE_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{}/schedule"
STANDINGS_URL = "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings"
SCHEDULE_FIELDS = ["game_id", "tipoff_utc", "home_team_id", "away_team_id", "neutral", "completed"]
HOME_PATTERN = [True, True, False, False, True, False, True]  # from the higher seed's side


def franchises() -> dict[str, dict[str, str]]:
    with open(config.REPO_ROOT / "dbt" / "seeds" / "franchises.csv") as f:
        return {r["franchise_id"]: r for r in csv.DictReader(f)}


def rating_points(elo: float, mean: float) -> float:
    """Points per game better than an average team on a neutral floor."""
    return (elo - mean) / ELO_PER_POINT


# --- inputs -------------------------------------------------------------------------------
def _get(url: str, session: requests.Session, **params) -> dict:
    resp = session.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_standings(session: requests.Session) -> dict[str, dict[str, int]]:
    data = _get(STANDINGS_URL, session)
    out = {}
    for conference in data["children"]:
        for entry in conference["standings"]["entries"]:
            stats = {s["name"]: s.get("value") for s in entry["stats"]}
            out[str(entry["team"]["id"])] = {"wins": int(stats.get("wins") or 0),
                                             "losses": int(stats.get("losses") or 0)}  # fmt: skip
    return out


def fetch_schedule(season: str, teams: list[str], session: requests.Session) -> list[dict]:
    """Every regular-season game on any team's schedule, once each."""
    espn_season = int(season[:4]) + 1
    games = {}
    for team in teams:
        data = _get(TEAM_SCHEDULE_URL.format(team), session, season=espn_season, seasontype=2)
        for event in data.get("events", []):
            comp = event["competitions"][0]
            sides = {c["homeAway"]: str(c["team"]["id"]) for c in comp["competitors"]}
            if set(sides) != {"home", "away"}:
                continue
            games[event["id"]] = {
                "game_id": event["id"],
                "tipoff_utc": event["date"],
                "home_team_id": sides["home"],
                "away_team_id": sides["away"],
                "neutral": bool(comp.get("neutralSite")),
                "completed": bool(comp["status"]["type"].get("completed")),
            }
    return sorted(games.values(), key=lambda g: (g["tipoff_utc"], g["game_id"]))


def schedule_path(root: Path, season: str) -> Path:
    return Path(root) / "schedule" / f"{season}.csv"


def projections_path(root: Path, season: str) -> Path:
    return Path(root) / "projections" / f"{season}.json"


# --- simulation ---------------------------------------------------------------------------
def _win_prob(diff: np.ndarray) -> np.ndarray:
    return 1 / (1 + 10 ** (-diff / 400))


def simulate(
    ratings: dict[str, float],
    home_court: float,
    k: float,
    standings: dict[str, dict[str, int]],
    remaining: list[dict],
    conferences: dict[str, str],
    mean: float,
    sims: int = SIMS,
    seed: int = 0,
) -> dict[str, dict[str, float]]:
    """Per team: projected wins (mean, 10th and 90th percentile) and the share of simulations
    in which it makes the play-in, the playoffs, each round, and wins the title."""
    rng = np.random.default_rng(seed)
    teams = sorted(conferences)
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    rows = np.arange(sims)
    r = np.tile(np.array([ratings.get(t, mean) for t in teams], dtype=float), (sims, 1))
    wins = np.tile(np.array([standings.get(t, {}).get("wins", 0) for t in teams], float), (sims, 1))
    played = np.array([sum(standings.get(t, {}).values()) for t in teams])

    def play(h, a, hca, live=None) -> np.ndarray:
        """One game in every simulation (h, a: team index, or an array of them per
        simulation). Returns home-win booleans; updates ratings where `live`."""
        h, a = np.broadcast_to(h, (sims,)), np.broadcast_to(a, (sims,))
        p = _win_prob(r[rows, h] - r[rows, a] + hca)
        won = rng.random(sims) < p
        delta = k * (won - p)
        if live is not None:
            delta = np.where(live, delta, 0.0)
        r[rows, h] += delta
        r[rows, a] -= delta
        return won

    def series(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        """Best of seven, home court to the better regular-season record; returns winners."""
        a_better = wins[rows, a] >= wins[rows, b]
        hi, lo = np.where(a_better, a, b), np.where(a_better, b, a)
        hi_w, lo_w = np.zeros(sims, int), np.zeros(sims, int)
        for hi_home in HOME_PATTERN:
            live = (hi_w < 4) & (lo_w < 4)
            won = play(hi, lo, home_court, live) if hi_home else ~play(lo, hi, home_court, live)
            hi_w += live & won
            lo_w += live & ~won
        return np.where(hi_w >= 4, hi, lo)

    # 1. The remaining schedule, in date order.
    scheduled = np.zeros(n, dtype=int)
    for g in remaining:
        h, a = idx.get(g["home_team_id"]), idx.get(g["away_team_id"])
        if h is None or a is None:
            continue
        scheduled[[h, a]] += 1
        won = play(h, a, 0.0 if g["neutral"] else home_court)
        wins[:, h] += won
        wins[:, a] += ~won
    # 2. Games not scheduled yet (December Cup fill-ins): against an average opponent.
    for t in range(n):
        for _ in range(max(0, GAMES - played[t] - scheduled[t])):
            p = _win_prob(r[:, t] - mean)
            won = rng.random(sims) < p
            r[:, t] += k * (won - p)
            wins[:, t] += won

    reached = {
        key: np.zeros((sims, n), bool)
        for key in ("top_seed", "playin", "playoffs", "second_round", "conf_finals", "finals")
    }
    champs = {}
    key = wins + rng.random(wins.shape) * 1e-3  # equal records: random tiebreak
    for conference in ("East", "West"):
        members = np.array([idx[t] for t in teams if conferences[t] == conference])
        ranked = members[np.argsort(-key[:, members], axis=1)]
        seed_of = {s: ranked[:, s - 1] for s in range(1, 11)}
        reached["top_seed"][rows, seed_of[1]] = True
        for s in (7, 8, 9, 10):
            reached["playin"][rows, seed_of[s]] = True
        # Play-in: 7 v 8, winner is 7th; loser hosts the 9 v 10 winner for 8th.
        w78 = play(seed_of[7], seed_of[8], home_court)
        seventh = np.where(w78, seed_of[7], seed_of[8])
        loser78 = np.where(w78, seed_of[8], seed_of[7])
        winner910 = np.where(play(seed_of[9], seed_of[10], home_court), seed_of[9], seed_of[10])
        eighth = np.where(play(loser78, winner910, home_court), loser78, winner910)
        alive = [seed_of[1], eighth, seed_of[4], seed_of[5], seed_of[3], seed_of[6],
                 seed_of[2], seventh]  # fmt: skip
        for team_col in alive:
            reached["playoffs"][rows, team_col] = True
        for stage in ("second_round", "conf_finals", "finals"):
            alive = [series(alive[i], alive[i + 1]) for i in range(0, len(alive), 2)]
            for team_col in alive:
                reached[stage][rows, team_col] = True
        champs[conference] = alive[0]
    title = series(champs["East"], champs["West"])

    out = {}
    for t in teams:
        i = idx[t]
        w = wins[:, i]
        out[t] = {
            "rating": round(rating_points(ratings.get(t, mean), mean), 2),
            "proj_wins": round(float(w.mean()), 1),
            "wins_p10": float(np.percentile(w, 10)),
            "wins_p90": float(np.percentile(w, 90)),
            **{stage: round(float(m[:, i].mean()), 4) for stage, m in reached.items()},
            "title": round(float(np.mean(title == i)), 4),
        }
    return out


def project(
    root: Path,
    standings: dict[str, dict[str, int]],
    schedule: list[dict],
    now: datetime,
    sims: int = SIMS,
) -> dict:
    """Write schedule/<season>.csv and projections/<season>.json from the published Elo state."""
    store = live.Store(root)
    state: EloState = store.load_state()
    season = state.season or live.season_for(now.date())
    teams = franchises()
    remaining = [g for g in schedule if not g["completed"]]
    path = schedule_path(root, season)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SCHEDULE_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(schedule)
    results = simulate(
        state.ratings, state.home_court(), state.params.k, standings, remaining,
        {t: row["conference"] for t, row in teams.items()}, state.params.mean, sims=sims,
        seed=int(now.strftime("%Y%m%d")),
    )  # fmt: skip
    payload = {
        "season": season,
        "updated_at": now.isoformat(timespec="seconds"),
        "ratings_through": state.last_date.isoformat() if state.last_date else None,
        "simulations": sims,
        "elo_per_point": ELO_PER_POINT,
        "teams": {
            t: {**standings.get(t, {"wins": 0, "losses": 0}), **v} for t, v in results.items()
        },  # fmt: skip
    }
    out = projections_path(root, season)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    return payload


def run(root: Path, now: datetime | None = None, sims: int = SIMS) -> dict:
    now = now or datetime.now(UTC)
    session = requests.Session()
    teams = sorted(franchises())
    season = live.Store(root).load_state().season or live.season_for(now.date())
    return project(
        root, fetch_standings(session), fetch_schedule(season, teams, session), now, sims
    )
