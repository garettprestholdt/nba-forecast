"""Historical backfill of every NBA game from ESPN's scoreboard, one request per game day.

Output is one raw Parquet file per season, one row per game (regular season, play-in, playoffs):

    <data_dir>/raw/espn_games/season=2004-05.parquet
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from nbaforecast.ingest import espn

log = logging.getLogger(__name__)

FIRST_SEASON = 2004  # 2004-05: first season with 30 teams (Charlotte Bobcats joined)

Fetcher = Callable[[date], dict[str, Any]]


def season_label(start_year: int) -> str:
    """2004 -> '2004-05'."""
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def current_season_start(today: date) -> int:
    """Start year of the season in progress (or just finished). Seasons tip off in October."""
    return today.year if today.month >= 10 else today.year - 1


def raw_path(data_dir: Path, season: str) -> Path:
    return data_dir / "raw" / "espn_games" / f"season={season}.parquet"


def _fetch_with_retries(fetch: Fetcher, day: date, attempts: int, backoff: float) -> dict:
    for attempt in range(1, attempts + 1):
        try:
            return fetch(day)
        except (requests.RequestException, ValueError) as exc:
            if attempt == attempts:
                raise
            wait = backoff * attempt
            log.warning("%s failed (%s); retrying in %.0fs", day, exc, wait)
            time.sleep(wait)
    raise AssertionError("unreachable")


@dataclass
class Result:
    season: str
    status: str  # "written", "skipped", or "empty"
    games: int = 0
    requests: int = 0


def backfill_season(
    start_year: int,
    fetch: Fetcher,
    *,
    today: date,
    pause: float,
    attempts: int,
    backoff: float,
) -> tuple[pd.DataFrame, int]:
    """All games in one season. Returns (games, number of requests made)."""
    season = season_label(start_year)
    # Any mid-season date returns the full season calendar.
    anchor = date(start_year + 1, 1, 15)
    first = _fetch_with_retries(fetch, anchor, attempts, backoff)
    days = [d for d in espn.season_calendar(first) if d <= today]

    rows, n_requests = [], 1
    for day in days:
        board = first if day == anchor else _fetch_with_retries(fetch, day, attempts, backoff)
        if day != anchor:
            n_requests += 1
            if pause:
                time.sleep(pause)  # be polite to ESPN
        rows.extend(espn.parse_games(board, datetime.now(UTC)))

    df = pd.DataFrame(rows)
    if df.empty:
        return df, n_requests
    # Late games can show up on two adjacent dates' boards; keep the freshest copy.
    df = (
        df[df["season"] == season]
        .sort_values("fetched_at")
        .drop_duplicates("game_id", keep="last")
        .sort_values(["tipoff_utc", "game_id"])
        .reset_index(drop=True)
    )
    return df, n_requests


def backfill(
    data_dir: Path,
    first: int = FIRST_SEASON,
    last: int | None = None,
    *,
    force: bool = False,
    fetch: Fetcher | None = None,
    today: date | None = None,
    pause: float = 0.25,
    attempts: int = 4,
    backoff: float = 5.0,
) -> list[Result]:
    """Download seasons first..last (start years, inclusive) into the local data directory.

    Finished seasons already on disk are skipped unless `force` is set. The current season is
    always re-downloaded because it's still growing.
    """
    today = today or datetime.now(espn.EASTERN).date()
    current = current_season_start(today)
    last = current if last is None else last
    if fetch is None:
        session = requests.Session()

        def fetch(day: date) -> dict[str, Any]:
            return espn.fetch_scoreboard(day, session)

    results = []
    for start_year in range(first, last + 1):
        season = season_label(start_year)
        path = raw_path(data_dir, season)
        if path.exists() and not force and start_year != current:
            results.append(Result(season, "skipped"))
            continue

        started = time.monotonic()
        df, n_requests = backfill_season(
            start_year, fetch, today=today, pause=pause, attempts=attempts, backoff=backoff
        )
        if df.empty:
            results.append(Result(season, "empty", 0, n_requests))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        df.to_parquet(tmp, index=False)
        tmp.replace(path)  # atomic: a crash never leaves a half-written file
        results.append(Result(season, "written", len(df), n_requests))
        log.info(
            "%s: %d games from %d requests in %.0fs",
            season,
            len(df),
            n_requests,
            time.monotonic() - started,
        )

    return results
