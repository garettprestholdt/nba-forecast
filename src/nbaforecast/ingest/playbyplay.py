"""Rebuild player box scores from ESPN play-by-play, for games whose box score has no stats.

About 13% of ESPN's 2012-18 box scores list the roster but leave every stat blank, while the
same game's play-by-play is complete. This module:

1. caches a slimmed copy of each game's summary (plays + roster) under
   `<data_dir>/raw/espn_plays/season=YYYY-YY/<game_id>.json.gz`, so the builder can be
   re-run without re-downloading;
2. rebuilds every player's line from the plays (shots, free throws, rebounds, assists, steals,
   blocks, turnovers, fouls, and minutes and plus-minus from lineup tracking);
3. writes `<data_dir>/raw/espn_pbp_boxscores/season=YYYY-YY.parquet` in the same layout as
   `espn_boxscores`.

The cache also holds a sample of games that have a real box score; dbt compares the rebuilt
lines with those (`fct_pbp_validation`) so the method is checked before it is trusted.
"""

import gzip
import hashlib
import json
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from nbaforecast.ingest.boxscores import FRANCHISES, STAT_COLUMNS, parse_boxscore

log = logging.getLogger(__name__)

Fetcher = Callable[[str], dict[str, Any]]
PERIOD_SECONDS = {"regulation": 12 * 60, "overtime": 5 * 60}
# Fouls that aren't personal fouls in the box score.
NOT_PERSONAL = ("technical", "turnover", "delay", "3-second", "3 second")


def plays_dir(data_dir: Path, season: str) -> Path:
    return data_dir / "raw" / "espn_plays" / f"season={season}"


def rebuilt_path(data_dir: Path, season: str) -> Path:
    return data_dir / "raw" / "espn_pbp_boxscores" / f"season={season}.parquet"


def slim(summary: dict[str, Any]) -> dict[str, Any]:
    """The parts of a game summary the rebuild needs (about a tenth of the full JSON)."""
    plays = [
        {
            "type": (p.get("type") or {}).get("text", ""),
            "text": p.get("text", ""),
            "period": (p.get("period") or {}).get("number", 0),
            "clock": (p.get("clock") or {}).get("displayValue", ""),
            "scoring": bool(p.get("scoringPlay")),
            "shooting": bool(p.get("shootingPlay")),
            "attempted": p.get("pointsAttempted", 0),
            "team": str((p.get("team") or {}).get("id", "")),
            "players": [
                str((q.get("athlete") or {}).get("id", "")) for q in p.get("participants") or []
            ],
            "home": p.get("homeScore"),
            "away": p.get("awayScore"),
        }
        for p in summary.get("plays") or []
    ]
    return {"boxscore": {"players": summary.get("boxscore", {}).get("players", [])}, "plays": plays}


def clock_seconds(display: str) -> float:
    """ESPN game clock: "11:45" or, under a minute, "45.2"."""
    if ":" in display:
        minutes, seconds = display.split(":")
        return int(minutes) * 60 + float(seconds)
    return float(display or 0)


def period_length(period: int) -> int:
    return PERIOD_SECONDS["regulation"] if period <= 4 else PERIOD_SECONDS["overtime"]


def _kind(play: dict[str, Any]) -> str:
    """Classify a play by what it adds to the box score."""
    typ = play["type"].lower()
    if typ == "substitution":
        return "sub"
    if typ.startswith("free throw"):
        return "ft"
    if play["shooting"]:
        return "fg"
    if "rebound" in typ:
        return "reb"
    if "turnover" in typ or typ in ("traveling", "double dribble", "discontinued dribble"):
        return "tov"
    # "Personal Block" and "Shooting Block" are blocking fouls, not blocked shots.
    if ("foul" in typ or typ.endswith(" block") or typ == "offensive charge") and not any(
        word in typ for word in NOT_PERSONAL
    ):
        return "foul"
    return "other"


def _shot_value(play: dict[str, Any]) -> int:
    """2 or 3. Some older plays have pointsAttempted = 0, so fall back to the text."""
    if play["attempted"] in (2, 3):
        return play["attempted"]
    return 3 if "three point" in play["text"].lower() else 2


def _points(play: dict[str, Any]) -> int:
    """Points scored on this play (ESPN's scoreValue is 0 on made free throws)."""
    if not play["scoring"]:
        return 0
    kind = _kind(play)
    return _shot_value(play) if kind == "fg" else 1 if kind == "ft" else 0


def _tally(plays: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Counting stats per player id from the plays."""
    stats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for play in plays:
        kind, who, text = _kind(play), play["players"], play["text"].lower()
        if not who or not who[0]:
            continue  # team rebounds, team turnovers, and the like
        first = stats[who[0]]
        second = stats[who[1]] if len(who) > 1 and who[1] else None
        if kind == "fg":
            three = _shot_value(play) == 3
            first["fga"] += 1
            first["fg3a"] += three
            if play["scoring"]:
                first["fgm"] += 1
                first["fg3m"] += three
                first["pts"] += 3 if three else 2
                if second is not None and "assist" in text:
                    second["ast"] += 1
            elif second is not None and "block" in text:
                second["blk"] += 1
        elif kind == "ft":
            first["fta"] += 1
            if play["scoring"]:
                first["ftm"] += 1
                first["pts"] += 1
        elif kind == "reb":
            first["oreb" if "offensive" in play["type"].lower() else "dreb"] += 1
        elif kind == "tov":
            first["tov"] += 1
            if second is not None and "steal" in text:
                second["stl"] += 1
        elif kind == "foul":
            first["pf"] += 1
            if second is not None and "double personal" in play["type"].lower():
                second["pf"] += 1  # both players are charged
    return stats


def _lineups(
    plays: list[dict[str, Any]],
    team_of: dict[str, str],
    starters: dict[str, set[str]],
    home_id: str,
) -> tuple[dict[str, float], dict[str, int], dict[str, bool]]:
    """Seconds on court, plus-minus per player, and whether each team's lineup stayed
    consistent, by tracking who is on the floor.

    ESPN logs substitutions within a period but not lineup changes between periods. A player
    is on the floor at the start of a period if their first appearance in it isn't entering the
    game; if that finds fewer than five, players on the floor at the end of the previous
    period who don't enter later fill the gap (someone can play a whole quarter without
    showing up in the plays).

    ESPN's play-by-play also drops the odd substitution. A team's lineup is consistent if it
    has exactly five players at every score change and every player subbed out was on the
    floor; plus-minus is only trusted for consistent teams.
    """
    by_period: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for play in plays:
        if play["period"]:
            by_period[play["period"]].append(play)

    seconds: dict[str, float] = defaultdict(float)
    plus_minus: dict[str, int] = defaultdict(int)
    on_court = {team: set(players) for team, players in starters.items()}
    consistent = {team: len(players) == 5 for team, players in on_court.items()}

    for period in sorted(by_period):
        period_plays = by_period[period]
        if period > 1:
            on_court = _period_starters(period_plays, team_of, on_court)
        since = {p: float(period_length(period)) for team in on_court.values() for p in team}
        for play in _subs_last(period_plays):
            now = clock_seconds(play["clock"])
            points = _points(play)
            scorer = team_of.get(play["players"][0]) if points and play["players"] else None
            if scorer in on_court:
                for team, players in on_court.items():
                    if len(players) != 5:
                        consistent[team] = False
                    for p in players:
                        plus_minus[p] += points if team == scorer else -points
            if _kind(play) == "sub":
                entering, leaving = _sub_sides(play)
                team = team_of.get(entering or "") or team_of.get(leaving or "")
                if team not in on_court:
                    continue
                if leaving is not None:
                    if leaving in on_court[team]:
                        seconds[leaving] += since.pop(leaving, now) - now
                        on_court[team].discard(leaving)
                    else:
                        consistent[team] = False  # they came back in on a missing play
                if entering is not None:
                    on_court[team].add(entering)
                    since[entering] = now
        for team in on_court.values():
            for p in team:
                seconds[p] += since.get(p, 0.0)
    return seconds, plus_minus, consistent


def _sub_sides(play: dict[str, Any]) -> tuple[str | None, str | None]:
    """(entering, leaving) for a substitution. ESPN occasionally splits one into two plays with
    one player each ("Brook Lopez enters the game for " / " enters the game for Isaiah
    Thomas"); the text says which side the lone player is on."""
    players = [p for p in play["players"] if p]
    if len(players) >= 2:
        return players[0], players[1]
    if len(players) == 1:
        leaving_only = play["text"].lstrip().lower().startswith("enters the game")
        return (None, players[0]) if leaving_only else (players[0], None)
    return None, None


def _subs_last(plays: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Within one moment of game clock, move substitutions after the other plays.

    Players often check in between two free throws; both shots belong to the lineup that was on
    the floor when the foul was called, which is how the NBA credits plus-minus.
    """
    return sorted(plays, key=lambda p: (-clock_seconds(p["clock"]), _kind(p) == "sub"))


def _period_starters(
    plays: list[dict[str, Any]], team_of: dict[str, str], previous: dict[str, set[str]]
) -> dict[str, set[str]]:
    first_seen: dict[str, str] = {}
    for play in plays:
        kind = _kind(play)
        if kind == "sub":
            entering, leaving = _sub_sides(play)
            if entering is not None:
                first_seen.setdefault(entering, "in")
            if leaving is not None:
                first_seen.setdefault(leaving, "out")
        elif kind != "other" or play["type"].lower() == "jump ball":
            for p in play["players"]:
                if p in team_of:
                    first_seen.setdefault(p, "act")
    lineup = {team: set() for team in previous}
    for p, how in first_seen.items():
        team = team_of.get(p)
        if how != "in" and team in lineup:
            lineup[team].add(p)
    for team, players in lineup.items():
        if len(players) < 5:
            for p in sorted(previous[team]):
                if len(players) >= 5:
                    break
                if first_seen.get(p) != "in":
                    players.add(p)
    return lineup


def rebuild_boxscore(summary: dict[str, Any], game: dict[str, Any]) -> list[dict[str, Any]]:
    """Box-score rows (same columns as `boxscores.parse_boxscore`) rebuilt from the plays."""
    roster = parse_boxscore(summary, game)
    plays = summary.get("plays") or []
    if not roster or not plays:
        return []
    team_of = {r["player_id"]: r["team_id"] for r in roster}
    starters: dict[str, set[str]] = {team: set() for team in team_of.values()}
    for r in roster:
        if r["starter"]:
            starters[r["team_id"]].add(r["player_id"])
    counts = _tally(plays)
    seconds, plus_minus, consistent = _lineups(plays, team_of, starters, str(game["home_team_id"]))
    rows = []
    for r in roster:
        p = r["player_id"]
        row = {**r, **{col: counts.get(p, {}).get(col, 0) for col in STAT_COLUMNS}}
        row["reb"] = row["oreb"] + row["dreb"]
        row["minutes"] = round(seconds.get(p, 0.0) / 60)
        clean = consistent.get(r["team_id"], False)
        row["lineup_consistent"] = clean
        row["plus_minus"] = (plus_minus.get(p, 0) if seconds.get(p) else 0) if clean else None
        rows.append(row)
    return rows


def _cache_path(data_dir: Path, season: str, game_id: str) -> Path:
    return plays_dir(data_dir, season) / f"{game_id}.json.gz"


def fetch_plays(
    data_dir: Path,
    games: pd.DataFrame,
    fetch: Fetcher,
    *,
    pause: float = 0.2,
    attempts: int = 4,
    backoff: float = 5.0,
) -> tuple[int, int]:
    """Cache slimmed summaries for `games` (season, game_id); returns (fetched, missing)."""
    fetched = missing = 0
    for i, game in enumerate(games.to_dict("records"), 1):
        path = _cache_path(data_dir, game["season"], str(game["game_id"]))
        if path.exists():
            continue
        summary = None
        for attempt in range(1, attempts + 1):
            try:
                summary = fetch(str(game["game_id"]))
                break
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    break
                if attempt == attempts:
                    raise
                time.sleep(backoff * attempt)
            except (requests.RequestException, ValueError):
                if attempt == attempts:
                    raise
                time.sleep(backoff * attempt)
        if summary is None or not summary.get("plays"):
            missing += 1
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(gzip.compress(json.dumps(slim(summary)).encode()))
        tmp.replace(path)
        fetched += 1
        if pause:
            time.sleep(pause)
        if i % 200 == 0:
            log.info("plays: %d/%d games", i, len(games))
    return fetched, missing


def _sample_key(game_id: str) -> str:
    return hashlib.md5(game_id.encode()).hexdigest()  # stable, arbitrary order


def select_games(data_dir: Path, per_season: int) -> pd.DataFrame:
    """Games to rebuild: every game with a team box score lacking stats, plus `per_season`
    games with complete box scores from each of those seasons, for validation."""
    columns = ["season", "game_id", "team_id", "minutes", "pts", "fga", "reb", "ast"]
    box = pd.concat(
        pd.read_parquet(path, columns=columns)
        for path in sorted((data_dir / "raw" / "espn_boxscores").glob("season=*.parquet"))
    )
    box = box[box["team_id"].astype(str).isin(FRANCHISES)]
    activity = box[["minutes", "pts", "fga", "reb", "ast"]].sum(axis=1)
    team_games = box.assign(activity=activity).groupby(["season", "game_id", "team_id"])
    blank = team_games["activity"].max().eq(0).groupby(level=["season", "game_id"]).any()
    blank = blank.reset_index(name="blank")
    blank["season"] = blank["season"].astype(str)
    seasons = set(blank.loc[blank["blank"], "season"])
    targets = blank[blank["blank"]]
    complete = blank[~blank["blank"] & blank["season"].isin(seasons)].copy()
    complete["key"] = complete["game_id"].astype(str).map(_sample_key)
    sample = complete.sort_values("key").groupby("season").head(per_season)
    games = pd.concat([targets, sample])[["season", "game_id", "blank"]]
    return games.sort_values(["season", "game_id"]).reset_index(drop=True)


def rebuild(data_dir: Path) -> dict[str, int]:
    """Rebuild every cached game; one Parquet file per season. Returns rows per season."""
    games_raw = pd.concat(
        pd.read_parquet(path) for path in (data_dir / "raw" / "espn_games").glob("*.parquet")
    )
    games_raw["game_id"] = games_raw["game_id"].astype(str)
    lookup = games_raw.set_index("game_id").to_dict("index")
    written = {}
    for season_dir in sorted((data_dir / "raw" / "espn_plays").glob("season=*")):
        season = season_dir.name.removeprefix("season=")
        rows = []
        for path in sorted(season_dir.glob("*.json.gz")):
            game_id = path.name.removesuffix(".json.gz")
            game = {**lookup[game_id], "game_id": game_id}
            summary = json.loads(gzip.decompress(path.read_bytes()))
            rows.extend(rebuild_boxscore(summary, game))
        if not rows:
            continue
        out = rebuilt_path(data_dir, season)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".parquet.tmp")
        pd.DataFrame(rows).to_parquet(tmp, index=False)
        tmp.replace(out)
        written[season] = len(rows)
    return written
