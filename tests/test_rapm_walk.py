from datetime import date

import numpy as np
import pandas as pd

from nbaforecast.models import impact
from nbaforecast.models.players import PlayerParams, PlayerState
from nbaforecast.research import impact_backtest, rapm_walk


def test_snapshots_are_only_used_on_or_after_their_date():
    r = rapm_walk.Ratings([(date(2025, 11, 1), {"a": (1.0, 1.0)}),
                           (date(2025, 11, 15), {"a": (2.0, 2.0)})])  # fmt: skip
    assert r.at(date(2025, 10, 31)) == {}
    assert r.at(date(2025, 11, 1))["a"] == (1.0, 1.0)  # fit on games before Nov 1
    assert r.at(date(2025, 11, 14))["a"] == (1.0, 1.0)
    assert r.at(date(2025, 12, 1))["a"] == (2.0, 2.0)


def test_refit_dates_cover_each_season():
    games = pd.DataFrame({"season": ["2024-25"] * 3 + ["2025-26"],
                          "game_date": [date(2024, 10, 22), date(2024, 11, 30), date(2025, 1, 2),
                                        date(2025, 10, 21)]})  # fmt: skip
    got = rapm_walk.refit_dates(games, ["2024-25", "2025-26"])
    assert got[0] == ("2024-25", date(2024, 10, 22)) and got[-1] == ("2025-26", date(2025, 10, 21))
    pairs = zip(got, got[1:], strict=False)
    assert all((b - a).days == rapm_walk.REFIT_DAYS for (s1, a), (s2, b) in pairs if s1 == s2)


def test_missing_a_good_player_costs_value_above_replacement():
    state = PlayerState(PlayerParams())
    state.rosters["T"] = {"star", "bench"}
    state.players["star"].avail_num, state.players["star"].avail_den = 34.0, 1.0
    state.players["bench"].avail_num, state.players["bench"].avail_den = 10.0, 1.0
    ratings = {"star": (4.0, 1.0), "bench": (-1.0, -0.5)}
    full = state.rapm_features("T", ["star", "bench"], ratings, impact.UNKNOWN)
    hurt = state.rapm_features("T", ["bench"], ratings, impact.UNKNOWN)
    assert full["absent_rapm"] == 0 and full["absent_minutes"] == 0
    assert hurt["absent_rapm"] > 0 and hurt["absent_minutes"] == 34
    assert hurt["rapm_o_lineup"] < full["rapm_o_lineup"]
    # a missing replacement-level player costs nothing
    gone = state.rapm_features("T", ["star"], ratings, impact.UNKNOWN)
    assert gone["absent_rapm"] == 0


def test_vif_flags_a_copy():
    rng = np.random.default_rng(0)
    a = rng.normal(size=500)
    df = pd.DataFrame(
        {"a": a, "b": rng.normal(size=500), "a2": a + rng.normal(scale=0.05, size=500)}
    )
    v = impact_backtest.vif(df)
    assert v["a"] > 100 and v["a2"] > 100 and v["b"] < 1.5
