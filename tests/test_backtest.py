import json
import random
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from nbaforecast.models.elo import EloParams
from nbaforecast.research import backtest


def league(seed=0, teams=10, days_per_season=40, seasons=("2004-05", "2005-06", "2020-21")):
    """Synthetic league: fixed team strengths plus home court, one season per label."""
    rng = random.Random(seed)
    strength = {f"T{i}": rng.gauss(0, 6) for i in range(teams)}
    rows = []
    for s_idx, season in enumerate(seasons):
        start = date(2004 + s_idx * 3, 11, 1)
        for day in range(days_per_season):
            ids = list(strength)
            rng.shuffle(ids)
            for home, away in zip(ids[::2], ids[1::2], strict=True):
                margin = round(strength[home] - strength[away] + 3 + rng.gauss(0, 11)) or 1
                d = start + timedelta(days=day)
                rows.append(
                    {
                        "game_id": f"{season}-{day}-{home}",
                        "season": season,
                        "season_type": "regular_season",
                        "game_date": d,
                        "tipoff_utc": datetime(d.year, d.month, d.day, 23, tzinfo=UTC),
                        "home_team_id": home,
                        "away_team_id": away,
                        "home_score": 100 + margin,
                        "away_score": 100,
                        "is_neutral_site": False,
                    }
                )
    return pd.DataFrame(rows)


CANDIDATES = [
    EloParams(k=2, home_advantage=0),  # barely learns, ignores home court
    EloParams(k=20, home_advantage=80),
    EloParams(k=20, hca_window=50),
]


def test_seasons():
    assert backtest.seasons(2019, 2020) == ["2019-20", "2020-21"]
    assert backtest.TRAIN_SEASONS[0] == "2005-06" and backtest.TRAIN_SEASONS[-1] == "2018-19"
    assert "2019-20" not in backtest.TRAIN_SEASONS + backtest.HOLDOUT_SEASONS


def test_tune_ranks_sensible_params_first():
    tuning = backtest.tune(league(), CANDIDATES)
    assert list(tuning["log_loss"]) == sorted(tuning["log_loss"])
    assert tuning.iloc[-1]["k"] == 2  # the do-nothing model is worst
    # Only train seasons are scored: 2005-06 here.
    assert tuning.iloc[0]["n"] == 40 * 5


def test_run_report_structure_and_beats_baselines():
    report = backtest.run(league(seed=4), CANDIDATES)
    models = report.holdout.set_index("model")
    assert models.loc["Coin flip", "log_loss"] == pytest.approx(0.693147, abs=1e-5)
    assert models.loc["Elo (chosen)", "log_loss"] < models.loc["Coin flip", "log_loss"]
    assert set(report.by_season["split"]) == {"train", "holdout"}
    assert "2004-05" not in set(report.by_season["season"])  # burn-in is never scored
    chosen_cal = report.calibration[report.calibration["model"] == "Elo (chosen)"]
    assert chosen_cal["n"].sum() == models.loc["Elo (chosen)", "n"]
    # Every model is scored per season, so the site can compare them.
    assert {"Elo (chosen)", "Coin flip"} <= set(report.by_season["model"])


def test_previous_season_home_rate_baseline():
    games = league(days_per_season=10)
    from nbaforecast.models import elo

    preds = backtest.add_baselines(elo.run(games)[0])
    first = preds[preds.season == "2004-05"]["p_home_rate"]
    assert (first == 0.5).all()  # no previous season
    prior_rate = preds[preds.season == "2004-05"]["home_win"].mean()
    assert (preds[preds.season == "2005-06"]["p_home_rate"] == prior_rate).all()


def test_write_outputs(tmp_path):
    report = backtest.run(league(seed=2), CANDIDATES)
    backtest.write(report, tmp_path)
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["champion"] == report.champion.as_dict()
    assert summary["holdout_seasons"] == ["2020-21", "2025-26"]
    for name in ["by_season.csv", "calibration.csv", "tuning_top25.csv", "README.md"]:
        assert (tmp_path / name).stat().st_size > 0
    assert "| model |" in (tmp_path / "README.md").read_text()
