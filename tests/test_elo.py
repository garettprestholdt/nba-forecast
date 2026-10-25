import random
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from nbaforecast.models import elo
from nbaforecast.models.elo import EloParams


def make_games(rows):
    """rows: (date, home, away, home_score, away_score[, neutral[, season]])"""
    records = []
    for i, r in enumerate(rows):
        d, home, away, hs, as_ = r[:5]
        neutral = r[5] if len(r) > 5 else False
        season = r[6] if len(r) > 6 else "2023-24"
        records.append(
            {
                "game_id": f"g{i}",
                "season": season,
                "season_type": "regular_season",
                "game_date": d,
                "tipoff_utc": datetime(d.year, d.month, d.day, 23, i % 60, tzinfo=UTC),
                "home_team_id": home,
                "away_team_id": away,
                "home_score": hs,
                "away_score": as_,
                "is_neutral_site": neutral,
            }
        )
    return pd.DataFrame(records)


def random_season(n_days=60, teams=8, seed=1, season="2023-24", start=date(2023, 10, 24)):
    rng = random.Random(seed)
    strength = {f"T{i}": rng.gauss(0, 5) for i in range(teams)}
    rows = []
    for day in range(n_days):
        ids = list(strength)
        rng.shuffle(ids)
        for home, away in zip(ids[::2], ids[1::2], strict=True):
            margin = round(strength[home] - strength[away] + 3 + rng.gauss(0, 12)) or 1
            rows.append((start + timedelta(days=day), home, away, 100 + margin, 100, False, season))
    return make_games(rows)


D = date(2023, 11, 1)


def test_win_probability():
    assert elo.win_probability(0) == 0.5
    assert elo.win_probability(400) == pytest.approx(10 / 11)
    assert elo.win_probability(100) + elo.win_probability(-100) == pytest.approx(1)


def test_elo_from_probability_inverts():
    for p in (0.2, 0.5, 0.64):
        assert elo.win_probability(elo.elo_from_probability(p)) == pytest.approx(p)


def test_mov_multiplier():
    assert elo.mov_multiplier(20, 0) > elo.mov_multiplier(5, 0)
    # Same margin counts less when a big favorite wins than when an underdog does.
    assert elo.mov_multiplier(10, 200) < elo.mov_multiplier(10, -200)


def test_first_game_uses_home_court_only():
    preds, _ = elo.run(make_games([(D, "A", "B", 110, 100)]), EloParams(home_advantage=100))
    assert preds.p_home.iloc[0] == pytest.approx(elo.win_probability(100))


def test_neutral_site_has_no_home_court():
    preds, _ = elo.run(make_games([(D, "A", "B", 110, 100, True)]))
    assert preds.p_home.iloc[0] == 0.5
    assert preds.home_court.iloc[0] == 0


def test_ratings_are_zero_sum():
    games = random_season()
    _, ratings = elo.run(games)
    assert sum(ratings.values()) == pytest.approx(1500 * len(ratings))


def test_winner_gains_and_upset_moves_more():
    _, r1 = elo.run(make_games([(D, "A", "B", 110, 100)]))  # favored home team wins
    _, r2 = elo.run(make_games([(D, "A", "B", 100, 110)]))  # home team upset by 10
    assert r1["A"] > 1500 > r1["B"]
    assert (1500 - r2["A"]) > (r1["A"] - 1500)


def test_no_leakage_prediction_ignores_own_result():
    base = random_season(n_days=10)
    flipped = base.copy()
    i = len(base) // 2
    flipped.loc[i, ["home_score", "away_score"]] = flipped.loc[
        i, ["away_score", "home_score"]
    ].values
    p_base, _ = elo.run(base)
    p_flip, _ = elo.run(flipped)
    target = base.loc[i, "game_id"]
    assert p_base.set_index("game_id").p_home[target] == p_flip.set_index("game_id").p_home[target]
    # ...but later predictions do react to that result.
    assert not p_base.p_home.equals(p_flip.p_home)


def test_same_day_games_dont_see_each_other():
    # A beats B at 7pm; C vs A's... different teams at 10pm the same date. Changing the early
    # result must not change the late game's prediction, even with adaptive home court.
    params = EloParams(hca_window=1, home_advantage=100)
    early_win = make_games([(D, "A", "B", 120, 90), (D, "C", "D", 100, 95)])
    early_loss = make_games([(D, "A", "B", 90, 120), (D, "C", "D", 100, 95)])
    p1, _ = elo.run(early_win, params)
    p2, _ = elo.run(early_loss, params)
    assert p1.p_home.iloc[1] == p2.p_home.iloc[1]


def test_team_listed_twice_on_one_date_gets_both_updates():
    games = make_games([(D, "A", "B", 110, 100), (D, "C", "A", 110, 100)])
    _, ratings = elo.run(games)
    assert sum(ratings.values()) == pytest.approx(1500 * 3)
    single = elo.run(make_games([(D, "A", "B", 110, 100)]))[1]["A"] - 1500
    assert ratings["A"] < 1500 + single  # the loss to C was applied too


def test_season_carryover_regresses_to_mean():
    d2 = date(2024, 10, 22)
    games = make_games(
        [(D, "A", "B", 130, 90, False, "2023-24"), (d2, "A", "B", 100, 99, True, "2024-25")]
    )
    preds, _ = elo.run(games, EloParams(carryover=0.5))
    first, second = preds.iloc[0], preds.iloc[1]
    _, after_first = elo.run(games.iloc[:1], EloParams(carryover=0.5))
    assert second.home_elo_pre - 1500 == pytest.approx(0.5 * (after_first["A"] - 1500))
    assert first.home_elo_pre == 1500


def test_adaptive_home_court_tracks_home_win_rate():
    # Home teams win 3 of 4 neutral-free games -> HCA = Elo equivalent of 75%.
    rows = [(D + timedelta(days=i), "A", "B", 110, 100) for i in range(3)]
    rows += [(D + timedelta(days=3), "A", "B", 100, 110), (D + timedelta(days=4), "A", "B", 1, 0)]
    preds, _ = elo.run(make_games(rows), EloParams(hca_window=4, home_advantage=100))
    assert preds.home_court.iloc[3] == 100  # window not full yet: prior
    assert preds.home_court.iloc[4] == pytest.approx(elo.elo_from_probability(0.75))


def test_better_teams_end_up_rated_higher():
    games = random_season(n_days=200, seed=3)
    _, ratings = elo.run(games)
    # Recompute true strengths from the same seed and compare rank order at the extremes.
    rng = random.Random(3)
    strength = {f"T{i}": rng.gauss(0, 5) for i in range(8)}
    best, worst = max(strength, key=strength.get), min(strength, key=strength.get)
    assert ratings[best] > ratings[worst]


def test_missing_columns_rejected():
    with pytest.raises(ValueError, match="missing columns"):
        elo.run(make_games([(D, "A", "B", 110, 100)]).drop(columns=["is_neutral_site"]))


def test_grid():
    params = elo.grid(k=[15, 20], carryover=[0.7, 0.8])
    assert len(params) == 4
    assert {p.k for p in params} == {15, 20}
