import json
from datetime import UTC, date, datetime

from nbaforecast import impact, live, shadow, shadow_v2
from nbaforecast.ingest import rosters
from nbaforecast.models.elo import EloState
from nbaforecast.models.hybrid import FormState
from nbaforecast.models.players import PlayerParams, PlayerState

TODAY = date(2026, 10, 28)
NOW = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)


def directory(star_status=""):
    def p(name, team, status=""):
        return {"name": name, "short": name, "position": "G", "jersey": "", "team_id": team,
                "status": status, "dob": "", "height_in": "", "weight_lb": ""}  # fmt: skip

    return {"star": p("Star", "16", star_status), "role": p("Role", "16"),
            "gtd": p("Gtd", "16", "Day-To-Day"), "opp": p("Opp", "9"), "opp2": p("Opp Two", "9"),
            "traded": p("Traded", "2")}  # fmt: skip


def player_state():
    s = PlayerState(PlayerParams())
    s.season = "2026-27"
    players = (("star", "16", 35, 35), ("role", "16", 20, 20), ("gtd", "16", 15, 15),
               ("traded", "16", 10, 10), ("opp", "9", 30, 30), ("opp2", "9", 25, 25))  # fmt: skip
    for pid, team, avail, role in players:
        s.rosters[team].add(pid)
        p = s.players[pid]
        p.avail_num, p.avail_den, p.role_num, p.role_den = avail, 1.0, role, 1.0
        p.gs, p.gs_min = 0.5 * 600, 600.0
        p.team, p.last_season = team, "2026-27"  # saved state keeps only recent players
    return s


RATINGS = {"star": (3.0, 0.5), "role": (0.0, 0.0), "gtd": (0.5, 0.5), "opp": (1.0, 1.0),
           "opp2": (0.0, 0.0), "traded": (2.0, 2.0)}  # fmt: skip


def test_chances_follow_the_injury_report():
    ch = shadow_v2.chances(directory("Out"), {"gtd": 0.8}, "16")
    assert ch == {"star": 0.0, "role": 1.0, "gtd": 0.8}  # traded player is on another roster now
    assert shadow_v2.chances(directory(), {}, "16")["gtd"] == 0.5  # no call: a coin flip


def test_a_missing_star_lowers_the_lineup_and_counts_as_missing_value():
    s = player_state()
    healthy = shadow_v2.team_lineup(s, "16", shadow_v2.chances(directory(), {"gtd": 1.0}, "16"),
                                    RATINGS, (-1.0, -0.5))  # fmt: skip
    hurt = shadow_v2.team_lineup(s, "16", shadow_v2.chances(directory("Out"), {"gtd": 1.0}, "16"),
                                 RATINGS, (-1.0, -0.5))  # fmt: skip
    assert healthy["absent_rapm"] == 0 and hurt["absent_rapm"] > 2
    assert hurt["rapm_o_lineup"] < healthy["rapm_o_lineup"]
    assert hurt["missing"] == ["star"] and healthy["missing"] == []


def test_stale_inputs():
    assert shadow_v2._stale(None, TODAY)
    assert not shadow_v2._stale("2026-10-28T14:05:00+00:00", TODAY)
    assert not shadow_v2._stale("2026-10-27T14:05:00+00:00", TODAY)
    assert shadow_v2._stale("2026-10-25T14:05:00+00:00", TODAY)


def setup(tmp_path, impact_date=NOW):
    store = live.Store(tmp_path)
    ratings = {t: 1500.0 for t in ("16", "9", "2")}
    store.save_state(EloState(live.LIVE_PARAMS, ratings=ratings, season="2026-27",
                              last_date=date(2026, 10, 27)))  # fmt: skip
    row = {**dict.fromkeys(live.PREDICTION_FIELDS, ""), "game_id": "g1", "season": "2026-27",
           "season_type": "regular_season", "game_date": "2026-10-28",
           "tipoff_utc": "2026-10-29T00:00:00+00:00", "home_team_id": "16", "away_team_id": "9",
           "home_team": "Minnesota Timberwolves", "away_team": "Golden State Warriors",
           "is_neutral_site": False, "p_home": 0.72, "home_elo": 1550, "away_elo": 1500,
           "home_court": 30, "predicted_at": "2026-10-28T14:05:00+00:00"}  # fmt: skip
    store.append(store.predictions_path("2026-27"), live.PREDICTION_FIELDS, [row])
    shadow.save_state(tmp_path, shadow.ShadowState(player_state(), FormState(), date(2026, 10, 27)))
    rosters.write_directory(tmp_path, directory("Out"), NOW)
    fit = {"lambda": 3000, "home_court": 1.3, "mean_ppp": 115, "prior": {},
           "players": {p: {"o": o, "d": d, "o_prior": o, "d_prior": d, "poss": 5000}
                       for p, (o, d) in RATINGS.items()}}  # fmt: skip
    impact.write(tmp_path, fit, ["2025-26", "2026-27"], impact_date)
    return store


def test_predictions_are_logged_once_and_verified(tmp_path):
    setup(tmp_path)
    out = shadow_v2.predict(tmp_path, TODAY, NOW, "sha")
    assert out["predicted"] == 1
    row = out["rows"][0]
    assert row["missing_home"] == "Star" and row["model"] == shadow_v2.MODEL_NAME
    assert 0 < float(row["p_home"]) < 1 and float(row["absent_diff"]) > 0
    again = shadow_v2.predict(tmp_path, TODAY, NOW, "sha")
    assert again["predicted"] == 0
    assert live.verify(tmp_path, lambda rel: None) == []
    card = json.loads(shadow_v2.scorecard_path(tmp_path, "2026-27").read_text())
    assert card["model"] == shadow_v2.MODEL_NAME
    assert card["promotion_check"]["rule"] == shadow_v2.RULE


def test_no_prediction_from_stale_ratings(tmp_path):
    setup(tmp_path, impact_date=datetime(2026, 10, 20, tzinfo=UTC))
    out = shadow_v2.predict(tmp_path, TODAY, NOW, "sha")
    assert out["predicted"] == 0 and out["skipped"]
    assert not shadow_v2.log_path(tmp_path, "2026-27").exists()


def test_the_star_moves_the_forecast(tmp_path):
    setup(tmp_path)
    without = float(shadow_v2.predict(tmp_path, TODAY, NOW, "sha")["rows"][0]["p_home"])
    other = tmp_path / "healthy"
    setup(other)
    rosters.write_directory(other, directory(""), NOW)
    with_star = float(shadow_v2.predict(other, TODAY, NOW, "sha")["rows"][0]["p_home"])
    assert with_star - without > 0.02
