import json
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from nbaforecast.ingest import boxscores as bx

FIXTURES = Path(__file__).parent / "fixtures"
OLD = json.loads((FIXTURES / "espn_boxscore_250115004.json").read_text())  # NY @ CHI, Jan 2005
NEW = json.loads((FIXTURES / "espn_boxscore_401909868.json").read_text())  # WSH @ CHA, Oct 2026


def game_row(summary, game_id, season, day):
    sides = {c["homeAway"]: c for c in summary["header"]["competitions"][0]["competitors"]}
    return {
        "game_id": game_id,
        "season": season,
        "game_date": day,
        "home_team_id": sides["home"]["id"],
        "away_team_id": sides["away"]["id"],
        "home_score": int(sides["home"]["score"]),
        "away_score": int(sides["away"]["score"]),
    }


OLD_GAME = game_row(OLD, "250115004", "2004-05", date(2005, 1, 15))
NEW_GAME = game_row(NEW, "401909868", "2026-27", date(2026, 10, 26))


def test_points_reconcile_with_final_score():
    for summary, game in ((OLD, OLD_GAME), (NEW, NEW_GAME)):
        rows = pd.DataFrame(bx.parse_boxscore(summary, game))
        pts = rows.groupby("is_home")["pts"].sum()
        assert pts[True] == game["home_score"] and pts[False] == game["away_score"]


def test_minutes_and_starters():
    rows = pd.DataFrame(bx.parse_boxscore(NEW, NEW_GAME))
    for _, team in rows.groupby("team_id"):
        assert team["starter"].sum() == 5
        assert 238 <= team["minutes"].sum() <= 242  # 240 in regulation, rounding per player
    dnp = rows[rows["did_not_play"]]
    assert (dnp["minutes"] == 0).all() and (dnp["dnp_reason"] != "").all()


def test_shooting_splits_parsed():
    rows = pd.DataFrame(bx.parse_boxscore(NEW, NEW_GAME))
    assert (rows["fgm"] <= rows["fga"]).all() and (rows["fg3m"] <= rows["fg3a"]).all()
    played = rows[~rows["did_not_play"]]
    assert (
        (2 * (played["fgm"] - played["fg3m"]) + 3 * played["fg3m"] + played["ftm"]) == played["pts"]
    ).all()


def test_plus_minus_missing_in_old_seasons_not_zero():
    old = pd.DataFrame(bx.parse_boxscore(OLD, OLD_GAME))
    new = pd.DataFrame(bx.parse_boxscore(NEW, NEW_GAME))
    assert old["plus_minus"].isna().all()
    assert new["plus_minus"].notna().any()
    assert new.loc[new["is_home"], "plus_minus"].dropna().apply(float.is_integer).all()


def test_opponent_and_home_flags():
    rows = pd.DataFrame(bx.parse_boxscore(NEW, NEW_GAME))
    assert set(rows["is_home"]) == {True, False}
    for _, r in rows.iterrows():
        assert r["opponent_id"] != r["team_id"]


def test_backfill_season_incremental(tmp_path):
    games_dir = tmp_path / "raw" / "espn_games"
    games_dir.mkdir(parents=True)
    pd.DataFrame(
        [
            {**NEW_GAME, "completed": True, "tipoff_utc": datetime(2026, 10, 26, 23, tzinfo=UTC)},
            {**NEW_GAME, "game_id": "allstar", "home_team_id": "31", "away_team_id": "32",
             "completed": True, "tipoff_utc": datetime(2027, 2, 14, 23, tzinfo=UTC)},
            {**NEW_GAME, "game_id": "future", "completed": False,
             "tipoff_utc": datetime(2026, 10, 30, 23, tzinfo=UTC)},
        ]
    ).to_parquet(games_dir / "season=2026-27.parquet")  # fmt: skip
    calls = []

    def fetch(game_id):
        calls.append(game_id)
        return NEW

    first = bx.backfill_season(tmp_path, "2026-27", fetch, pause=0, attempts=1, backoff=0)
    assert calls == ["401909868"]  # All-Star and unplayed games skipped
    assert first.status == "written" and first.games == 1
    again = bx.backfill_season(tmp_path, "2026-27", fetch, pause=0, attempts=1, backoff=0)
    assert again.status == "skipped" and calls == ["401909868"]  # nothing refetched
    stored = pd.read_parquet(bx.raw_path(tmp_path, "2026-27"))
    assert stored["game_id"].nunique() == 1


def test_missing_summary_counted_not_fatal(tmp_path):
    import requests

    games_dir = tmp_path / "raw" / "espn_games"
    games_dir.mkdir(parents=True)
    tip = datetime(2026, 10, 26, 23, tzinfo=UTC)
    pd.DataFrame(
        [
            {**NEW_GAME, "game_id": "gone", "completed": True, "tipoff_utc": tip},
            {**NEW_GAME, "completed": True, "tipoff_utc": tip},
        ]
    ).to_parquet(games_dir / "season=2026-27.parquet")

    def fetch(game_id):
        if game_id == "gone":
            resp = requests.Response()
            resp.status_code = 404
            raise requests.HTTPError("404 Client Error", response=resp)
        return NEW

    result = bx.backfill_season(tmp_path, "2026-27", fetch, pause=0, attempts=3, backoff=0)
    assert result.status == "written" and result.games == 1 and result.missing == 1


@pytest.mark.parametrize(("text", "value"), [("+6", 6), ("-17", -17), ("0", 0), ("--", 0), ("", 0)])
def test_int_parsing(text, value):
    assert bx._int(text) == value


def test_player_without_id_uses_link_or_is_skipped():
    import copy

    summary = copy.deepcopy(NEW)
    athletes = summary["boxscore"]["players"][0]["statistics"][0]["athletes"]
    first_id = athletes[0]["athlete"].pop("id")
    athletes[0]["athlete"]["links"] = [
        {"href": f"https://www.espn.com/nba/player/_/id/{first_id}/x"}
    ]
    athletes[1]["athlete"] = {"links": [], "shortName": "Olbrich"}  # no id anywhere
    athletes[2]["athlete"] = {"links": []}  # no id and no name
    rows = bx.parse_boxscore(summary, NEW_GAME)
    assert any(r["player_id"] == str(first_id) for r in rows)
    team_id = summary["boxscore"]["players"][0]["team"]["id"]
    stand_in = [r for r in rows if r["player_id"] == f"noid-{team_id}-olbrich"]
    assert len(stand_in) == 1 and stand_in[0]["player"] == "Olbrich"
    assert len(rows) == len(bx.parse_boxscore(NEW, NEW_GAME)) - 1  # only the nameless row dropped
