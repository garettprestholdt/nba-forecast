"""Each team's rating, game by game, this season: why a team is rated where it is.

The live Elo state only keeps current ratings. So the ratings at the start of the season (after
the summer pull toward average) are published once on main
(`data/published/elo/season_start_<season>.json`, from `nbaf season-start`, local), and the site
replays this season's results from them with the same walk the live model uses. The replay must
land exactly on the live ratings; `check` says whether it did.
"""

import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from nbaforecast import config
from nbaforecast.models import elo
from nbaforecast.models.elo import EloState
from nbaforecast.pipeline import live


def start_path(season: str, published: Path | None = None) -> Path:
    return (published or config.PUBLISHED_DIR) / "elo" / f"season_start_{season}.json"


def season_start_state(games: pd.DataFrame, season: str) -> EloState:
    """Ratings after every earlier season, pulled toward average for the new one."""
    state = EloState(live.LIVE_PARAMS)
    elo.walk(games[games["season"] < season], state)
    state.start_day(season)
    return state


def write_start(state: EloState, season: str, published: Path | None = None) -> Path:
    path = start_path(season, published)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), indent=1) + "\n")
    return path


def _results_frame(rows: list[dict[str, str]]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["game_date"] = df["game_date"].map(date.fromisoformat)
    df["tipoff_utc"] = df["tipoff_utc"].map(datetime.fromisoformat)
    df["home_score"] = df["home_score"].astype(int)
    df["away_score"] = df["away_score"].astype(int)
    df["is_neutral_site"] = df["is_neutral_site"].map(lambda v: str(v) == "True")
    return df


def replay(store: live.Store, season: str, published: Path | None = None) -> dict[str, Any] | None:
    """Every team's season so far: start rating, each game with its rating change, end rating.
    None if there's no published season-start file."""
    path = start_path(season, published)
    if not path.exists():
        return None
    state = EloState.from_dict(json.loads(path.read_text()))
    results = _results_frame(store.read(store.results_path(season)))
    start = dict(state.ratings)
    preds = elo.walk(results, state) if not results.empty else pd.DataFrame()
    games: dict[str, list[dict]] = defaultdict(list)
    for p in preds.itertuples(index=False):
        r = results.loc[results["game_id"] == p.game_id].iloc[0]
        for team, opp, pre, opp_pre, home in (
            (p.home_team_id, p.away_team_id, p.home_elo_pre, p.away_elo_pre, True),
            (p.away_team_id, p.home_team_id, p.away_elo_pre, p.home_elo_pre, False),
        ):
            pts, opp_pts = (r.home_score, r.away_score) if home else (r.away_score, r.home_score)
            games[team].append({
                "game_id": p.game_id, "date": p.game_date.isoformat(), "opponent": opp,
                "home": home, "pts": int(pts), "opp_pts": int(opp_pts),
                "won": bool(pts > opp_pts), "pre": float(pre), "opp_pre": float(opp_pre),
                "p_win": float(p.p_home if home else 1 - p.p_home),
            })  # fmt: skip
    for team, rows in games.items():
        for i, g in enumerate(rows):
            post = rows[i + 1]["pre"] if i + 1 < len(rows) else state.ratings[team]
            g["post"] = float(post)
            g["change"] = float(post - g["pre"])
    return {"start": start, "end": dict(state.ratings), "games": dict(games),
            "mean": state.params.mean}  # fmt: skip


def check(timeline: dict[str, Any], live_ratings: dict[str, float], tol: float = 0.01) -> bool:
    """True if the replay ends on the live model's ratings (same games, same updates)."""
    return all(abs(timeline["end"].get(t, r) - r) <= tol for t, r in live_ratings.items())
