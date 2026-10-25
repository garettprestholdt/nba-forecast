import math

import pytest

from nbaforecast.metrics import brier_score, devig_two_way, log_loss


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
