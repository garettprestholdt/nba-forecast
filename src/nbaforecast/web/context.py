"""Everything the shared page templates need, read from the data branch and data/published/."""

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nbaforecast import metrics
from nbaforecast.agents import availability, recap
from nbaforecast.ingest.espn import EASTERN  # noqa: F401  (slate dates stay US Eastern)
from nbaforecast.pipeline import projections, shadow_v2
from nbaforecast.pipeline.live import Store
from nbaforecast.web import fan
from nbaforecast.web.filters import CENTRAL, WIN_AXIS

REPO_URL = "https://github.com/garettprestholdt/nba-forecast"


def _latest_season(store: Store) -> str:
    files = sorted((store.root / "predictions").glob("*.csv")) or sorted(
        (store.root / "results").glob("*.csv")
    )
    if not files:
        raise FileNotFoundError(f"no predictions or results under {store.root}")
    return files[-1].stem


def _injury_watch(store: Store, season: str, day: str | None) -> dict[str, Any] | None:
    """The latest slate's injury calls (keyword rule vs. agent) and the season's head-to-head."""
    log_path, results_path = availability.paths(store, season)
    if not log_path.exists() or day is None:
        return None
    outcomes = {(r["game_id"], r["player_id"]): r for r in store.read(results_path)}
    calls = []
    for r in store.read(log_path):
        if r["game_date"] != day:
            continue
        o = outcomes.get((r["game_id"], r["player_id"]))
        calls.append(
            {
                "player": r["player"],
                "team": r["team"],
                "opponent": r["opponent"],
                "note": r["note"],
                "baseline_p": float(r["baseline_p"]),
                "agent_p": float(r["agent_p"]) if r["agent_p"] else None,
                "quote": r["agent_quote"],
                "minutes_restriction": r["minutes_restriction"] == "True",
                "played": None if o is None else o["played"] == "True",
                "minutes": int(o["minutes"]) if o else None,
            }
        )
    card_path = store.root / "scorecard" / f"{season}-availability.json"
    card = json.loads(card_path.read_text()) if card_path.exists() else None
    return {"date": day, "calls": calls, "scorecard": card} if calls or card else None


def _race(store: Store, season: str) -> dict[str, Any] | None:
    """Elo vs. the challenger vs. the betting market, on every finished game all three forecast
    before tip-off (the same games for each, so the numbers compare directly)."""
    results = {r["game_id"]: r for r in store.read(store.results_path(season))}
    v2 = shadow_v2.load_log(store.root, season)
    games = []
    for e in store.read(store.predictions_path(season)):
        r, c = results.get(e["game_id"]), v2.get(e["game_id"])
        if r is None or c is None or not e["market_p_home"]:
            continue
        y = int(int(r["home_score"]) > int(r["away_score"]))
        games.append((e["game_date"], y, float(e["p_home"]), float(c["p_home"]),
                      float(e["market_p_home"])))  # fmt: skip
    if not games:
        return None
    y = [g[1] for g in games]
    rows = []
    for name, i, cls in (("Elo (published)", 2, "s1"), ("Player + injury model", 3, "s3"),
                         ("Betting market", 4, "s2")):  # fmt: skip
        probs = [g[i] for g in games]
        ll = metrics.log_loss(probs, y)
        rows.append({"name": name, "cls": cls, "log_loss": ll,
                     "right": sum((p >= 0.5) == bool(o) for p, o in zip(probs, y, strict=True)),
                     "skill": max(0.0, 1 - ll / math.log(2))})  # fmt: skip
    best = min(r["log_loss"] for r in rows)
    for r in rows:
        r["leads"] = r["log_loss"] == best
    return {"games": len(games), "since": min(g[0] for g in games), "rows": rows}


def _latest_recap(store: Store) -> dict[str, Any] | None:
    """The newest published recap as blocks: {"p": text} paragraphs and {"ul": [items]} lists."""
    files = sorted((store.root / "recaps").glob("*.md"))
    if not files:
        return None
    source, body = recap.read_published(files[-1])
    blocks = []
    for chunk in body.split("\n\n"):
        lines = [line.strip() for line in chunk.strip().splitlines() if line.strip()]
        if lines and all(line.startswith("- ") for line in lines):
            blocks.append({"ul": [line[2:] for line in lines]})
        elif lines:
            blocks.append({"p": " ".join(lines)})
    day = files[-1].stem
    return {"date": day, "source": source, "blocks": blocks, "facts_file": f"recaps/{day}.json"}


def build_context(data_dir: Path) -> dict[str, Any]:
    store = Store(data_dir)
    season = _latest_season(store)
    predictions = store.read(store.predictions_path(season))
    finished = {r["game_id"] for r in store.read(store.results_path(season))}
    scorecard = (
        json.loads(store.scorecard_path(season).read_text())
        if store.scorecard_path(season).exists()
        else None
    )
    state = store.load_state()
    latest_date = max((p["game_date"] for p in predictions), default=None)

    hca = state.home_court()
    team_map = fan.teams(store.root, state.ratings, state.params.mean)
    tonight = fan.matchups(store, season, latest_date, team_map) if latest_date else []
    standings = fan.standings(store.root, team_map)
    ratings_now = [t.rating for t in team_map.values() if t.rating is not None]
    return {
        "team_map": team_map,
        "espn_images": fan.USE_ESPN_IMAGES,
        "slate_date": latest_date,
        "games": tonight,
        "spotlight": fan.game_of_the_night([m for m in tonight if not m.final]),
        "ticker": fan.last_night(store, season, latest_date, team_map) if latest_date else [],
        "standings": standings,
        "power": sorted(
            (t for t in team_map.values() if t.rating is not None), key=lambda t: -t.rating
        ),
        "win_axis": WIN_AXIS,
        "rating_span": max([abs(r) for r in ratings_now] + [1.0]),
        "home_court_points": hca / projections.ELO_PER_POINT,
        "race": _race(store, season),
        "repo_url": REPO_URL,
        "season": season,
        "built_at": datetime.now(UTC).astimezone(CENTRAL).strftime("%b %-d, %Y %-I:%M %p CT"),
        "ratings_through": state.last_date.isoformat() if state.last_date else None,
        "latest_date": latest_date,
        "scorecard": scorecard,
        "games_scored": sum(1 for p in predictions if p["game_id"] in finished),
        "recap": _latest_recap(store),
        "injury_watch": _injury_watch(store, season, latest_date),
    }
