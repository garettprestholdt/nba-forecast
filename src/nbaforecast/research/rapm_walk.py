"""Leak-free player impact ratings (RAPM) for the backtest, and the team strengths they add up to.

The live site refits RAPM every morning (`impact.py`) on last season's and this season's
stints. The backtest does the same thing as of each date: every REFIT_DAYS it refits on the
stints of the previous season and the current season *before that date*, with the box-score
prior built from the same games, so a game's features never see that game or anything after it.

Stints come from the local play-by-play cache (`ingest/playbyplay.py`, the same slim summaries
`nbaf pbp` keeps) through `court.parse_stints`, and are cached as one parquet file per season
under `<data_dir>/raw/stints/`. Backtest-only: the live model reads the daily `players/impact.json`.
"""

import gzip
import json
import logging
from bisect import bisect_right
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from nbaforecast.ingest import playbyplay
from nbaforecast.models import impact
from nbaforecast.pipeline import court

log = logging.getLogger(__name__)

REFIT_DAYS = 14


def stints_path(data_dir: Path, season: str) -> Path:
    return Path(data_dir) / "raw" / "stints" / f"season={season}.parquet"


def load_stints(data_dir: Path, seasons: Iterable[str], games: pd.DataFrame) -> pd.DataFrame:
    """Stints for every cached game in `seasons` (built once per season, then read back)."""
    frames = []
    for season in seasons:
        path = stints_path(data_dir, season)
        if not path.exists():  # build from the plays cache
            rows = []
            by_id = {
                str(g["game_id"]): g for g in games[games["season"] == season].to_dict("records")
            }
            for f in sorted(playbyplay.plays_dir(data_dir, season).glob("*.json.gz")):
                g = by_id.get(f.name.split(".")[0])
                if g is None:
                    continue
                slim = json.loads(gzip.decompress(f.read_bytes()))
                game = {**g, "game_id": str(g["game_id"]), "game_date": str(g["game_date"]),
                        "home_team_id": str(g["home_team_id"]),
                        "away_team_id": str(g["away_team_id"])}  # fmt: skip
                try:
                    rows += court.parse_stints(slim, game)
                except (ValueError, KeyError) as exc:  # a handful of malformed clocks
                    log.warning("stints for %s skipped: %s", game["game_id"], exc)
            df = pd.DataFrame(rows, columns=court.STINT_FIELDS)
            expected = int(((games["season"] == season)
                            & (games["season_type"] == "regular_season")).sum())  # fmt: skip
            if df["game_id"].nunique() >= 0.98 * expected:  # cache only a complete season
                path.parent.mkdir(parents=True, exist_ok=True)
                df.to_parquet(path, index=False)
            else:
                log.warning("%s: stints for %d of %d games; not cached",
                            season, df["game_id"].nunique(), expected)  # fmt: skip
        else:
            df = pd.read_parquet(path)
        frames.append(df.assign(season=season))
    out = pd.concat(frames, ignore_index=True)
    out["game_date"] = pd.to_datetime(out["game_date"]).dt.date
    return out


def _season_window(season: str) -> list[str]:
    return [court.previous_season(season), season]


class Ratings:
    """Snapshots of (offense, defense) per player, each fit only on games before its date."""

    def __init__(self, snapshots: list[tuple[date, dict[str, tuple[float, float]]]]):
        self.dates = [d for d, _ in snapshots]
        self.values = [v for _, v in snapshots]

    def at(self, day: date) -> dict[str, tuple[float, float]]:
        """The latest snapshot dated on or before `day`. A snapshot dated d was fit only on
        games before d, so it's safe for games on d. Empty before the first one."""
        i = bisect_right(self.dates, day) - 1
        return self.values[i] if i >= 0 else {}


def refit_dates(games: pd.DataFrame, seasons: Iterable[str]) -> list[tuple[str, date]]:
    """Every REFIT_DAYS through each season, starting the day before its first game."""
    out = []
    for season in seasons:
        days = sorted(games.loc[games["season"] == season, "game_date"])
        if not days:
            continue
        d, end = days[0], days[-1]
        while d <= end:
            out.append((season, d))
            d += timedelta(days=REFIT_DAYS)
    return out


def walk_ratings(
    stints: pd.DataFrame, box_rows: pd.DataFrame, games: pd.DataFrame, seasons: Iterable[str]
) -> Ratings:
    """Refit RAPM (plain and box-prior, blended as on the site) as of each refit date."""
    box = box_rows.copy()
    box["game_date"] = pd.to_datetime(box["game_date"]).dt.date
    snaps = []
    for season, day in refit_dates(games, seasons):
        window = _season_window(season)
        s = stints[stints["season"].isin(window) & (stints["game_date"] < day)]
        if s.empty:
            continue
        b = box[box["season"].isin(window) & (box["game_date"] < day) & (box["minutes"] > 0)]
        lines = b.groupby("player_id")[["minutes", "blk", "stl", "reb"]].sum().reset_index()
        lines["player_id"] = lines["player_id"].astype(str)
        rows = s[["offense", "defense", "pts", "poss", "home"]].astype(str).to_dict("records")
        fit = impact.fit(rows, impact.per36_rates(lines.to_dict("records")))
        snaps.append((day, {p: impact.blend(r) for p, r in fit["players"].items()}))
        log.info(
            "RAPM as of %s: %d players from %d stint ends", day, len(fit["players"]), len(rows)
        )
    return Ratings(snaps)
