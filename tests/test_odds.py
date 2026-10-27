import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nbaforecast.ingest import odds
from nbaforecast.metrics import devig_two_way

EVENTS = json.loads((Path(__file__).parent / "fixtures" / "odds_api_nba_h2h.json").read_text())


def test_market_lines_median_of_devigged_books():
    lines = {line.home_team: line for line in odds.market_lines(EVENTS)}
    bos = lines["Boston Celtics"]
    expected = sorted(devig_two_way(h, a)[0] for h, a in [(1.5, 2.6), (1.48, 2.7), (1.52, 2.55)])[1]
    assert bos.p_home == pytest.approx(expected)
    assert bos.books == 3
    assert 0.6 < bos.p_home < 0.7


def test_events_without_bookmakers_are_dropped():
    assert "Miami Heat" not in {line.home_team for line in odds.market_lines(EVENTS)}


def test_match_handles_espn_name_differences():
    lines = odds.market_lines(EVENTS)
    tipoff = datetime(2026, 10, 28, 2, 30, tzinfo=UTC)
    line = odds.match(lines, "LA Clippers", "Denver Nuggets", tipoff)
    assert line is not None and line.p_home < 0.5


def test_match_requires_same_home_away_and_close_time():
    lines = odds.market_lines(EVENTS)
    tipoff = datetime(2026, 10, 27, 23, 30, tzinfo=UTC)
    assert odds.match(lines, "Boston Celtics", "New York Knicks", tipoff) is not None
    assert odds.match(lines, "New York Knicks", "Boston Celtics", tipoff) is None  # swapped
    next_week = datetime(2026, 11, 3, 23, 30, tzinfo=UTC)
    assert odds.match(lines, "Boston Celtics", "New York Knicks", next_week) is None
