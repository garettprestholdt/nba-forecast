"""ESPN's NBA injury list, and box-score participation (who actually played) for scoring.

NBA's official injury report PDF rejects non-browser clients, so ESPN's injury JSON is the
source. Each entry has a status (Out, Day-To-Day, ...) and a free-text note; the notes are what
the availability agent reads.
"""

import re
from datetime import datetime
from typing import Any

import requests

from nbaforecast.ingest.espn import USER_AGENT

INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"


def _get(url: str, params: dict | None = None, session: requests.Session | None = None) -> dict:
    http = session or requests
    resp = http.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_injuries(session: requests.Session | None = None) -> dict[str, Any]:
    return _get(INJURIES_URL, session=session)


def fetch_summary(game_id: str, session: requests.Session | None = None) -> dict[str, Any]:
    return _get(SUMMARY_URL, params={"event": game_id}, session=session)


def _athlete_id(athlete: dict) -> str | None:
    if athlete.get("id"):
        return str(athlete["id"])
    for link in athlete.get("links", []):
        m = re.search(r"/id/(\d+)", link.get("href", ""))
        if m:
            return m.group(1)
    return None


def parse_injuries(data: dict[str, Any], fetched_at: datetime) -> list[dict[str, Any]]:
    """One row per injured player."""
    rows = []
    for team in data.get("injuries", []):
        for entry in team.get("injuries", []):
            athlete = entry.get("athlete", {})
            player_id = _athlete_id(athlete)
            if player_id is None:
                continue
            details = entry.get("details") or {}
            rows.append(
                {
                    "player_id": player_id,
                    "player": athlete.get("displayName", ""),
                    "position": (athlete.get("position") or {}).get("abbreviation") or "",
                    "team_id": str(team["id"]),
                    "team": team.get("displayName", ""),
                    "status": entry.get("status", ""),
                    "injury": details.get("type") or "",
                    "return_date": details.get("returnDate") or "",
                    "note": (entry.get("shortComment") or "").strip(),
                    "note_detail": (entry.get("longComment") or "").strip(),
                    "note_updated": entry.get("date") or "",
                    "fetched_at": fetched_at.isoformat(timespec="seconds"),
                }
            )
    return rows


def parse_participation(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """player_id -> {played, minutes, reason} from a final box score.

    A player who dressed but didn't play has `didNotPlay` and a reason ("COACH'S DECISION",
    an injury, ...). Players missing from the box score entirely were inactive.
    """
    out = {}
    for team in summary.get("boxscore", {}).get("players", []):
        for block in team.get("statistics", [])[:1]:
            names = block.get("names", [])
            minutes_at = names.index("MIN") if "MIN" in names else 0
            for a in block.get("athletes", []):
                stats = a.get("stats") or []
                raw = stats[minutes_at] if len(stats) > minutes_at else ""
                minutes = int(raw) if str(raw).isdigit() else 0
                played = not a.get("didNotPlay", False) and minutes > 0
                out[str(a["athlete"]["id"])] = {
                    "team_id": str(team["team"]["id"]),
                    "played": played,
                    "minutes": minutes,
                    "reason": a.get("reason") or "",
                }
    return out
