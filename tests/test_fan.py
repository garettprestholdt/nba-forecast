import json
from datetime import date

import pytest

from nbaforecast.ingest import rosters
from nbaforecast.models.hybrid import FormState
from nbaforecast.models.players import PlayerParams, PlayerState
from nbaforecast.pipeline import live, projections, shadow
from nbaforecast.web import fan
from nbaforecast.web.filters import _odds

TEAMS = projections.franchises()


@pytest.mark.parametrize("team_id", sorted(TEAMS))
@pytest.mark.parametrize("mode", ["light", "dark"])
def test_every_team_color_stands_out_on_the_card(team_id, mode):
    color = fan.display_color(TEAMS[team_id], mode)
    assert color == fan.NEUTRAL or fan._contrast(color, fan.SURFACES[mode]) >= fan.MIN_CONTRAST


def test_black_teams_switch_color_in_dark_mode():
    nets = next(t for t in TEAMS.values() if t["abbr"] == "BKN")
    assert fan.display_color(nets, "light") == nets["color"]
    assert fan.display_color(nets, "dark") != nets["color"]


def test_similar_colors_are_told_apart():
    for home in TEAMS.values():
        for away in TEAMS.values():
            if home is away:
                continue
            for mode in ("light", "dark"):
                h, a = fan.matchup_colors(home, away, mode)
                assert fan._distance(h, a) >= 0.35 or a == fan.NEUTRAL, (home, away, mode)


@pytest.mark.parametrize(
    ("p", "text"), [(0.0, "<1%"), (0.003, "<1%"), (0.2, "20%"), (0.998, ">99%"), (1.0, "100%")]
)
def test_odds_never_claim_impossible(p, text):
    assert _odds(p) == text


def test_roster_parsing():
    data = {"athletes": [
        {"id": 1, "displayName": "Jayson Tatum", "shortName": "J. Tatum", "jersey": "0",
         "position": {"abbreviation": "F"}, "injuries": [{"status": "Out"}],
         "dateOfBirth": "1998-03-03T08:00Z", "height": 80.0, "weight": 210.0},
        {"id": 2, "displayName": "Derrick White", "position": {"abbreviation": "G"}},
    ]}  # fmt: skip
    players = rosters.parse_roster(data, "2")
    assert players["1"] == {"name": "Jayson Tatum", "short": "J. Tatum", "position": "F",
                            "jersey": "0", "team_id": "2", "status": "Out", "dob": "1998-03-03",
                            "height_in": "80", "weight_lb": "210"}  # fmt: skip
    assert players["2"]["status"] == "" and players["2"]["short"] == "Derrick White"
    assert players["2"]["dob"] == players["2"]["height_in"] == ""


def test_lineups_key_players_and_absences(tmp_path):
    state = PlayerState(PlayerParams())
    state.season = "2026-27"
    for pid, minutes, gs in (("a", 34, 25.0), ("b", 30, 12.0), ("c", 28, 9.0), ("d", 25, 8.0),
                             ("e", 22, 6.0), ("f", 12, 3.0), ("g", 33, 20.0)):  # fmt: skip
        row = type("R", (), {"player_id": pid, "stats_missing": False, "played": True,
                             "minutes": minutes, "game_score": gs, "plus_minus": 0})  # fmt: skip
        state.record("2", [row])
    shadow.save_state(tmp_path, shadow.ShadowState(state, FormState(), date(2026, 10, 26)))
    directory = {
        pid: {"name": f"Player {pid.upper()}", "short": pid.upper(), "position": "G",
              "jersey": "", "team_id": "2", "status": ""}
        for pid in "abcdefg"
    }  # fmt: skip
    directory["g"]["status"] = "Out"
    directory["b"]["status"] = "Day-To-Day"
    rosters.write_directory(tmp_path, directory, __import__("datetime").datetime.now())
    lineup = fan.lineups(tmp_path, {"2"})["2"]
    assert [p.player_id for p in lineup.key][0] == "a"  # best projected score first
    assert {p.player_id for p in lineup.key} == {"a", "b", "c", "d", "e"}  # top 5 by minutes
    assert [p.player_id for p in lineup.out] == ["g"]
    assert next(p for p in lineup.key if p.player_id == "b").status == "Day-To-Day"
    assert all(p.headshot is None or "headshots" in p.headshot for p in lineup.key)


def test_no_lineups_without_state_or_directory(tmp_path):
    assert fan.lineups(tmp_path, {"2"}) == {}


def test_images_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(fan, "USE_ESPN_IMAGES", False)
    assert fan.logo_url("bos") is None and fan.headshot_url("123") is None
    t = fan.teams(live.Store("/nonexistent").root, {}, 1505.0)["2"]
    assert t.logo is None and t.rating is None and json.dumps(t.record) == '"0-0"'
