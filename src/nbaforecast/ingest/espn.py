"""ESPN's public scoreboard JSON: the source for both the historical backfill and daily results.

stats.nba.com and cdn.nba.com reject non-browser clients, so we don't use them. ESPN's
scoreboard needs no key and returns every game on a date, with stable franchise IDs, tip-off
time, period-by-period scores, and a neutral-site flag.
"""

import logging
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
USER_AGENT = "nba-forecast (+https://github.com/garettprestholdt/nba-forecast)"

# ESPN season type -> our label. 1 = preseason and 4 = All-Star are skipped.
SEASON_TYPES = {2: "regular_season", 3: "playoffs", 5: "playin"}

# NBA game dates are reported in US Eastern time: a 7pm ET tip-off is 00:00 UTC the next day.
EASTERN = ZoneInfo("America/New_York")


def fetch_scoreboard(day: date, session: requests.Session | None = None) -> dict[str, Any]:
    http = session or requests
    resp = http.get(
        SCOREBOARD_URL,
        params={"dates": day.strftime("%Y%m%d"), "limit": 100},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def season_label_from_espn_year(year: int) -> str:
    """ESPN names seasons by end year: 2005 -> '2004-05'."""
    return f"{year - 1}-{year % 100:02d}"


def season_calendar(scoreboard: dict[str, Any]) -> list[date]:
    """Every date with games in the scoreboard's season (preseason through the Finals)."""
    league = scoreboard["leagues"][0]
    if league.get("calendarType") != "day":
        raise ValueError(f"unexpected calendarType {league.get('calendarType')!r}")
    # Entries look like '2004-10-10T07:00Z': midnight US Pacific, so the date part is the day.
    return sorted({date.fromisoformat(entry[:10]) for entry in league["calendar"]})


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _score(competitor: dict[str, Any]) -> int | None:
    score = competitor.get("score")
    if isinstance(score, dict):  # some endpoints nest it
        score = score.get("value")
    return int(float(score)) if score not in (None, "") else None


def parse_games(scoreboard: dict[str, Any], fetched_at: datetime) -> list[dict[str, Any]]:
    """One row per game. Games still scheduled or in progress have null scores."""
    rows = []
    for event in scoreboard.get("events", []):
        if "season" not in event or not event.get("competitions"):
            # ESPN occasionally returns an empty `{}` event (seen on a 2007 preseason date).
            # If a real game were ever dropped this way, dbt's game-count tests would catch it.
            log.warning("skipping malformed ESPN event: %.200s", event)
            continue
        season_type = SEASON_TYPES.get(event["season"]["type"])
        if season_type is None:
            continue
        comp = event["competitions"][0]
        sides = {c["homeAway"]: c for c in comp["competitors"]}
        if set(sides) != {"home", "away"}:
            raise ValueError(f"game {event['id']}: expected home and away, got {sorted(sides)}")
        home, away = sides["home"], sides["away"]

        completed = bool(event["status"]["type"]["completed"])
        tipoff = _parse_time(comp.get("startDate") or event["date"])
        periods = max(len(home.get("linescores", [])), len(away.get("linescores", [])))
        rows.append(
            {
                "game_id": str(event["id"]),
                "season": season_label_from_espn_year(event["season"]["year"]),
                "season_type": season_type,
                "tipoff_utc": tipoff,
                "game_date": tipoff.astimezone(EASTERN).date(),
                "status": event["status"]["type"]["name"],
                "completed": completed,
                "neutral_site": bool(comp.get("neutralSite", False)),
                "venue": (comp.get("venue") or {}).get("fullName"),
                "home_team_id": str(home["team"]["id"]),
                "home_team_abbr": home["team"]["abbreviation"],
                "home_team_name": home["team"]["displayName"],
                "away_team_id": str(away["team"]["id"]),
                "away_team_abbr": away["team"]["abbreviation"],
                "away_team_name": away["team"]["displayName"],
                "home_score": _score(home) if completed else None,
                "away_score": _score(away) if completed else None,
                "periods": periods if completed else None,
                "fetched_at": fetched_at,
            }
        )
    return rows
