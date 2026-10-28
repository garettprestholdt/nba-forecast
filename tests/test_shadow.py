import json
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import requests

from nbaforecast.ingest import boxscores as bx
from nbaforecast.models import hybrid
from nbaforecast.models.elo import EloState
from nbaforecast.models.hybrid import Fit, FitParams, FormState
from nbaforecast.models.players import PlayerParams, PlayerState
from nbaforecast.pipeline import live, shadow

FIXTURES = Path(__file__).parent / "fixtures"
SUMMARY = json.loads((FIXTURES / "espn_boxscore_401909868.json").read_text())  # WSH @ CHA
SIDES = {c["homeAway"]: c for c in SUMMARY["header"]["competitions"][0]["competitors"]}
HOME, AWAY = SIDES["home"]["id"], SIDES["away"]["id"]
GAME = {
    "game_id": "401909868", "season": "2026-27", "season_type": "regular_season",
    "game_date": "2026-10-26", "tipoff_utc": "2026-10-26T23:00:00+00:00",
    "home_team_id": HOME, "away_team_id": AWAY, "home_team": "Home", "away_team": "Away",
    "home_score": SIDES["home"]["score"], "away_score": SIDES["away"]["score"],
    "is_neutral_site": False,
}  # fmt: skip
TODAY = date(2026, 10, 27)
NOW = datetime(2026, 10, 27, 14, 5, tzinfo=UTC)


def parsed():
    return bx.parse_boxscore(SUMMARY, {**GAME, "game_date": date(2026, 10, 26)})


def test_clean_box_matches_dbt_rules():
    box = shadow.clean_box(parsed(), aliases={})
    assert set(box) == {HOME, AWAY}
    for team, rows in box.items():
        played = [r for r in rows if r.played]
        assert all(r.minutes > 0 for r in played) and not any(r.stats_missing for r in rows)
        pts = sum(r.pts for r in rows)
        assert pts == int(SIDES["home" if team == HOME else "away"]["score"])
        r = played[0]
        expected = round(
            r.pts + 0.4 * r.fgm - 0.7 * r.fga - 0.4 * (r.fta - r.ftm) + 0.7 * r.oreb
            + 0.3 * r.dreb + r.stl + 0.7 * r.ast + 0.7 * r.blk - 0.4 * r.pf - r.tov, 1
        )  # fmt: skip
        assert r.game_score == expected


def test_clean_box_maps_aliases_and_merges_duplicates():
    rows = parsed()
    star = max(rows, key=lambda r: r["minutes"])
    duplicate = {**star, "player_id": "alias-1", "minutes": 0, "pts": 0, "did_not_play": True}
    box = shadow.clean_box([*rows, duplicate], {"alias-1": (star["player_id"], star["player"])})
    team = box[star["team_id"]]
    assert sum(r.player_id == star["player_id"] for r in team) == 1
    assert next(r for r in team if r.player_id == star["player_id"]).minutes == star["minutes"]


def test_blank_box_is_stats_missing_not_zero():
    rows = parsed()
    blank = [{**r, **dict.fromkeys(bx.STAT_COLUMNS, 0)} if r["team_id"] == HOME else r
             for r in rows]  # fmt: skip
    box = shadow.clean_box(blank, {})
    assert all(r.stats_missing and r.minutes is None and r.game_score is None for r in box[HOME])
    assert not any(r.stats_missing for r in box[AWAY])
    assert shadow.form_values(box[HOME], box[AWAY], 100, 90) is None  # skipped, like dbt


def test_form_values_are_consistent():
    box = shadow.clean_box(parsed(), {})
    hs, as_ = int(GAME["home_score"]), int(GAME["away_score"])
    home = shadow.form_values(box[HOME], box[AWAY], hs, as_)
    away = shadow.form_values(box[AWAY], box[HOME], as_, hs)
    assert np.sign(home[0]) == np.sign(hs - as_) and home[0] == pytest.approx(-away[0], abs=0.11)
    assert home[1:] == pytest.approx(-away[1:], abs=2e-4)  # each edge is home minus away


def toy_model() -> shadow.Model:
    names = hybrid.feature_set("realistic")
    coef = np.zeros(len(names))
    coef[names.index("elo_strength")] = 1.0
    coef[names.index("elo_home")] = 1.0
    fit = Fit(FitParams("logistic", 1.0, intercept=False), names, np.zeros(len(names)),
              np.ones(len(names)), coef)  # fmt: skip
    return shadow.Model(fit, PlayerParams(), 0.95, {"fit_through": "2026-04-12"})


@pytest.fixture
def branch(tmp_path):
    store = live.Store(tmp_path)
    elo_state = EloState(live.LIVE_PARAMS, ratings={HOME: 1550.0, AWAY: 1450.0},
                         season="2026-27", last_date=date(2026, 10, 26))  # fmt: skip
    elo_state.record_played((HOME, AWAY), date(2026, 10, 26))
    store.save_state(elo_state)
    store.append(store.results_path("2026-27"), live.RESULT_FIELDS, [GAME])
    tonight = {
        **{k: GAME[k] for k in ("season", "season_type", "home_team_id", "away_team_id",
                                "home_team", "away_team", "is_neutral_site")},
        "game_id": "401909999", "game_date": "2026-10-27",
        "tipoff_utc": "2026-10-27T23:30:00+00:00", "p_home": 0.7, "home_elo": 1550.0,
        "away_elo": 1450.0, "home_court": 60.0, "market_p_home": 0.65, "market_books": 5,
        "model": live.MODEL_NAME, "ratings_through": "2026-10-26",
        "predicted_at": "2026-10-27T14:00:00+00:00", "code_sha": "abc",
    }  # fmt: skip
    store.append(store.predictions_path("2026-27"), live.PREDICTION_FIELDS, [tonight])
    state = shadow.ShadowState(PlayerState(PlayerParams()), FormState(), date(2026, 10, 25))
    shadow.save_state(tmp_path, state)
    return tmp_path


def run(root, fetch=lambda game_id: SUMMARY):
    return shadow.run(root, today=TODAY, now=NOW, fetch_summary=fetch, model=toy_model(),
                      code_sha="test")  # fmt: skip


def test_daily_shadow_run_end_to_end(branch):
    out = run(branch)
    assert out["ingested_box_scores"] == 1 and out["predicted"] == 1
    state = shadow.load_state(branch)
    assert state.last_date == date(2026, 10, 26)
    assert HOME in state.players.rosters and state.form.den[HOME] == 1
    row = live.Store.read(shadow.log_path(branch, "2026-27"))[0]
    # The toy model is Elo's own logit (strength + home court), so it reproduces Elo.
    expected = 1 / (1 + 10 ** (-(1550 - 1450 + 60) / 400))
    assert float(row["p_home"]) == pytest.approx(expected, abs=1e-6)
    assert row["model"] == shadow.MODEL_NAME and row["ratings_through"] == "2026-10-26"
    assert live.verify(branch, lambda rel: None) == []


def test_rerun_does_not_duplicate_or_reingest(branch):
    calls = []
    run(branch, fetch=lambda gid: calls.append(gid) or SUMMARY)
    again = run(branch, fetch=lambda gid: calls.append(gid) or SUMMARY)
    assert calls == ["401909868"] and again["predicted"] == 0
    assert len(live.Store.read(shadow.log_path(branch, "2026-27"))) == 1


def test_missing_box_score_is_skipped(branch):
    def gone(game_id):
        resp = requests.Response()
        resp.status_code = 404
        raise requests.HTTPError("404", response=resp)

    out = run(branch, fetch=gone)
    assert out["ingested_missing"] == 1 and shadow.load_state(branch).last_date == date(
        2026, 10, 26
    )


def test_network_error_leaves_state_untouched(branch):
    before = shadow.state_path(branch).read_text()

    def down(game_id):
        raise requests.ConnectionError("down")

    with pytest.raises(requests.ConnectionError):
        run(branch, fetch=down)
    assert shadow.state_path(branch).read_text() == before  # next run catches up


def test_started_games_are_not_predicted(branch):
    late = shadow.run(branch, today=TODAY, now=datetime(2026, 10, 28, 1, tzinfo=UTC),
                      fetch_summary=lambda g: SUMMARY, model=toy_model(), code_sha="t")  # fmt: skip
    assert late["predicted"] == 0 and late["already_started"] == 1


def test_scorecard_compares_on_the_same_games(branch):
    run(branch)
    store = live.Store(branch)
    result = {**GAME, "game_id": "401909999", "game_date": "2026-10-27", "home_score": 110,
              "away_score": 100}  # fmt: skip
    store.append(store.results_path("2026-27"), live.RESULT_FIELDS, [result])
    card = shadow.scorecard(store, "2026-27", NOW)
    assert card["games_scored"] == 1 and card["hybrid"]["accuracy"] == 1.0
    assert card["head_to_head_with_market"]["games"] == 1


def test_state_and_model_round_trip(branch):
    run(branch)
    data = json.loads(shadow.state_path(branch).read_text())
    again = shadow.ShadowState.from_dict(data)
    assert again.to_dict() == data | {"form": again.form.to_dict()}
    model = toy_model()
    loaded = shadow.Model.from_dict(json.loads(json.dumps(model.to_dict())))
    df = pd.DataFrame([dict.fromkeys(model.fit.features, 0.3)])
    assert loaded.fit.predict(df) == pytest.approx(model.fit.predict(df))


def test_player_state_prunes_long_gone_players():
    state = PlayerState(PlayerParams())
    for season, pid in (("2023-24", "old"), ("2025-26", "recent"), ("2026-27", "now")):
        state.season = season
        row = pd.Series({"player_id": pid, "stats_missing": False, "played": True, "minutes": 30,
                         "game_score": 12.0, "plus_minus": 3})  # fmt: skip
        state.record("T", [row])
    kept = state.to_dict(keep_seasons=2)["players"]
    assert set(kept) == {"recent", "now"}


def window(tmp_path, n=400, *, published=0.6, hybrid=0.6, shadow_share=1.0, playoffs=True,
           season="2026-27", seed=1, truth=lambda i: 0.6):  # fmt: skip
    """A season where the home team wins 60% of the time; each model forecasts a fixed
    probability, so the better model is the one closer to 0.6."""
    rng = np.random.default_rng(seed)
    store = live.Store(tmp_path)
    blank = dict.fromkeys(live.PREDICTION_FIELDS, "")
    results, preds, shadows = [], [], []
    for i in range(n):
        day = f"2026-11-{1 + i % 28:02d}"
        base = {"game_id": f"{season}-{i}", "season": season, "season_type": "regular_season",
                "game_date": day, "tipoff_utc": f"{day}T23:30:00+00:00"}  # fmt: skip
        home_won = rng.random() < truth(i)
        results.append({**dict.fromkeys(live.RESULT_FIELDS, ""), **base,
                        "home_score": 110 if home_won else 100,
                        "away_score": 100 if home_won else 110})  # fmt: skip
        preds.append({**blank, **base, "p_home": published})
        if i < shadow_share * n:
            p = hybrid(i) if callable(hybrid) else hybrid
            shadows.append({**dict.fromkeys(shadow.SHADOW_FIELDS, ""), **base, "p_home": p})
    if playoffs:
        results.append({**dict.fromkeys(live.RESULT_FIELDS, ""), "game_id": "po1",
                        "season": season, "season_type": "playoffs", "game_date": "2027-04-20",
                        "home_score": 100, "away_score": 90})  # fmt: skip
    store.append(store.results_path(season), live.RESULT_FIELDS, results)
    store.append(store.predictions_path(season), live.PREDICTION_FIELDS, preds)
    live.Store.append(shadow.log_path(tmp_path, season), shadow.SHADOW_FIELDS, shadows)
    return store


def test_promotion_waits_for_the_end_of_the_regular_season(tmp_path):
    store = window(tmp_path, published=0.5, hybrid=0.6, playoffs=False)
    check = shadow.promotion_check(store, ["2026-27"])
    assert check["status"] == "in progress" and check["games"] == 400
    assert check["mean_difference"] > 0  # interim numbers are visible, but no decision


def test_promotes_a_clearly_better_hybrid(tmp_path):
    check = shadow.promotion_check(window(tmp_path, published=0.5, hybrid=0.6), ["2026-27"])
    assert check["status"] == "promote" and all(check["conditions"].values())


def test_retires_a_clearly_worse_hybrid(tmp_path):
    check = shadow.promotion_check(window(tmp_path, published=0.6, hybrid=0.45), ["2026-27"])
    assert check["status"] == "retire" and check["ci95"][1] < 0


def test_inconclusive_first_window_extends_then_stops(tmp_path):
    # Even on average but noisy game to game: too few games to call it either way.
    store = window(tmp_path, n=60, published=0.6, hybrid=lambda i: 0.5 if i % 2 else 0.7)
    assert shadow.promotion_check(store, ["2026-27"])["status"] == "extend"
    pooled = ["2026-27", "2027-28"]
    window(tmp_path, n=60, published=0.6, hybrid=lambda i: 0.5 if i % 2 else 0.7,
           season="2027-28", playoffs=False)  # fmt: skip
    assert shadow.promotion_check(store, pooled)["status"] == "in progress"
    assert shadow.promotion_check(store, pooled)["games"] == 120  # both seasons count
    live.Store.append(store.results_path("2027-28"), live.RESULT_FIELDS, [
        {**dict.fromkeys(live.RESULT_FIELDS, ""), "game_id": "po2", "season": "2027-28",
         "season_type": "playoffs", "home_score": 100, "away_score": 90}
    ])  # fmt: skip
    assert shadow.promotion_check(store, pooled)["status"] == "not promoted"


def test_low_coverage_blocks_promotion(tmp_path):
    store = window(tmp_path, published=0.5, hybrid=0.6, shadow_share=0.8)
    check = shadow.promotion_check(store, ["2026-27"])
    assert check["coverage"] == pytest.approx(0.8) and not check["conditions"]["coverage"]
    assert check["status"] == "extend"


def test_better_but_miscalibrated_hybrid_is_not_promoted(tmp_path):
    # Two kinds of games: home teams truly win 80% or 40%. The published model says 60% for
    # both; the hybrid tells them apart but overstates it (90% / 30%). It wins on log-loss but
    # misses its own forecasts by 10 points, so only the calibration guardrail fails.
    kind_a = lambda i: i % 2 == 0  # noqa: E731
    store = window(tmp_path, n=4000, published=0.6,
                   hybrid=lambda i: 0.9 if kind_a(i) else 0.3,
                   truth=lambda i: 0.8 if kind_a(i) else 0.4)  # fmt: skip
    check = shadow.promotion_check(store, ["2026-27"])
    assert check["conditions"] == {"better_on_average": True, "not_meaningfully_worse": True,
                                   "coverage": True, "calibration": False}  # fmt: skip
    assert check["status"] == "extend"


def test_promotion_check_pools_the_right_seasons():
    assert shadow._promotion_seasons("2026-27") == ["2026-27"]
    assert shadow._promotion_seasons("2027-28") == ["2026-27", "2027-28"]
    assert shadow._promotion_seasons("2028-29") is None


def test_not_started_without_shadow_predictions(tmp_path):
    store = window(tmp_path, shadow_share=0)
    assert shadow.promotion_check(store, ["2026-27"])["status"] == "not started"
