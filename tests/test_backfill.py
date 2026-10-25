import copy
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import requests

from nbaforecast.ingest import backfill as bf

BOARD = json.loads(
    (Path(__file__).parent / "fixtures" / "espn_scoreboard_20050115.json").read_text()
)


def board_for(season_end_year: int, calendar: list[str]) -> dict:
    """The fixture board, relabeled to another season with a given calendar."""
    board = copy.deepcopy(BOARD)
    board["leagues"][0]["calendar"] = calendar
    for i, event in enumerate(board["events"]):
        event["season"]["year"] = season_end_year
        event["id"] = f"{season_end_year}{i}"
    return board


class FakeFetch:
    """Returns the same 3-game board for every day, and records which days were asked for."""

    def __init__(self, season_end_year=2005, calendar=None, fail_times=0):
        calendar = calendar or ["2005-01-14T08:00Z", "2005-01-15T08:00Z", "2005-01-16T08:00Z"]
        self.board = board_for(season_end_year, calendar)
        self.fail_times, self.calls = fail_times, []

    def __call__(self, day):
        self.calls.append(day)
        if self.fail_times:
            self.fail_times -= 1
            raise requests.ConnectionError("simulated timeout")
        return self.board


def run(tmp_path, first, last, fetch, today=date(2026, 10, 24), **kw):
    return bf.backfill(tmp_path, first, last, fetch=fetch, today=today, pause=0, **kw)


def test_season_label():
    assert bf.season_label(2004) == "2004-05"
    assert bf.season_label(2099) == "2099-00"


@pytest.mark.parametrize(
    ("today", "expected"),
    [(date(2026, 10, 24), 2026), (date(2027, 3, 1), 2026), (date(2027, 9, 30), 2026)],
)
def test_current_season_start(today, expected):
    assert bf.current_season_start(today) == expected


def test_one_request_per_calendar_day_and_dedupes(tmp_path):
    fetch = FakeFetch()
    (result,) = run(tmp_path, 2004, 2004, fetch)
    # Anchor (Jan 15) is reused; Jan 14 and Jan 16 are fetched.
    assert sorted(fetch.calls) == [date(2005, 1, 14), date(2005, 1, 15), date(2005, 1, 16)]
    assert result.requests == 3
    # Same 3 games appear on every board but are stored once.
    assert result.games == 3
    df = pd.read_parquet(bf.raw_path(tmp_path, "2004-05"))
    assert df["game_id"].is_unique
    assert list(df["tipoff_utc"]) == sorted(df["tipoff_utc"])


def test_skips_finished_seasons_but_refreshes_current(tmp_path):
    today = date(2026, 10, 24)
    run(tmp_path, 2025, 2025, FakeFetch(2026), today=today)
    run(tmp_path, 2026, 2026, FakeFetch(2027, ["2026-10-22T07:00Z"]), today=today)

    old, cur = FakeFetch(2026), FakeFetch(2027, ["2026-10-22T07:00Z"])
    assert run(tmp_path, 2025, 2025, old, today=today)[0].status == "skipped"
    assert old.calls == []
    assert run(tmp_path, 2026, 2026, cur, today=today)[0].status == "written"


def test_current_season_ignores_future_dates(tmp_path):
    calendar = ["2026-10-22T07:00Z", "2026-10-23T07:00Z", "2026-11-30T08:00Z"]
    fetch = FakeFetch(2027, calendar)
    run(tmp_path, 2026, 2026, fetch, today=date(2026, 10, 24))
    assert date(2026, 11, 30) not in fetch.calls


def test_retries_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(bf.time, "sleep", lambda s: None)
    (result,) = run(tmp_path, 2004, 2004, FakeFetch(fail_times=2), attempts=3)
    assert result.status == "written"


def test_gives_up_after_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(bf.time, "sleep", lambda s: None)
    with pytest.raises(requests.ConnectionError):
        run(tmp_path, 2004, 2004, FakeFetch(fail_times=5), attempts=3)
    assert not bf.raw_path(tmp_path, "2004-05").exists()
