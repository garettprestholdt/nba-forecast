"""Player box scores for every game, from ESPN's game summary endpoint (one request per game).

Output is one raw Parquet file per season, one row per player per game:

    <data_dir>/raw/espn_boxscores/season=2004-05.parquet

Finished seasons already on disk are skipped; for the current season only new games are fetched.
Plus-minus wasn't recorded in older seasons (ESPN shows 0 for everyone), so a game where every
player's +/- is 0 gets null plus-minus rather than fake zeros.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from nbaforecast.ingest.backfill import current_season_start, season_label
from nbaforecast.ingest.injuries import _athlete_id, fetch_summary

log = logging.getLogger(__name__)

FRANCHISES = {str(i) for i in range(1, 31)}  # skips All-Star games
SHOOTING = {"FG": ("fgm", "fga"), "3PT": ("fg3m", "fg3a"), "FT": ("ftm", "fta")}
COUNTS = {
    "MIN": "minutes", "PTS": "pts", "REB": "reb", "AST": "ast", "TO": "tov", "STL": "stl",
    "BLK": "blk", "OREB": "oreb", "DREB": "dreb", "PF": "pf",
}  # fmt: skip
STAT_COLUMNS = ["minutes", "pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb",
                "reb", "ast", "stl", "blk", "tov", "pf"]  # fmt: skip

Fetcher = Callable[[str], dict[str, Any]]


def raw_path(data_dir: Path, season: str) -> Path:
    return data_dir / "raw" / "espn_boxscores" / f"season={season}.parquet"


def _int(value: Any) -> int:
    text = str(value).strip()
    return int(text) if text.lstrip("+-").isdigit() else 0


def parse_boxscore(summary: dict[str, Any], game: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows for every player listed in a final box score. `game` is the row from espn_games."""
    rows = []
    home_id, away_id = str(game["home_team_id"]), str(game["away_team_id"])
    for team in summary.get("boxscore", {}).get("players", []):
        team_id = str(team["team"]["id"])
        if team_id not in (home_id, away_id):
            continue
        for block in team.get("statistics", [])[:1]:
            names = block.get("names", [])
            for a in block.get("athletes", []):
                athlete = a.get("athlete") or {}
                name = athlete.get("displayName") or athlete.get("shortName") or ""
                player_id = _athlete_id(athlete)
                if player_id is None and name:
                    # ESPN occasionally lists a real player with no id (Lachlan Olbrich in
                    # 2025-26). Keep their stats under a stable stand-in id so totals add up.
                    player_id = f"noid-{team_id}-{'-'.join(name.lower().split())}"
                if player_id is None:
                    # Nameless, id-less rows (seen once, in 2012-13) carry no stats.
                    log.warning("game %s: player without an id skipped: %.120s", game["game_id"], a)
                    continue
                values = dict(zip(names, a.get("stats") or [], strict=False))
                row = {
                    "game_id": str(game["game_id"]),
                    "season": game["season"],
                    "game_date": game["game_date"],
                    "team_id": team_id,
                    "opponent_id": away_id if team_id == home_id else home_id,
                    "is_home": team_id == home_id,
                    "player_id": player_id,
                    "player": name,
                    "position": (athlete.get("position") or {}).get("abbreviation") or "",
                    "starter": bool(a.get("starter", False)),
                    "did_not_play": bool(a.get("didNotPlay", False)),
                    "dnp_reason": a.get("reason") or "",
                    "ejected": bool(a.get("ejected", False)),
                }
                for col in STAT_COLUMNS:
                    row[col] = 0
                for name, col in COUNTS.items():
                    row[col] = _int(values.get(name, 0))
                for name, (made, att) in SHOOTING.items():
                    m, _, t = str(values.get(name, "0-0")).partition("-")
                    row[made], row[att] = _int(m), _int(t)
                pm = values.get("+/-")
                row["plus_minus"] = _int(pm) if pm not in (None, "", "--") else None
                rows.append(row)
    # Plus-minus wasn't recorded in older seasons: all zeros means "missing", not "even".
    if rows and all(not r["plus_minus"] for r in rows):
        for r in rows:
            r["plus_minus"] = None
    return rows


@dataclass
class Result:
    season: str
    status: str  # "written", "skipped", or "empty"
    games: int = 0
    rows: int = 0
    missing: int = 0  # games ESPN returned no box score for


def backfill_season(
    data_dir: Path,
    season: str,
    fetch: Fetcher,
    *,
    pause: float,
    attempts: int,
    backoff: float,
    limit: int | None = None,
) -> Result:
    games_path = data_dir / "raw" / "espn_games" / f"season={season}.parquet"
    games = pd.read_parquet(games_path)
    games = games[
        games["completed"]
        & games["home_team_id"].astype(str).isin(FRANCHISES)
        & games["away_team_id"].astype(str).isin(FRANCHISES)
    ].sort_values(["tipoff_utc", "game_id"])
    out_path = raw_path(data_dir, season)
    existing = pd.read_parquet(out_path) if out_path.exists() else None
    done = set(existing["game_id"]) if existing is not None else set()
    todo = [g for g in games.to_dict("records") if str(g["game_id"]) not in done][:limit]

    rows, missing = [], 0
    for i, game in enumerate(todo, 1):
        summary = None
        for attempt in range(1, attempts + 1):
            try:
                summary = fetch(str(game["game_id"]))
                break
            except requests.HTTPError as exc:
                # ESPN has no summary for a few final games (e.g. a 2026 playoff game);
                # retrying won't help, so count it as missing and move on.
                if exc.response is not None and exc.response.status_code == 404:
                    log.warning("%s: no summary on ESPN (404); skipped", game["game_id"])
                    break
                if attempt == attempts:
                    raise
                log.warning("%s: %s; retrying", game["game_id"], exc)
                time.sleep(backoff * attempt)
            except (requests.RequestException, ValueError) as exc:
                if attempt == attempts:
                    raise
                log.warning("%s: %s; retrying", game["game_id"], exc)
                time.sleep(backoff * attempt)
        parsed = parse_boxscore(summary, game) if summary is not None else []
        if not parsed:
            missing += 1
        rows.extend(parsed)
        if pause:
            time.sleep(pause)
        if i % 200 == 0:
            log.info("%s: %d/%d games", season, i, len(todo))

    if not rows:
        return Result(season, "empty" if not done else "skipped", 0, 0, missing)
    df = pd.DataFrame(rows)
    if existing is not None:
        df = pd.concat([existing, df], ignore_index=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(out_path)  # atomic: a crash never leaves a half-written file
    return Result(season, "written", len(todo) - missing, len(df), missing)


def backfill(
    data_dir: Path,
    first: int = 2004,
    last: int | None = None,
    *,
    fetch: Fetcher | None = None,
    today=None,
    pause: float = 0.2,
    attempts: int = 4,
    backoff: float = 5.0,
    limit: int | None = None,
) -> list[Result]:
    from datetime import UTC, datetime

    today = today or datetime.now(UTC).date()
    current = current_season_start(today)
    last = current if last is None else last
    if fetch is None:
        session = requests.Session()

        def fetch(game_id: str) -> dict:
            return fetch_summary(game_id, session)

    results = []
    for start_year in range(first, last + 1):
        season = season_label(start_year)
        if raw_path(data_dir, season).exists() and start_year != current and limit is None:
            results.append(Result(season, "skipped"))
            continue
        started = time.monotonic()
        result = backfill_season(
            data_dir, season, fetch, pause=pause, attempts=attempts, backoff=backoff, limit=limit
        )
        results.append(result)
        log.info(
            "%s: %s, %d games, %d rows, %d without a box score, %.0fs",
            season, result.status, result.games, result.rows, result.missing,
            time.monotonic() - started,
        )  # fmt: skip
    return results
