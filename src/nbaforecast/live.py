"""The daily job: ingest last night's results, update Elo, predict today's games, keep score.

It runs in GitHub Actions against a checkout of the `data` branch (`root`):

    state/elo_state.json        ratings and everything else Elo needs to resume
    results/<season>.csv        completed games, as ingested
    predictions/<season>.csv    append-only; one row per game, written before tip-off
    scorecard/<season>.json     season to date: model vs. market vs. coin flip

`nbaf bootstrap` creates the first state from the local history; `nbaf daily` advances it.
"""

import copy
import csv
import json
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from nbaforecast import config, metrics
from nbaforecast.ingest import espn, odds
from nbaforecast.models import elo
from nbaforecast.models.elo import EloParams, EloState

log = logging.getLogger(__name__)

# Adaptive home court won the Phase 2 holdout: docs/findings/elo-backtest.md
LIVE_PARAMS = EloParams(k=20, hca_window=2460, carryover=0.7)
MODEL_NAME = "elo-adaptive-v1"

# Games that will never be played on this date: skip them instead of waiting for them.
NOT_PLAYED = {"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED", "STATUS_FORFEIT"}

RESULT_FIELDS = [
    "game_id",
    "season",
    "season_type",
    "game_date",
    "tipoff_utc",
    "home_team_id",
    "away_team_id",
    "home_team",
    "away_team",
    "home_score",
    "away_score",
    "is_neutral_site",
]
PREDICTION_FIELDS = [
    "game_id",
    "season",
    "season_type",
    "game_date",
    "tipoff_utc",
    "home_team_id",
    "away_team_id",
    "home_team",
    "away_team",
    "is_neutral_site",
    "p_home",
    "home_elo",
    "away_elo",
    "home_court",
    "market_p_home",
    "market_books",
    "model",
    "ratings_through",
    "predicted_at",
    "code_sha",
]

Fetcher = Callable[[date], dict[str, Any]]


def franchise_ids() -> set[str]:
    with open(config.REPO_ROOT / "dbt" / "seeds" / "franchises.csv") as f:
        return {row["franchise_id"] for row in csv.DictReader(f)}


def season_for(day: date) -> str:
    start = day.year if day.month >= 9 else day.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


class Store:
    """Files on the data branch."""

    def __init__(self, root: Path):
        self.root = Path(root)

    @property
    def state_path(self) -> Path:
        return self.root / "state" / "elo_state.json"

    def results_path(self, season: str) -> Path:
        return self.root / "results" / f"{season}.csv"

    def predictions_path(self, season: str) -> Path:
        return self.root / "predictions" / f"{season}.csv"

    def scorecard_path(self, season: str) -> Path:
        return self.root / "scorecard" / f"{season}.json"

    def load_state(self) -> EloState:
        if not self.state_path.exists():
            raise FileNotFoundError(f"{self.state_path} not found: run `nbaf bootstrap` first")
        return EloState.from_dict(json.loads(self.state_path.read_text()))

    def save_state(self, state: EloState) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state.to_dict(), indent=1) + "\n")

    @staticmethod
    def read(path: Path) -> list[dict[str, str]]:
        if not path.exists():
            return []
        with open(path, newline="") as f:
            return list(csv.DictReader(f))

    @staticmethod
    def append(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> int:
        """Append rows, never rewriting what's already there (the file stays append-only)."""
        rows = list(rows)
        if not rows:
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists()
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
            if new_file:
                writer.writeheader()
            writer.writerows(rows)
        return len(rows)


def _franchise_games(board: dict, franchises: set[str], fetched_at: datetime) -> list[dict]:
    games = espn.parse_games(board, fetched_at)
    return [g for g in games if g["home_team_id"] in franchises and g["away_team_id"] in franchises]


def _result_row(g: dict) -> dict:
    return {
        "game_id": g["game_id"],
        "season": g["season"],
        "season_type": g["season_type"],
        "game_date": g["game_date"].isoformat(),
        "tipoff_utc": g["tipoff_utc"].isoformat(),
        "home_team_id": g["home_team_id"],
        "away_team_id": g["away_team_id"],
        "home_team": g["home_team_name"],
        "away_team": g["away_team_name"],
        "home_score": g["home_score"],
        "away_score": g["away_score"],
        "is_neutral_site": g["neutral_site"],
    }


def ingest_results(
    store: Store,
    state: EloState,
    fetch: Fetcher,
    through: date,
    franchises: set[str],
    now: datetime,
) -> list[dict]:
    """Rate every completed game after `state.last_date`, up to and including `through`.

    Stops early at a date whose games aren't all final yet; the next run picks it up. Each date
    is processed exactly like the backtest: predict from start-of-day ratings, then update.
    """
    if state.last_date is None:
        raise ValueError("state has no last_date: run `nbaf bootstrap` first")
    ingested = []
    day = state.last_date + timedelta(days=1)
    while day <= through:
        games = _franchise_games(fetch(day), franchises, now)
        pending = [g for g in games if not g["completed"] and g["status"] not in NOT_PLAYED]
        if pending:
            log.warning("%s: %d games not final yet; stopping before this date", day, len(pending))
            break
        final = sorted(
            (g for g in games if g["completed"]), key=lambda g: (g["tipoff_utc"], g["game_id"])
        )
        if final:
            state.start_day(final[0]["season"])
            forecasts = [
                state.predict(g["home_team_id"], g["away_team_id"], g["neutral_site"])
                for g in final
            ]
            for g, f in zip(final, forecasts, strict=True):
                state.update(f, g["home_score"], g["away_score"])
            for season in {g["season"] for g in final}:
                known = {r["game_id"] for r in store.read(store.results_path(season))}
                rows = [
                    _result_row(g)
                    for g in final
                    if g["season"] == season and g["game_id"] not in known
                ]
                store.append(store.results_path(season), RESULT_FIELDS, rows)
                ingested.extend(rows)
        state.last_date = day
        day += timedelta(days=1)
    return ingested


def predict_games(
    store: Store,
    state: EloState,
    board: dict,
    franchises: set[str],
    now: datetime,
    market: list[odds.MarketLine],
    code_sha: str,
) -> tuple[list[dict], list[str]]:
    """Predict today's games that haven't tipped off and aren't already predicted.

    Returns (new prediction rows, ids of games skipped because they had already started).
    """
    games = [g for g in _franchise_games(board, franchises, now) if g["status"] not in NOT_PLAYED]
    if not games:
        return [], []
    preview = copy.deepcopy(state)  # start-of-day ratings; the real state isn't advanced
    preview.start_day(games[0]["season"])

    rows, started = [], []
    for g in sorted(games, key=lambda g: (g["tipoff_utc"], g["game_id"])):
        if g["completed"] or g["tipoff_utc"] <= now:
            started.append(g["game_id"])
            continue
        already = {r["game_id"] for r in store.read(store.predictions_path(g["season"]))}
        if g["game_id"] in already:
            continue
        f = preview.predict(g["home_team_id"], g["away_team_id"], g["neutral_site"])
        line = odds.match(market, g["home_team_name"], g["away_team_name"], g["tipoff_utc"])
        rows.append(
            {
                "game_id": g["game_id"],
                "season": g["season"],
                "season_type": g["season_type"],
                "game_date": g["game_date"].isoformat(),
                "tipoff_utc": g["tipoff_utc"].isoformat(),
                "home_team_id": g["home_team_id"],
                "away_team_id": g["away_team_id"],
                "home_team": g["home_team_name"],
                "away_team": g["away_team_name"],
                "is_neutral_site": g["neutral_site"],
                "p_home": round(f.p_home, 6),
                "home_elo": round(f.home_elo, 2),
                "away_elo": round(f.away_elo, 2),
                "home_court": round(f.home_court, 2),
                "market_p_home": round(line.p_home, 6) if line else "",
                "market_books": line.books if line else 0,
                "model": MODEL_NAME,
                "ratings_through": state.last_date.isoformat() if state.last_date else "",
                "predicted_at": now.isoformat(timespec="seconds"),
                "code_sha": code_sha,
            }
        )
    for season in {r["season"] for r in rows}:
        store.append(
            store.predictions_path(season),
            PREDICTION_FIELDS,
            [r for r in rows if r["season"] == season],
        )
    return rows, started


def _score(probs: list[float], outcomes: list[int]) -> dict[str, float] | None:
    if not probs:
        return None
    return {
        "log_loss": round(metrics.log_loss(probs, outcomes), 5),
        "brier": round(metrics.brier_score(probs, outcomes), 5),
        "accuracy": round(metrics.accuracy(probs, outcomes), 5),
    }


def scorecard(store: Store, season: str, now: datetime) -> dict:
    """Season-to-date scores for every prediction whose game has a result."""
    results = {r["game_id"]: r for r in store.read(store.results_path(season))}
    scored = []
    for p in store.read(store.predictions_path(season)):
        r = results.get(p["game_id"])
        if r is not None:
            scored.append((p, int(int(r["home_score"]) > int(r["away_score"]))))
    model = [float(p["p_home"]) for p, _ in scored]
    outcomes = [y for _, y in scored]
    with_market = [(p, y) for p, y in scored if p["market_p_home"] != ""]
    return {
        "season": season,
        "model": MODEL_NAME,
        "updated_at": now.isoformat(timespec="seconds"),
        "games_predicted": len(store.read(store.predictions_path(season))),
        "games_scored": len(scored),
        "elo": _score(model, outcomes),
        "coin_flip": _score([0.5] * len(scored), outcomes),
        "head_to_head_with_market": {
            "games": len(with_market),
            "elo": _score(
                [float(p["p_home"]) for p, _ in with_market], [y for _, y in with_market]
            ),
            "market": _score(
                [float(p["market_p_home"]) for p, _ in with_market], [y for _, y in with_market]
            ),
        },
    }


def daily(
    root: Path,
    *,
    today: date,
    now: datetime,
    fetch: Fetcher,
    odds_events: list[dict] | None,
    code_sha: str,
) -> dict:
    """One daily run. `today` is the US Eastern date; `now` is the current UTC time."""
    store = Store(root)
    state = store.load_state()
    franchises = franchise_ids()

    ingested = ingest_results(store, state, fetch, today - timedelta(days=1), franchises, now)
    market = odds.market_lines(odds_events) if odds_events else []
    predicted, started = predict_games(
        store, state, fetch(today), franchises, now, market, code_sha
    )
    store.save_state(state)

    season = season_for(today)
    card = scorecard(store, season, now)
    store.scorecard_path(season).parent.mkdir(parents=True, exist_ok=True)
    store.scorecard_path(season).write_text(json.dumps(card, indent=2) + "\n")
    return {
        "results_ingested": len(ingested),
        "ratings_through": state.last_date.isoformat(),
        "predicted": len(predicted),
        "with_market": sum(1 for r in predicted if r["market_p_home"] != ""),
        "already_started": len(started),
        "scorecard": card,
    }


def bootstrap(
    root: Path, games: pd.DataFrame, through: date, params: EloParams = LIVE_PARAMS
) -> EloState:
    """First state for the data branch: rate every game up to `through` (inclusive).

    Also seeds this season's results file, so the season log is complete from opening night.
    """
    store = Store(root)
    if store.state_path.exists():
        raise FileExistsError(f"{store.state_path} already exists; refusing to overwrite it")
    history = games[games["game_date"] <= through]
    state = EloState(params)
    elo.walk(history, state)
    state.last_date = through
    store.save_state(state)

    season = season_for(through)
    current = history[history["season"] == season].sort_values(["tipoff_utc", "game_id"])
    store.append(
        store.results_path(season),
        RESULT_FIELDS,
        [
            {
                "game_id": g.game_id,
                "season": g.season,
                "season_type": g.season_type,
                "game_date": g.game_date.isoformat(),
                "tipoff_utc": pd.Timestamp(g.tipoff_utc).tz_convert(UTC).isoformat(),
                "home_team_id": g.home_team_id,
                "away_team_id": g.away_team_id,
                "home_team": g.home_team_name,
                "away_team": g.away_team_name,
                "home_score": int(g.home_score),
                "away_score": int(g.away_score),
                "is_neutral_site": bool(g.is_neutral_site),
            }
            for g in current.itertuples(index=False)
        ],
    )
    return state


def verify(root: Path, previous: Callable[[Path], str | None]) -> list[str]:
    """Integrity checks run before every daily commit. Returns a list of problems.

    For game predictions and injury (availability) calls alike:
    - the log is append-only: the committed file must be an exact prefix of the new one
    - every row was written before its game's tip-off
    - nothing is predicted twice
    Results files must not record a game (or a player in a game) twice.
    """
    store = Store(root)
    problems = []
    logs = [
        (sorted((store.root / "predictions").glob("*.csv")), ("game_id",)),
        (
            [
                p
                for p in sorted((store.root / "availability").glob("*.csv"))
                if not p.stem.endswith("-results")
            ],
            ("game_id", "player_id"),
        ),
    ]
    for files, key in logs:
        for path in files:
            rel = path.relative_to(store.root)
            old_text = previous(rel)
            if old_text is not None and not path.read_text().startswith(old_text):
                problems.append(f"{rel}: existing rows were changed (must be append-only)")
            rows = store.read(path)
            keys = [tuple(r[k] for k in key) for r in rows]
            if len(keys) != len(set(keys)):
                problems.append(f"{rel}: duplicate {'/'.join(key)}")
            for r in rows:
                if datetime.fromisoformat(r["predicted_at"]) >= datetime.fromisoformat(
                    r["tipoff_utc"]
                ):
                    problems.append(
                        f"{rel}: {'/'.join(r[k] for k in key)} written at/after tip-off"
                    )
    results = [(p, ("game_id",)) for p in sorted((store.root / "results").glob("*.csv"))]
    results += [
        (p, ("game_id", "player_id"))
        for p in sorted((store.root / "availability").glob("*-results.csv"))
    ]
    for path, key in results:
        keys = [tuple(r[k] for k in key) for r in store.read(path)]
        if len(keys) != len(set(keys)):
            problems.append(f"{path.relative_to(store.root)}: duplicate {'/'.join(key)}")
    return problems
