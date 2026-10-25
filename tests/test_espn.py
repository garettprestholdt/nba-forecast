import copy
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from nbaforecast.ingest import espn

FIXTURES = Path(__file__).parent / "fixtures"
FETCHED = datetime(2026, 10, 24, tzinfo=UTC)


def load(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def regular():
    return load("espn_scoreboard_20050115.json")


@pytest.fixture
def playin():
    return load("espn_scoreboard_20210521_playin.json")


def test_parse_regular_season(regular):
    games = espn.parse_games(regular, FETCHED)
    assert len(games) == 3
    g = games[0]
    assert g["season"] == "2004-05"
    assert g["season_type"] == "regular_season"
    assert (g["home_team_abbr"], g["away_team_abbr"]) == ("CHI", "NY")
    assert (g["home_score"], g["away_score"]) == (86, 84)
    assert g["periods"] == 4
    assert g["completed"] is True
    assert isinstance(g["game_id"], str) and isinstance(g["home_team_id"], str)


def test_late_tipoff_uses_eastern_date(regular):
    # POR @ MIN tipped at 00:00 UTC on Jan 16, which is 7pm ET on Jan 15.
    late = espn.parse_games(regular, FETCHED)[2]
    assert late["tipoff_utc"] == datetime(2005, 1, 16, 0, 0, tzinfo=UTC)
    assert late["game_date"] == date(2005, 1, 15)


def test_parse_playin_with_overtime(playin):
    (g,) = espn.parse_games(playin, FETCHED)
    assert g["season"] == "2020-21"
    assert g["season_type"] == "playin"
    assert g["periods"] == 5  # one overtime
    assert g["away_score"] > g["home_score"]  # MEM won at GS


def test_skips_preseason_and_all_star(regular):
    board = copy.deepcopy(regular)
    board["events"][0]["season"]["type"] = 1
    board["events"][1]["season"]["type"] = 4
    assert len(espn.parse_games(board, FETCHED)) == 1


def test_unfinished_game_has_null_scores(regular):
    board = copy.deepcopy(regular)
    board["events"][0]["status"]["type"].update(completed=False, name="STATUS_SCHEDULED")
    g = espn.parse_games(board, FETCHED)[0]
    assert g["completed"] is False
    assert g["home_score"] is None and g["periods"] is None


def test_skips_empty_event(regular, caplog):
    board = copy.deepcopy(regular)
    board["events"].insert(0, {})
    assert len(espn.parse_games(board, FETCHED)) == 3
    assert "malformed" in caplog.text


def test_rejects_game_without_both_sides(regular):
    board = copy.deepcopy(regular)
    board["events"][0]["competitions"][0]["competitors"][1]["homeAway"] = "home"
    with pytest.raises(ValueError, match="expected home and away"):
        espn.parse_games(board, FETCHED)


def test_season_calendar(regular):
    days = espn.season_calendar(regular)
    assert days[0] == date(2004, 10, 10)
    assert days == sorted(days)


def test_season_label_from_espn_year():
    assert espn.season_label_from_espn_year(2005) == "2004-05"
    assert espn.season_label_from_espn_year(2000) == "1999-00"
