"""Where each game is played, from ESPN's scoreboard: `schedule/venues.json` on the data branch.

The daily job looks up today and the next two days and merges them in, so the game pages can
say "State Farm Arena, Atlanta, GA" (or flag a neutral site) without the site calling ESPN.
"""

import json
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any


def path(root: Path) -> Path:
    return Path(root) / "schedule" / "venues.json"


def parse(scoreboard: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for event in scoreboard.get("events", []):
        for comp in event.get("competitions", [])[:1]:
            venue = comp.get("venue") or {}
            if not venue.get("fullName"):
                continue
            address = venue.get("address") or {}
            home = next((c for c in comp.get("competitors", []) if c.get("homeAway") == "home"), {})
            out[str(event["id"])] = {
                "home_team_id": str((home.get("team") or {}).get("id", home.get("id", ""))),
                "arena": venue["fullName"],
                "city": address.get("city", ""),
                "state": address.get("state", ""),
                "country": address.get("country", ""),
                "neutral": bool(comp.get("neutralSite", False)),
            }
    return out


def load(root: Path) -> dict[str, dict[str, Any]]:
    p = path(root)
    return json.loads(p.read_text()) if p.exists() else {}


def run(root: Path, today: date, fetch: Callable[[date], dict], days: int = 3) -> int:
    """Merge venues for `days` days starting today. Returns how many games were looked up."""
    known = load(root)
    found = {}
    for i in range(days):
        found.update(parse(fetch(today + timedelta(days=i))))
    known.update(found)
    p = path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(known, indent=1, sort_keys=True) + "\n")
    return len(found)
