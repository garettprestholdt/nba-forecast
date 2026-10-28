"""Player heights from ESPN's athlete endpoint, for players no longer on a current roster.

Used locally by `nbaf coldcase-bank`. Answers are cached in
`<data_dir>/raw/espn_athletes.json` so each player is looked up once.
"""

import json
import re
from pathlib import Path
from typing import Any

import requests

from nbaforecast.ingest.espn import USER_AGENT

ATHLETE_URL = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{}"


def cache_path(data_dir: Path) -> Path:
    return Path(data_dir) / "raw" / "espn_athletes.json"


def parse_height(athlete: dict[str, Any]) -> tuple[str, int | None]:
    """(display height like 6' 8", inches) from an athlete payload."""
    shown = athlete.get("displayHeight") or ""
    m = re.match(r"(\d+)' (\d+)", shown)
    return shown, (int(m.group(1)) * 12 + int(m.group(2))) if m else None


class Heights:
    def __init__(self, data_dir: Path, session: requests.Session | None = None):
        self.path = cache_path(data_dir)
        self.known: dict[str, list] = (
            json.loads(self.path.read_text()) if self.path.exists() else {}
        )
        self.session = session or requests.Session()

    def __call__(self, player_id: str) -> tuple[str, int | None]:
        if player_id not in self.known:
            try:
                resp = self.session.get(ATHLETE_URL.format(player_id),
                                        headers={"User-Agent": USER_AGENT}, timeout=30)  # fmt: skip
                resp.raise_for_status()
                self.known[player_id] = list(parse_height(resp.json().get("athlete", {})))
            except requests.RequestException:
                return "", None
        shown, inches = self.known[player_id]
        return shown, inches

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.known, sort_keys=True))
