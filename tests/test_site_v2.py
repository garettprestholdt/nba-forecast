import json
from datetime import UTC, date, datetime

import pandas as pd
import pytest

from nbaforecast import boxlog, live, seasonelo
from nbaforecast.models import elo
from nbaforecast.models.elo import EloState
from nbaforecast.web import fan, pages

FIXTURE = json.loads(
    (
        __import__("pathlib").Path(__file__).parent / "fixtures" / "espn_boxscore_401909868.json"
    ).read_text()
)  # WSH @ CHA, Oct 2026
SIDES = {c["homeAway"]: c for c in FIXTURE["header"]["competitions"][0]["competitors"]}
HOME, AWAY = SIDES["home"]["id"], SIDES["away"]["id"]


def row(**kw):
    base = dict.fromkeys(boxlog.FIELDS, 0)
    base.update(game_id="g1", game_date="2026-10-26", team_id="1", opponent_id="2",
                is_home=True, player_id="p", player="P", position="G", starter=True,
                did_not_play=False, dnp_reason="", stats_missing=False, plus_minus=5,
                game_score=10.0)  # fmt: skip
    base.update(kw)
    return base


def test_season_line_shooting_and_usage():
    games = [row(game_id="g1", minutes=30, pts=20, fgm=7, fga=14, fg3m=2, fg3a=5, ftm=4, fta=4,
                 tov=2, reb=6, ast=5),
             row(game_id="g2", minutes=34, pts=30, fgm=10, fga=20, fg3m=4, fg3a=9, ftm=6, fta=8,
                 tov=4, reb=4, ast=7)]  # fmt: skip
    team = {"g1": {"minutes": 240, "fga": 85, "fta": 20, "tov": 14},
            "g2": {"minutes": 240, "fga": 90, "fta": 25, "tov": 12}}  # fmt: skip
    line = boxlog.season_line(games, team)
    assert line["g"] == 2 and line["pts"] == 25.0 and line["min"] == 32.0
    assert line["ts_pct"] == pytest.approx(50 / (2 * (34 + 0.44 * 12)), abs=1e-3)
    assert line["efg_pct"] == pytest.approx((17 + 3) / 34, abs=1e-3)
    usage = 100 * (34 + 0.44 * 12 + 6) * (480 / 5) / (64 * (175 + 0.44 * 45 + 26))
    assert line["usg_pct"] == pytest.approx(usage, abs=0.1)


def test_season_line_ignores_dnps_and_blank_box_scores():
    games = [row(minutes=0, did_not_play=True), row(game_id="g2", stats_missing=True, minutes=0)]
    assert boxlog.season_line(games, {}) == {"g": 0}


def test_box_sync_appends_missing_games_once(tmp_path):
    store = live.Store(tmp_path)
    game = {**dict.fromkeys(live.RESULT_FIELDS, ""), "game_id": "401909868", "season": "2026-27",
            "season_type": "regular_season", "game_date": "2026-10-26",
            "tipoff_utc": "2026-10-26T23:00:00+00:00", "home_team_id": HOME, "away_team_id": AWAY,
            "home_score": SIDES["home"]["score"], "away_score": SIDES["away"]["score"]}  # fmt: skip
    store.append(store.results_path("2026-27"), live.RESULT_FIELDS, [game])
    calls = []
    out = boxlog.sync(store, "2026-27", lambda g: calls.append(g) or FIXTURE, date(2026, 10, 26))
    again = boxlog.sync(store, "2026-27", lambda g: calls.append(g) or FIXTURE, date(2026, 10, 26))
    assert out["games_added"] == 1 and again["games_added"] == 0 and calls == ["401909868"]
    assert live.verify(tmp_path, lambda rel: None) == []
    rows = live.Store.read(boxlog.path(tmp_path, "2026-27"))
    live.Store.append(boxlog.path(tmp_path, "2026-27"), boxlog.FIELDS, rows[:1])  # a duplicate
    assert any("duplicate" in p for p in live.verify(tmp_path, lambda rel: None))
    rows = rows
    pts = {t: sum(int(r["pts"]) for r in rows if r["team_id"] == t) for t in (HOME, AWAY)}
    assert pts[HOME] == int(SIDES["home"]["score"]) and pts[AWAY] == int(SIDES["away"]["score"])


def games_frame(n_days=6):
    rows = []
    for d in range(n_days):
        day = date(2026, 10, 20 + d)
        rows.append({"game_id": f"x{d}", "season": "2026-27", "season_type": "regular_season",
                     "game_date": day, "tipoff_utc": datetime(2026, 10, 20 + d, 23, tzinfo=UTC),
                     "home_team_id": "2" if d % 2 else "17", "away_team_id": "17" if d % 2 else "2",
                     "home_score": 110 + d, "away_score": 100,
                     "is_neutral_site": False})  # fmt: skip
    return pd.DataFrame(rows)


def test_replay_reproduces_live_ratings(tmp_path):
    published = tmp_path / "pub"
    start = EloState(live.LIVE_PARAMS, ratings={"2": 1600.0, "17": 1400.0})
    start.start_day("2026-27")
    seasonelo.write_start(start, "2026-27", published)
    games = games_frame()
    live_state = EloState.from_dict(start.to_dict())
    elo.walk(games, live_state)
    store = live.Store(tmp_path / "data")
    store.append(store.results_path("2026-27"), live.RESULT_FIELDS, [
        {**{k: g[k] for k in ("game_id", "season", "season_type", "home_team_id", "away_team_id",
                              "home_score", "away_score", "is_neutral_site")},
         "game_date": g["game_date"].isoformat(), "tipoff_utc": g["tipoff_utc"].isoformat(),
         "home_team": "", "away_team": ""}
        for g in games.to_dict("records")
    ])  # fmt: skip
    tl = seasonelo.replay(store, "2026-27", published)
    assert seasonelo.check(tl, live_state.ratings)
    bos = tl["games"]["2"]
    assert len(bos) == 6 and bos[0]["pre"] == pytest.approx(1600.0)
    assert sum(g["change"] for g in bos) == pytest.approx(live_state.ratings["2"] - 1600.0)
    assert all(g["won"] == (g["pts"] > g["opp_pts"]) for g in bos)


def test_ranks_respect_direction():
    stats = {"a": {"tov": 0.10, "efg": 0.55}, "b": {"tov": 0.15, "efg": 0.50}}
    for t in stats:
        stats[t].update({k: 0.0 for k, *_ in pages.TEAM_METRICS if k not in stats[t]})
    rk = pages.ranks(stats)
    assert rk["a"]["tov"] == 1 and rk["a"]["efg"] == 1  # fewer turnovers, better shooting
    assert rk["b"]["tov"] == 2


def test_history_counts_and_buckets(tmp_path):
    store = live.Store(tmp_path)
    blank = dict.fromkeys(live.PREDICTION_FIELDS, "")
    preds = [{**blank, "game_id": "a", "game_date": "2026-10-27", "home_team_id": "2",
              "away_team_id": "17", "p_home": "0.8", "season": "2026-27"},
             {**blank, "game_id": "b", "game_date": "2026-10-27", "home_team_id": "1",
              "away_team_id": "5", "p_home": "0.55", "season": "2026-27"}]  # fmt: skip
    results = [{**dict.fromkeys(live.RESULT_FIELDS, ""), "game_id": "a", "home_score": 110,
                "away_score": 100},
               {**dict.fromkeys(live.RESULT_FIELDS, ""), "game_id": "b", "home_score": 90,
                "away_score": 100}]  # fmt: skip
    store.append(store.predictions_path("2026-27"), live.PREDICTION_FIELDS, preds)
    store.append(store.results_path("2026-27"), live.RESULT_FIELDS, results)
    team_map = fan.teams(tmp_path, {}, 1505.0)
    h = pages.history(store, "2026-27", team_map)
    assert h["summary"]["games"] == 2 and h["summary"]["correct"] == 1
    assert {b["label"]: b["games"] for b in h["buckets"]} == {"50–60%": 1, "80%+": 1}
    boston = next(t for t in h["teams"] if t["team"].abbr == "BOS")
    assert boston["games"] == 1 and boston["accuracy"] == 1.0


def test_rotation_puzzle_picks_the_biggest_underdog(tmp_path):
    from nbaforecast import shadow
    from nbaforecast.ingest import rosters
    from nbaforecast.models.hybrid import FormState
    from nbaforecast.models.players import PlayerParams, PlayerState

    store = live.Store(tmp_path)
    blank = dict.fromkeys(live.PREDICTION_FIELDS, "")
    store.append(store.predictions_path("2026-27"), live.PREDICTION_FIELDS, [
        {**blank, "game_id": "a", "game_date": "2026-10-27", "home_team_id": "2",
         "away_team_id": "17", "p_home": "0.9", "season": "2026-27"},
        {**blank, "game_id": "b", "game_date": "2026-10-27", "home_team_id": "1",
         "away_team_id": "5", "p_home": "0.6", "season": "2026-27"},
    ])  # fmt: skip
    state = PlayerState(PlayerParams())
    state.season = "2026-27"
    directory = {}
    for i in range(7):
        pid = f"n{i}"
        r = type(
            "R",
            (),
            {
                "player_id": pid,
                "stats_missing": False,
                "played": True,
                "minutes": 30 - 3 * i,
                "game_score": 12.0 - i,
                "plus_minus": 0,
            },
        )
        state.record("17", [r])
        directory[pid] = {"name": f"Net {i}", "short": f"N{i}", "position": "G", "jersey": "",
                          "team_id": "17", "status": "Out" if i == 0 else ""}  # fmt: skip
    shadow.save_state(tmp_path, shadow.ShadowState(state, FormState(), date(2026, 10, 26)))
    rosters.write_directory(tmp_path, directory, datetime.now(UTC))
    pub = tmp_path / "pub" / "hybrid"
    pub.mkdir(parents=True)
    (pub / "model.json").write_text(json.dumps({"features": ["production_real"], "coef": [0.3],
                                                "sd": [6.0]}))  # fmt: skip
    z = pages.rotation_puzzle(store, "2026-27", "2026-10-27", fan.teams(tmp_path, {}, 1505.0),
                              tmp_path / "pub")  # fmt: skip
    assert z["underdog"]["abbr"] == "BKN" and z["p_dog"] == pytest.approx(0.1)
    assert "n0" not in {p["id"] for p in z["players"]}  # ruled out
    assert z["beta"] == pytest.approx(0.05) and len(z["players"]) == 6
