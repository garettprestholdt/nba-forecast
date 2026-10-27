import gzip
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import requests

from nbaforecast.ingest import boxscores as bx
from nbaforecast.ingest import playbyplay as pbp

FIXTURES = Path(__file__).parent / "fixtures"


def load(game_id: str) -> dict:
    return json.loads(gzip.decompress((FIXTURES / f"espn_plays_{game_id}.json.gz").read_bytes()))


# MIL @ DET, Nov 2013: ESPN has the full box score, so the rebuild can be checked against it.
FULL = load("400489077")
FULL_GAME = {
    "game_id": "400489077", "season": "2013-14", "game_date": date(2013, 11, 25),
    "home_team_id": "8", "away_team_id": "15", "home_score": 113, "away_score": 94,
}  # fmt: skip
# LAC @ NO, Dec 2016: ESPN's box score lists the players but every stat is blank.
BLANK = load("400899923")
BLANK_GAME = {
    "game_id": "400899923", "season": "2016-17", "game_date": date(2016, 12, 28),
    "home_team_id": "3", "away_team_id": "12", "home_score": 102, "away_score": 98,
}  # fmt: skip


def frame(rows) -> pd.DataFrame:
    return pd.DataFrame(rows).set_index("player_id")


def test_rebuild_matches_real_box_score_exactly():
    real = frame(bx.parse_boxscore(FULL, FULL_GAME))
    rebuilt = frame(pbp.rebuild_boxscore(FULL, FULL_GAME))
    assert rebuilt.index.equals(real.index)
    for col in bx.STAT_COLUMNS + ["plus_minus"]:
        assert (rebuilt[col] == real[col]).all(), col
    assert rebuilt["lineup_consistent"].all()


def test_blank_box_score_rebuilt_to_final_score():
    blank = frame(bx.parse_boxscore(BLANK, BLANK_GAME))
    assert blank[["minutes", "pts", "fga"]].sum().sum() == 0  # what ESPN gives us
    rebuilt = pd.DataFrame(pbp.rebuild_boxscore(BLANK, BLANK_GAME))
    pts = rebuilt.groupby("is_home")["pts"].sum()
    assert pts[True] == 102 and pts[False] == 98
    for _, team in rebuilt.groupby("team_id"):
        assert 238 <= team["minutes"].sum() <= 242
        assert team.loc[team["did_not_play"], "minutes"].eq(0).all()
    # Lineups stay consistent, so plus-minus is kept and sums to five times the margin.
    assert rebuilt["lineup_consistent"].all()
    assert rebuilt.groupby("is_home")["plus_minus"].sum()[True] == 5 * (102 - 98)


@pytest.mark.parametrize(
    ("text", "seconds"), [("11:45", 705), ("0:07", 7), ("45.2", 45.2), ("", 0)]
)
def test_clock_seconds(text, seconds):
    assert pbp.clock_seconds(text) == pytest.approx(seconds)


def play(
    type_, text="", *, shooting=False, attempted=0, scoring=False, players=("1",), clock="5:00"
):
    return {
        "type": type_, "text": text, "period": 1, "clock": clock, "scoring": scoring,
        "shooting": shooting, "attempted": attempted, "team": "", "players": list(players),
        "home": None, "away": None,
    }  # fmt: skip


@pytest.mark.parametrize(
    ("p", "kind"),
    [
        (play("Jump Shot", shooting=True, attempted=2), "fg"),
        (play("Jump Shot", "x misses 16-foot jumper", shooting=True), "fg"),  # attempted = 0
        (play("Free Throw - 1 of 2", shooting=True, attempted=1), "ft"),
        (play("Shooting Block"), "foul"),  # a blocking foul, not a blocked shot
        (play("Offensive Charge"), "foul"),
        (play("Technical Foul"), "other"),
        (play("Defensive 3-Seconds Technical"), "other"),
        (play("Offensive Foul Turnover"), "tov"),
        (play("Traveling"), "tov"),
        (play("Substitution"), "sub"),
    ],
)
def test_play_kinds(p, kind):
    assert pbp._kind(p) == kind


def test_tally_assists_blocks_steals_and_free_throws():
    plays = [
        play("Jump Shot", "a makes three point jumper (b assists)", shooting=True, attempted=3,
             scoring=True, players=("a", "b")),
        play("Layup Shot", "c blocks a's layup", shooting=True, attempted=2, players=("a", "c")),
        play("Bad Pass\nTurnover", "a bad pass (c steals)", players=("a", "c")),
        play("Free Throw - 1 of 2", "a makes free throw", shooting=True, scoring=True),
        play("Double Personal Foul", "a double personal foul", players=("a", "c")),
        play("Defensive Rebound", "team rebound", players=()),
    ]  # fmt: skip
    for p in plays[3:4]:
        p["players"] = ["a"]
    stats = pbp._tally(plays)
    assert stats["a"]["pts"] == 4 and stats["a"]["fga"] == 2 and stats["a"]["fg3m"] == 1
    assert stats["a"]["ftm"] == 1 and stats["a"]["tov"] == 1 and stats["a"]["pf"] == 1
    assert stats["b"]["ast"] == 1
    assert stats["c"]["blk"] == 1 and stats["c"]["stl"] == 1 and stats["c"]["pf"] == 1


def test_free_throws_credited_before_substitutions_at_same_time():
    plays = [
        play("Shooting Foul", clock="5:00"),
        play("Free Throw - 1 of 2", clock="5:00"),
        play("Substitution", clock="5:00"),
        play("Free Throw - 2 of 2", clock="5:00"),
        play("Jump Shot", clock="4:40"),
    ]
    order = [p["type"] for p in pbp._subs_last(plays)]
    assert order.index("Substitution") == 3 and order[-1] == "Jump Shot"


def test_fetch_plays_caches_and_counts_404(tmp_path):
    calls = []

    def fetch(game_id):
        calls.append(game_id)
        if game_id == "gone":
            resp = requests.Response()
            resp.status_code = 404
            raise requests.HTTPError("404", response=resp)
        return {"plays": [{"type": {"text": "Jump Ball"}}], "boxscore": {"players": []}}

    games = pd.DataFrame({"season": ["2016-17", "2016-17"], "game_id": ["1", "gone"]})
    assert pbp.fetch_plays(tmp_path, games, fetch, pause=0, attempts=1) == (1, 1)
    assert (pbp.plays_dir(tmp_path, "2016-17") / "1.json.gz").exists()
    pbp.fetch_plays(tmp_path, games, fetch, pause=0, attempts=1)
    assert calls == ["1", "gone", "gone"]  # cached games are not fetched again


def test_select_games_blank_plus_stable_sample(tmp_path):
    rows = []
    for i in range(6):
        for team in ("1", "2"):
            blank = i == 0 and team == "1"
            rows.append({"season": "2016-17", "game_id": str(100 + i), "team_id": team,
                         "minutes": 0 if blank else 30, "pts": 0 if blank else 10,
                         "fga": 0 if blank else 8, "reb": 0, "ast": 0})  # fmt: skip
    rows.append({**rows[-1], "season": "2018-19", "game_id": "900"})  # season with no blanks
    out = tmp_path / "raw" / "espn_boxscores"
    out.mkdir(parents=True)
    pd.DataFrame(rows).to_parquet(out / "season=2016-17.parquet")
    games = pbp.select_games(tmp_path, per_season=2)
    assert list(games.loc[games["blank"], "game_id"]) == ["100"]
    assert len(games) == 3 and set(games["season"]) == {"2016-17"}
    assert games.equals(pbp.select_games(tmp_path, per_season=2))


def test_split_substitution_sides():
    entering = play("Substitution", "Brook Lopez enters the game for ", players=("3448",))
    leaving = play("Substitution", " enters the game for Isaiah Thomas", players=("6472",))
    whole = play("Substitution", "A enters the game for B", players=("a", "b"))
    assert pbp._sub_sides(entering) == ("3448", None)
    assert pbp._sub_sides(leaving) == (None, "6472")
    assert pbp._sub_sides(whole) == ("a", "b")
