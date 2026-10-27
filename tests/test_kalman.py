import random
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from nbaforecast.models import kalman
from nbaforecast.models.kalman import KalmanParams, KalmanState


def game(i, day, home, away, hs, as_, season="2023-24", neutral=False):
    return {
        "game_id": f"g{i}",
        "season": season,
        "season_type": "regular_season",
        "game_date": day,
        "tipoff_utc": datetime(day.year, day.month, day.day, 23, i % 60, tzinfo=UTC),
        "home_team_id": home,
        "away_team_id": away,
        "home_score": hs,
        "away_score": as_,
        "is_neutral_site": neutral,
    }


D = date(2023, 11, 1)


def frame(rows):
    return pd.DataFrame(rows)


def test_equal_teams_at_neutral_site_is_a_coin_flip():
    preds, _ = kalman.run(frame([game(0, D, "1", "2", 110, 100, neutral=True)]))
    assert preds.p_home.iloc[0] == pytest.approx(0.5)


def test_home_court_prior_favors_home_team():
    preds, _ = kalman.run(frame([game(0, D, "1", "2", 110, 100)]), KalmanParams(hca_prior=3.0))
    assert preds.p_home.iloc[0] > 0.5
    assert preds.expected_margin.iloc[0] == pytest.approx(3.0)


def test_update_moves_strengths_and_shrinks_uncertainty():
    _, state = kalman.run(frame([game(0, D, "1", "2", 130, 100)]))
    ratings = state.ratings()
    assert ratings["1"] > 0 > ratings["2"]
    assert state.cov[0, 0] < KalmanParams().prior_var


def test_margin_is_capped():
    _, a = kalman.run(frame([game(0, D, "1", "2", 160, 100)]), KalmanParams(margin_cap=30))
    _, b = kalman.run(frame([game(0, D, "1", "2", 130, 100)]), KalmanParams(margin_cap=30))
    assert a.ratings()["1"] == pytest.approx(b.ratings()["1"])


def test_no_leakage_own_result_and_same_day():
    rows = [game(0, D, "1", "2", 120, 90), game(1, D, "3", "4", 100, 95)]
    flipped = [game(0, D, "1", "2", 90, 120), game(1, D, "3", "4", 100, 95)]
    a, _ = kalman.run(frame(rows))
    b, _ = kalman.run(frame(flipped))
    assert a.p_home.tolist() == b.p_home.tolist()  # neither game sees game 0's result


def test_new_season_regresses_and_widens():
    rows = [
        game(0, D, "1", "2", 130, 100),
        game(1, date(2024, 10, 22), "3", "4", 100, 99, season="2024-25"),
    ]
    params = KalmanParams(carryover=0.5, season_var=9.0)
    state = KalmanState(params)
    kalman.walk(frame(rows[:1]), state)
    before_mean, before_var = state.mean[0], state.cov[0, 0]
    state.advance_to(date(2024, 10, 22), "2024-25")
    assert state.mean[0] == pytest.approx(0.5 * before_mean)
    days = (date(2024, 10, 22) - D).days
    expected = 0.25 * (before_var + params.team_drift**2 * days) + 9.0
    assert state.cov[0, 0] == pytest.approx(expected)


def test_back_to_back_and_travel_features():
    rows = [
        game(0, D, "13", "2", 100, 99),  # Boston plays at the Lakers...
        game(1, D + timedelta(days=1), "12", "2", 100, 99),  # ...then at the Clippers next day
    ]
    preds, _ = kalman.run(frame(rows))
    second = preds.iloc[1]
    assert second.away_back_to_back == 1.0 and second.home_back_to_back == 0.0
    assert 0 < second.travel_gap_1000km < 0.05  # Crypto.com Arena -> Intuit Dome is ~13 km


def test_haversine_boston_to_los_angeles():
    assert kalman.km(kalman.ARENAS["2"], kalman.ARENAS["13"]) == pytest.approx(4170, rel=0.02)


def test_relocations_and_bubble():
    assert kalman.home_city("25", "2006-07") == kalman.SEATTLE
    assert kalman.home_city("25", "2008-09") == kalman.ARENAS["25"]
    assert kalman.home_city("3", "2005-06") == kalman.OKLAHOMA_CITY
    g = pd.Series(game(0, date(2020, 8, 1), "14", "2", 1, 0, season="2019-20"))
    assert kalman.game_city(g) == kalman.ORLANDO_BUBBLE


def test_features_off_keeps_coefficients_at_zero():
    rng = random.Random(1)
    rows = []
    for d in range(30):
        teams = [str(t) for t in range(1, 31)]
        rng.shuffle(teams)
        for i, (h, a) in enumerate(zip(teams[::2], teams[1::2], strict=True)):
            rows.append(
                game(d * 15 + i, D + timedelta(days=d), h, a, 100 + rng.randint(-15, 15), 100)
            )
    _, state = kalman.run(frame(rows), KalmanParams(use_features=False))
    coefs = state.coefficients()
    assert all(coefs[f] == 0 for f in kalman.FEATURES)


def test_covariance_stays_symmetric_and_positive():
    rng = random.Random(2)
    rows = [
        game(
            i,
            D + timedelta(days=i // 5),
            str(rng.randint(1, 15)),
            str(rng.randint(16, 30)),
            100 + rng.randint(-20, 20),
            100,
        )
        for i in range(200)
    ]
    _, state = kalman.run(frame(rows))
    assert np.allclose(state.cov, state.cov.T)
    assert np.all(np.linalg.eigvalsh(state.cov) > -1e-9)
