import json
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from nbaforecast import court, impact, live
from nbaforecast.ingest import venues
from nbaforecast.web import pages

FIXTURES = Path(__file__).parent / "fixtures"
SUMMARY = json.loads((FIXTURES / "espn_summary_plays_401909868.json").read_text())  # WSH @ CHA
SIDES = {c["homeAway"]: c for c in SUMMARY["header"]["competitions"][0]["competitors"]}
GAME = {"game_id": "401909868", "season": "2026-27", "game_date": "2026-10-26",
        "home_team_id": SIDES["home"]["id"], "away_team_id": SIDES["away"]["id"]}  # fmt: skip


@pytest.mark.parametrize(
    ("x", "y", "three", "expected"),
    [
        (25, 2, False, "inside"),  # at the rim
        (25, -3, False, "inside"),  # behind the basket
        (25, 18, False, "mid_center"),  # free-throw line extended
        (10, 8, False, "mid_left"),  # left baseline jumper
        (42, 10, False, "mid_right"),
        (1, 3, True, "corner_left"),
        (49, 0, True, "corner_right"),
        (25, 26, True, "arc_center"),
        (5, 15, True, "arc_left"),  # left wing, above the break
        (45, 15, True, "arc_right"),
        (-214748340, -214748365, False, None),  # ESPN's "no location" sentinel
    ],
)
def test_zones(x, y, three, expected):
    assert court.zone(x, y, three) == expected


def test_shots_match_the_box_score():
    rows = court.parse_shots(SUMMARY, GAME)
    assert {r["zone"] for r in rows} <= set(court.ZONES)
    box = {}
    for team in SUMMARY["boxscore"]["players"]:
        s = team["statistics"][0]
        i = s["names"].index("FG")
        box[team["team"]["id"]] = sum(int(a["stats"][i].split("-")[1])
                                      for a in s["athletes"] if a.get("stats"))  # fmt: skip
    for team, fga in box.items():
        got = sum(r["fga"] for r in rows if r["team_id"] == team)
        assert abs(got - fga) <= 2, (team, got, fga)  # a shot or two can lack a location
    assert all(r["opponent_id"] != r["team_id"] for r in rows)


def test_stints_add_up_to_the_final_score():
    stints = court.parse_stints(SUMMARY, GAME)
    for side in ("home", "away"):
        tid = SIDES[side]["id"]
        mine = [s for s in stints if s["offense_id"] == tid]
        assert sum(s["pts"] for s in mine) == int(SIDES[side]["score"])
        assert 85 < sum(s["poss"] for s in mine) < 110
        assert all(s["home"] == int(side == "home") for s in mine)
        assert all(len(s["offense"].split(";")) == 5 == len(s["defense"].split(";")) for s in mine)


def test_sync_is_idempotent_and_verified(tmp_path):
    store = live.Store(tmp_path)
    result = {**dict.fromkeys(live.RESULT_FIELDS, ""), **GAME, "season_type": "regular_season",
              "tipoff_utc": "2026-10-26T23:00:00+00:00", "home_score": SIDES["home"]["score"],
              "away_score": SIDES["away"]["score"]}  # fmt: skip
    store.append(store.results_path("2026-27"), live.RESULT_FIELDS, [result])
    calls = []
    first = court.sync(store, "2026-27", lambda g: calls.append(g) or SUMMARY, date(2026, 10, 26))
    again = court.sync(store, "2026-27", lambda g: calls.append(g) or SUMMARY, date(2026, 10, 26))
    assert first["games_added"] == 1 and again["games_added"] == 0 and calls == ["401909868"]
    assert live.verify(tmp_path, lambda rel: None) == []
    rows = live.Store.read(court.shots_path(tmp_path, "2026-27"))
    live.Store.append(court.shots_path(tmp_path, "2026-27"), court.SHOT_FIELDS, rows[:1])
    assert any("shots" in p and "duplicate" in p for p in live.verify(tmp_path, lambda rel: None))


def synthetic_stints(rng, n=4000):
    """Twelve players per side of a fake league; player d0 is a great defender (-6 per 100)
    and o0 a great scorer (+6)."""
    offense = [f"o{i}" for i in range(12)]
    defense = [f"d{i}" for i in range(12)]
    rows = []
    for _ in range(n):
        o = list(rng.choice(offense, 5, replace=False))
        d = list(rng.choice(defense, 5, replace=False))
        poss = 10.0
        rate = 112 + 6 * ("o0" in o) - 6 * ("d0" in d)
        pts = rng.poisson(rate / 100 * poss)
        rows.append({"offense": ";".join(o), "defense": ";".join(d), "pts": str(pts),
                     "poss": str(poss), "home": "0"})  # fmt: skip
    return rows


def test_rapm_finds_the_planted_players():
    rows = synthetic_stints(np.random.default_rng(7))
    per36 = {"d0": {"blk": 2.5, "stl": 1.5, "reb": 9.0}}
    out = impact.fit(rows, per36)["players"]
    assert max(out, key=lambda p: out[p]["d"]) == "d0" and out["d0"]["d"] > 2
    assert max(out, key=lambda p: out[p]["o"]) == "o0" and out["o0"]["o"] > 2
    o, d = impact.blend(out["d0"])
    assert d > 2 and abs(o) < 1  # d0 only ever played defense


def test_defense_tags_need_the_impact_rating_to_agree():
    # a 6'8" star with plenty of steals but an average defensive rating: no "stopper" tag
    assert pages.defense_tags(80, blk36=0.6, stl36=1.6, d=-0.4) == []
    assert pages.defense_tags(80, blk36=0.6, stl36=1.6, d=1.5) == ["Wing stopper"]
    assert pages.defense_tags(85, blk36=3.0, stl36=1.0, d=5.0) == ["Rim protector"]
    assert pages.defense_tags(None, 3.0, 2.0, 3.0) == []


def test_venues_parse_and_merge(tmp_path):
    board = json.loads((FIXTURES / "espn_scoreboard_20210521_playin.json").read_text())
    parsed = venues.parse(board)
    gid = str(board["events"][0]["id"])
    assert parsed[gid]["arena"] == "Chase Center" and parsed[gid]["neutral"] is False
    assert parsed[gid]["home_team_id"] == "9"
    venues.run(tmp_path, date(2021, 5, 21), lambda d: board, days=2)
    assert venues.load(tmp_path)[gid]["arena"] == "Chase Center"


def test_previous_season():
    assert court.previous_season("2026-27") == "2025-26"
    assert court.previous_season("2000-01") == "1999-00"
