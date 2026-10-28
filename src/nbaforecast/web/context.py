"""Everything the shared page templates need, read from the data branch and data/published/."""

import csv
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nbaforecast import config, metrics
from nbaforecast.agents import availability, recap
from nbaforecast.ingest.espn import EASTERN  # noqa: F401  (slate dates stay US Eastern)
from nbaforecast.models.elo import win_probability
from nbaforecast.pipeline import projections, shadow_v2
from nbaforecast.pipeline.live import Store
from nbaforecast.web import fan
from nbaforecast.web.charts import (
    ADAPTIVE,
    BACKTEST_NAMES,
    LIVE,
    _by_season_spec,
    _calibration_spec,
    _elo_vs_market_spec,
    _market_gap_spec,
    _ratings_spec,
)
from nbaforecast.web.filters import CENTRAL, WIN_AXIS, _tip_et

REPO_URL = "https://github.com/garettprestholdt/nba-forecast"


LIVE_CALIBRATION_MIN_GAMES = 100  # fewer than this and live calibration bins are mostly noise


def _simple_backtest(summary: dict, published: Path) -> list[dict]:
    """The held-out comparison in plain language: a few approaches, one line each."""
    rows = {r["model"]: r for r in summary["holdout"]}
    v1_path = published / "hybrid" / "summary.json"
    v1 = {r["model"]: r for r in json.loads(v1_path.read_text())["holdout"]} \
        if v1_path.exists() else {}  # fmt: skip
    v2_path = published / "hybrid_v2" / "backtest.json"
    v2 = {r["model"]: r for r in json.loads(v2_path.read_text())["holdout"]} \
        if v2_path.exists() else {}  # fmt: skip
    plan = [
        (rows.get("Coin flip"), "Coin flip", "Says 50% for every game.", "muted-row"),
        (rows.get("Previous season's home win rate"), "Always back the home team",
         "Says the home team wins as often as home teams did last season.", "muted-row"),
        (next((r for m, r in rows.items() if "adaptive" in m), None), "Our model (published)",
         "Rates every team from its results and margins; the forecast on this site.", "ours"),
        (v2.get(shadow_v2.MODEL_NAME), "Player + injury model (challenger)",
         "Our model plus each available player's production and offense and defense impact, "
         "and the value of whoever is out. Running in shadow mode against the published model.",
         ""),
        (v1.get("Hybrid, realistic"), "Team + player model (earlier challenger)",
         "Our model plus every player's production and plus-minus, but blind to injuries. "
         "Replaced by the player + injury model; not shown elsewhere.", "muted-row"),
    ]  # fmt: skip
    return [{"name": name, "what": what, "cls": cls, "games": r["n"],
             "accuracy": r["accuracy"], "log_loss": r["log_loss"]}
            for r, name, what, cls in plan if r]  # fmt: skip


def _franchises() -> dict[str, dict[str, str]]:
    with open(config.REPO_ROOT / "dbt" / "seeds" / "franchises.csv") as f:
        return {row["franchise_id"]: row for row in csv.DictReader(f)}


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


@dataclass
class Game:
    game_id: str
    game_date: str
    tip: str
    away: str
    home: str
    p_home: float
    market_p_home: float | None
    home_score: int | None = None
    away_score: int | None = None

    @property
    def final(self) -> bool:
        return self.home_score is not None

    @property
    def pick(self) -> str:
        return self.home if self.p_home >= 0.5 else self.away

    @property
    def pick_prob(self) -> float:
        return max(self.p_home, 1 - self.p_home)

    @property
    def market_pick(self) -> str | None:
        if self.market_p_home is None:
            return None
        return self.home if self.market_p_home >= 0.5 else self.away

    @property
    def home_won(self) -> bool | None:
        return None if not self.final else self.home_score > self.away_score

    @property
    def correct(self) -> bool | None:
        return None if not self.final else (self.p_home >= 0.5) == self.home_won

    @property
    def gap(self) -> float | None:
        """Elo minus market, in percentage points for the home team."""
        return None if self.market_p_home is None else self.p_home - self.market_p_home


def _games(store: Store, season: str) -> list[Game]:
    results = {r["game_id"]: r for r in store.read(store.results_path(season))}
    games = []
    for p in store.read(store.predictions_path(season)):
        r = results.get(p["game_id"])
        games.append(
            Game(
                game_id=p["game_id"],
                game_date=p["game_date"],
                tip=_tip_et(p["tipoff_utc"]),
                away=p["away_team"],
                home=p["home_team"],
                p_home=float(p["p_home"]),
                market_p_home=float(p["market_p_home"]) if p["market_p_home"] else None,
                home_score=int(r["home_score"]) if r else None,
                away_score=int(r["away_score"]) if r else None,
            )
        )
    return games


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


def _shadow(store: Store, season: str, published: Path) -> dict[str, Any] | None:
    """The challenger shown on the site (hybrid v2) : its live scorecard and backtest. Hybrid v1
    keeps logging in shadow (its pre-registered window runs on), but isn't shown."""
    card_path = shadow_v2.scorecard_path(store.root, season)
    backtest_path = published / "hybrid_v2" / "backtest.json"
    if not card_path.exists() or not backtest_path.exists():
        return None
    holdout = {r["model"]: r for r in json.loads(backtest_path.read_text())["holdout"]}
    return {
        "card": json.loads(card_path.read_text()),
        "backtest_challenger": holdout.get(shadow_v2.MODEL_NAME),
        "backtest_elo": holdout.get("Elo, adaptive home court (published)"),
    }


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


def _live_calibration(games: list[Game]) -> list[dict]:
    final = [g for g in games if g.final]
    if len(final) < LIVE_CALIBRATION_MIN_GAMES:
        return []
    table = metrics.calibration_table([g.p_home for g in final], [int(g.home_won) for g in final])
    return [
        {**row, "series": LIVE, "bin": f"{row['bin_low']:.0%}–{row['bin_high']:.0%}"}
        for row in table
    ]


def build_context(data_dir: Path, published_dir: Path | None = None) -> dict[str, Any]:
    store = Store(data_dir)
    published = published_dir or config.PUBLISHED_DIR
    season = _latest_season(store)
    games = _games(store, season)
    scorecard = (
        json.loads(store.scorecard_path(season).read_text())
        if store.scorecard_path(season).exists()
        else None
    )
    state = store.load_state()
    franchises = _franchises()

    latest_date = max((g.game_date for g in games), default=None)
    slate = [g for g in games if g.game_date == latest_date]
    recent = sorted(
        (g for g in games if g.final), key=lambda g: (g.game_date, g.game_id), reverse=True
    )

    teams = sorted(
        (
            {
                "name": franchises[t]["name"] if t in franchises else t,
                "abbr": franchises[t]["abbr"] if t in franchises else t,
                "elo": rating,
                "vs_average": rating - state.params.mean,
                "p_vs_average": win_probability(rating - state.params.mean),
            }
            for t, rating in state.ratings.items()
            if t in franchises
        ),
        key=lambda row: -row["elo"],
    )

    backtest_dir = published / "backtest"
    summary = json.loads((backtest_dir / "summary.json").read_text())
    labels = {
        "Elo (chosen)": "Elo, fixed home court (chosen on training seasons)",
        "Elo, best adaptive home court": "Elo, adaptive home court (the live model)",
    }
    for row in summary["holdout"]:
        row["model"] = labels.get(row["model"], row["model"])
    live_backtest = next(r for r in summary["holdout"] if "live model" in r["model"])
    backtest_calibration = [
        {
            "series": BACKTEST_NAMES[r["model"]],
            "bin": f"{float(r['bin_low']):.0%}–{float(r['bin_high']):.0%}",
            "n": int(r["n"]),
            "mean_forecast": float(r["mean_forecast"]),
            "observed_rate": float(r["observed_rate"]),
        }
        for r in _read_csv(backtest_dir / "calibration.csv")
        if r["model"] in BACKTEST_NAMES
    ]
    by_season = [
        {
            "season": r["season"],
            "split": r["split"],
            "series": BACKTEST_NAMES[r["model"]],
            "n": int(r["n"]),
            "log_loss": float(r["log_loss"]),
        }
        for r in _read_csv(backtest_dir / "by_season.csv")
        if r["split"] in ("train", "holdout") and r["model"] in BACKTEST_NAMES
    ]
    live_cal = _live_calibration(games)
    market_points = [
        {
            "date": g.game_date,
            "matchup": f"{g.away} @ {g.home}",
            "elo": g.p_home,
            "market": g.market_p_home,
            "result": f"{g.away_score}–{g.home_score}" if g.final else "Not played yet",
            "gap": round((g.p_home - g.market_p_home) * 100, 1),
            "outcome": "Not played yet"
            if not g.final
            else ("We got it right" if g.correct else "We got it wrong"),
        }
        for g in games
        if g.market_p_home is not None
    ]

    hca = state.home_court()
    team_map = fan.teams(store.root, state.ratings, state.params.mean)
    tonight = fan.matchups(store, season, latest_date, team_map) if latest_date else []
    v2_backtest = published / "hybrid_v2" / "backtest.json"
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
        "challenger_backtest": (
            json.loads(v2_backtest.read_text())["holdout"] if v2_backtest.exists() else None
        ),
        "shadow": _shadow(store, season, published),
        "repo_url": REPO_URL,
        "season": season,
        "built_at": datetime.now(UTC).astimezone(CENTRAL).strftime("%b %-d, %Y %-I:%M %p CT"),
        "ratings_through": state.last_date.isoformat() if state.last_date else None,
        "latest_date": latest_date,
        "slate": slate,
        "recent": recent[:30],
        "scorecard": scorecard,
        "teams": teams,
        "home_court": hca,
        "home_court_p": win_probability(hca),
        "model_params": state.params.as_dict(),
        "backtest": summary,
        "live_backtest": live_backtest,
        "charts": {
            "calibration": _calibration_spec(
                [r for r in backtest_calibration if r["series"] == ADAPTIVE] + live_cal
            ),
            "market_gap": _market_gap_spec(market_points) if market_points else None,
            "by_season": _by_season_spec(by_season),
            "elo_vs_market": _elo_vs_market_spec(market_points) if market_points else None,
            "ratings": _ratings_spec(teams),
        },
        "simple_backtest": _simple_backtest(summary, published),
        "live_calibration_ready": bool(live_cal),
        "live_calibration_min": LIVE_CALIBRATION_MIN_GAMES,
        "backtest_calibration": backtest_calibration,
        "games_scored": sum(1 for g in games if g.final),
        "recap": _latest_recap(store),
        "injury_watch": _injury_watch(store, season, latest_date),
    }
