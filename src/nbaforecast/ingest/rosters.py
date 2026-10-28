"""Current NBA rosters from ESPN, for the site's player cards: one file on the data branch,
`players/directory.json`, rewritten each morning (its git history keeps the old versions).

Each player: name, position, jersey number, team, and ESPN's current injury status ("Out",
"Day-To-Day", or blank). Headshots aren't stored: ESPN serves them at a URL built from the
player id (see web.headshot_url).
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from nbaforecast.ingest.espn import USER_AGENT

ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{}/roster"


def directory_path(root: Path) -> Path:
    return Path(root) / "players" / "directory.json"


def parse_roster(data: dict[str, Any], team_id: str) -> dict[str, dict[str, str]]:
    players = {}
    for a in data.get("athletes", []):
        injuries = a.get("injuries") or []
        players[str(a["id"])] = {
            "name": a.get("displayName", ""),
            "short": a.get("shortName", a.get("displayName", "")),
            "position": (a.get("position") or {}).get("abbreviation", ""),
            "jersey": a.get("jersey", ""),
            "team_id": team_id,
            "status": injuries[0].get("status", "") if injuries else "",
        }
    return players


def fetch_directory(teams: list[str], session: requests.Session) -> dict[str, dict[str, str]]:
    players: dict[str, dict[str, str]] = {}
    for team in teams:
        resp = session.get(ROSTER_URL.format(team), headers={"User-Agent": USER_AGENT}, timeout=30)
        resp.raise_for_status()
        players.update(parse_roster(resp.json(), team))
    return players


def write_directory(root: Path, players: dict[str, dict[str, str]], now: datetime) -> Path:
    path = directory_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": now.isoformat(timespec="seconds"), "players": players}
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    return path


def load_directory(root: Path) -> dict[str, dict[str, str]]:
    path = directory_path(root)
    return json.loads(path.read_text())["players"] if path.exists() else {}


def run(root: Path, teams: list[str], now: datetime | None = None) -> int:
    players = fetch_directory(teams, requests.Session())
    write_directory(root, players, now or datetime.now(UTC))
    return len(players)
