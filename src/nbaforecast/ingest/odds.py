"""Pre-game moneyline odds from The Odds API, turned into a market win probability.

One request per daily run (the free tier allows ~500 a month). Each bookmaker's two-way
moneyline is de-vigged, and the market probability is the median across bookmakers.
"""

import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import requests

from nbaforecast.metrics import devig_two_way

log = logging.getLogger(__name__)

ODDS_URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"

# ESPN display name -> The Odds API name, where they differ.
NAME_FIXES = {"LA Clippers": "Los Angeles Clippers"}

# Odds API start times are the scheduled tip-off; allow for small differences from ESPN.
MATCH_WINDOW = timedelta(hours=6)


def fetch_odds(api_key: str, session: requests.Session | None = None) -> list[dict[str, Any]]:
    http = session or requests
    resp = http.get(
        ODDS_URL,
        params={"apiKey": api_key, "regions": "us", "markets": "h2h", "oddsFormat": "decimal"},
        timeout=30,
    )
    resp.raise_for_status()
    log.info("Odds API requests remaining this month: %s", resp.headers.get("x-requests-remaining"))
    return resp.json()


def odds_name(espn_name: str) -> str:
    return NAME_FIXES.get(espn_name, espn_name)


@dataclass
class MarketLine:
    home_team: str
    away_team: str
    commence_time: datetime
    p_home: float  # median de-vigged probability across bookmakers
    books: int


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def market_lines(events: list[dict[str, Any]]) -> list[MarketLine]:
    lines = []
    for event in events:
        home, away = event["home_team"], event["away_team"]
        probs = []
        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                prices = {o["name"]: o["price"] for o in market.get("outcomes", [])}
                if home in prices and away in prices and len(prices) == 2:
                    try:
                        probs.append(devig_two_way(prices[home], prices[away])[0])
                    except ValueError:
                        log.warning("bad prices from %s for %s @ %s", book.get("key"), away, home)
        if probs:
            lines.append(
                MarketLine(
                    home,
                    away,
                    _parse_time(event["commence_time"]),
                    statistics.median(probs),
                    len(probs),
                )
            )
    return lines


def match(
    lines: list[MarketLine], home_name: str, away_name: str, tipoff: datetime
) -> MarketLine | None:
    """The market line for an ESPN game: same home and away team, start times close."""
    home, away = odds_name(home_name), odds_name(away_name)
    candidates = [
        line
        for line in lines
        if line.home_team == home
        and line.away_team == away
        and abs(line.commence_time - tipoff) <= MATCH_WINDOW
    ]
    return min(candidates, key=lambda line: abs(line.commence_time - tipoff), default=None)
