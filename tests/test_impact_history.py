import math

import pandas as pd
import pytest

from nbaforecast.research import impact_history


def test_compare_pairs_models_on_shared_games_only():
    base = pd.DataFrame({"game_id": ["a", "b", "c", "d"], "season": ["2008-09"] * 3 + ["2020-21"],
                         "home_win": [1, 0, 1, 1], "p_home": [0.6, 0.4, 0.6, 0.6]})  # fmt: skip
    base = base.set_index("game_id")
    better = base.assign(p_home=[0.7, 0.3, 0.7, 0.5]).drop("c")  # didn't predict game c
    out = impact_history.compare({"base": base, "better": better}, ["2008-09"], "base")
    assert out["games"] == 2  # a and b: both predicted, and in the window
    assert set(out["models"]["n"]) == {2}
    # positive = better than base: each game's log-loss falls from -ln 0.6 to -ln 0.7
    assert out["paired"]["better"]["mean_difference"] == pytest.approx(
        math.log(0.7) - math.log(0.6), abs=1e-4
    )
