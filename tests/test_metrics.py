import math

import pytest

from nbaforecast.metrics import (
    accuracy,
    brier_decomposition,
    brier_score,
    calibration_table,
    devig_two_way,
    log_loss,
)


def test_coin_flip_baselines():
    probs, outcomes = [0.5, 0.5, 0.5, 0.5], [1, 0, 1, 1]
    assert log_loss(probs, outcomes) == pytest.approx(math.log(2))
    assert brier_score(probs, outcomes) == pytest.approx(0.25)


def test_perfect_forecast():
    assert brier_score([1.0, 0.0], [1, 0]) == 0.0
    assert log_loss([1.0, 0.0], [1, 0]) == pytest.approx(0.0, abs=1e-12)


def test_confident_miss_is_finite_but_large():
    loss = log_loss([1.0], [0])
    assert math.isfinite(loss)
    assert loss > 30


def test_known_values():
    # One home win forecast at 0.7, one home loss forecast at 0.4.
    assert log_loss([0.7, 0.4], [1, 0]) == pytest.approx((-math.log(0.7) - math.log(0.6)) / 2)
    assert brier_score([0.7, 0.4], [1, 0]) == pytest.approx((0.09 + 0.16) / 2)


@pytest.mark.parametrize(
    ("probs", "outcomes"),
    [([0.5], [1, 0]), ([], []), ([1.2], [1]), ([0.5], [2])],
)
def test_invalid_inputs_raise(probs, outcomes):
    with pytest.raises(ValueError):
        log_loss(probs, outcomes)


def test_devig_removes_margin():
    # -110 / -110 in American odds = 1.909 decimal on both sides: a fair 50/50 after de-vigging.
    home, away = devig_two_way(1.909, 1.909)
    assert home == pytest.approx(0.5)
    assert home + away == pytest.approx(1.0)


def test_devig_favorite():
    home, away = devig_two_way(1.5, 2.6)
    assert home > away
    assert home + away == pytest.approx(1.0)


def test_devig_rejects_bad_odds():
    with pytest.raises(ValueError):
        devig_two_way(1.0, 2.0)


def test_accuracy_half_credit_for_coin_flips():
    assert accuracy([0.7, 0.3, 0.5], [1, 1, 0]) == pytest.approx((1 + 0 + 0.5) / 3)


def test_calibration_table_bins():
    probs = [0.05, 0.15, 0.12, 0.95, 1.0]
    outcomes = [0, 0, 1, 1, 1]
    table = calibration_table(probs, outcomes, bins=10)
    assert sum(r["n"] for r in table) == 5
    second = next(r for r in table if r["bin_low"] == 0.1)
    assert second["n"] == 2 and second["observed_rate"] == 0.5
    top = table[-1]
    assert top["bin_high"] == 1.0 and top["n"] == 2  # p = 1.0 lands in the last bin


def test_brier_decomposition_adds_up():
    probs = [0.2, 0.2, 0.8, 0.8, 0.8, 0.5]
    outcomes = [0, 1, 1, 1, 0, 1]
    d = brier_decomposition(probs, outcomes)
    assert d["reliability"] - d["resolution"] + d["uncertainty"] + d["within_bin"] == (
        pytest.approx(d["brier"])
    )
    # Forecasts constant within each bin -> the decomposition is exact.
    assert d["within_bin"] == pytest.approx(0, abs=1e-12)


def test_perfectly_calibrated_has_zero_reliability():
    probs = [0.25] * 4 + [0.75] * 4
    outcomes = [1, 0, 0, 0, 1, 1, 1, 0]
    assert brier_decomposition(probs, outcomes)["reliability"] == pytest.approx(0)
