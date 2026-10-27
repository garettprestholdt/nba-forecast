from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from nbaforecast.models import hybrid
from nbaforecast.models.hybrid import FORM_COLUMNS, FitParams


def synthetic(n_seasons=4, games=400, seed=0):
    """Games where the home team wins more often when feature x is high."""
    rng = np.random.default_rng(seed)
    rows = []
    for s in range(n_seasons):
        season = f"{2005 + s}-{(6 + s) % 100:02d}"
        x = rng.normal(size=games)
        margin = 3 + 6 * x + rng.normal(scale=12, size=games)
        for i in range(games):
            rows.append({"season": season, "game_id": f"{s}-{i}", "x": x[i],
                         "noise": rng.normal(), "margin": margin[i],
                         "home_win": int(margin[i] > 0)})  # fmt: skip
    return pd.DataFrame(rows)


@pytest.mark.parametrize("link", ["logistic", "margin"])
def test_fit_finds_the_signal(link):
    df = synthetic()
    model = hybrid.fit(df, ["x", "noise"], FitParams(link, 1.0))
    coef = model.coefficients()
    assert coef["x"] > 0 and abs(coef["noise"]) < 0.2 * coef["x"]
    p = model.predict(pd.DataFrame({"x": [-2.0, 0.0, 2.0], "noise": [0.0, 0.0, 0.0]}))
    assert p[0] < p[1] < p[2] and p[1] > 0.5  # home edge from the intercept


def test_penalty_shrinks_coefficients():
    df = synthetic()
    loose = hybrid.fit(df, ["x"], FitParams("logistic", 0.0)).coefficients()["x"]
    tight = hybrid.fit(df, ["x"], FitParams("logistic", 1e5)).coefficients()["x"]
    assert abs(tight) < abs(loose)


def test_walk_forward_never_sees_the_season_it_predicts():
    df = synthetic(n_seasons=5)
    preds, fits = hybrid.walk_forward(df, ["x"], FitParams("logistic", 1.0))
    assert sorted(fits) == sorted(df["season"].unique())[2:]  # two seasons of history first
    last = sorted(fits)[-1]
    flipped = df.copy()
    target = flipped["season"] == last
    flipped.loc[target, "home_win"] = 1 - flipped.loc[target, "home_win"]
    again, _ = hybrid.walk_forward(flipped, ["x"], FitParams("logistic", 1.0))
    a = preds[preds["season"] == last]["p_home"].to_numpy()
    b = again[again["season"] == last]["p_home"].to_numpy()
    assert np.allclose(a, b)


def test_team_form_is_pre_game_and_shrunk():
    days = [date(2021, 1, 1) + timedelta(days=i) for i in range(3)]
    tg = pd.DataFrame(
        [
            {
                "game_id": f"g{i}",
                "team_id": "A",
                "season": "2020-21",
                "game_date": d,
                **dict.fromkeys(FORM_COLUMNS, 10.0),
            }
            for i, d in enumerate(days)
        ]  # fmt: skip
    )
    form = hybrid.team_form(tg, decay=1.0, carryover=1.0, prior=1.0).set_index("game_id")
    assert form.loc["g0", "net_rating"] == 0  # no history yet
    assert form.loc["g1", "net_rating"] == pytest.approx(10 * 1 / 2)  # one game + prior of 1
    assert form.loc["g2", "net_rating"] == pytest.approx(10 * 2 / 3)


def test_back_to_backs():
    t0 = datetime(2021, 1, 1, tzinfo=UTC)
    games = pd.DataFrame(
        [
            {
                "game_id": "1",
                "home_team_id": "A",
                "away_team_id": "B",
                "tipoff_utc": t0,
                "game_date": date(2021, 1, 1),
            },
            {
                "game_id": "2",
                "home_team_id": "C",
                "away_team_id": "A",
                "tipoff_utc": t0 + timedelta(days=1),
                "game_date": date(2021, 1, 2),
            },
            {
                "game_id": "3",
                "home_team_id": "B",
                "away_team_id": "C",
                "tipoff_utc": t0 + timedelta(days=3),
                "game_date": date(2021, 1, 4),
            },
        ]  # fmt: skip
    )
    flags = hybrid.back_to_backs(games).set_index("game_id")
    assert flags.loc["2"].tolist() == [0, 1]  # A played yesterday; C didn't
    assert flags.loc["3"].tolist() == [0, 0]


def test_without_intercept_home_edge_comes_only_from_the_home_feature():
    df = synthetic().assign(home=1.0)
    df.loc[df.index % 10 == 0, "home"] = 0.0  # some neutral-site games
    model = hybrid.fit(df, ["x", "home"], FitParams("logistic", 1.0, intercept=False))
    assert "intercept" not in model.coefficients() and model.coefficients()["home"] > 0
    p = model.predict(pd.DataFrame({"x": [0.0, 0.0], "home": [0.0, 1.0]}))
    assert p[0] == pytest.approx(0.5) and p[1] > 0.5  # even teams, neutral floor: a coin flip
