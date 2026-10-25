"""Scoring rules for probabilistic forecasts of binary outcomes (home team wins = 1)."""

import math
from collections.abc import Sequence

# Probabilities are clipped away from 0 and 1 so one overconfident miss gives a large but finite
# log-loss instead of infinity.
EPS = 1e-15


def _validate(probs: Sequence[float], outcomes: Sequence[int]) -> None:
    if len(probs) != len(outcomes):
        raise ValueError(f"length mismatch: {len(probs)} probs vs {len(outcomes)} outcomes")
    if not probs:
        raise ValueError("need at least one forecast")
    if any(not 0.0 <= p <= 1.0 for p in probs):
        raise ValueError("probabilities must be in [0, 1]")
    if any(y not in (0, 1) for y in outcomes):
        raise ValueError("outcomes must be 0 or 1")


def log_loss(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    """Mean negative log-likelihood. A coin flip (p=0.5 everywhere) scores ln 2 ≈ 0.693."""
    _validate(probs, outcomes)
    total = 0.0
    for p, y in zip(probs, outcomes, strict=True):
        p = min(max(p, EPS), 1 - EPS)
        total += -math.log(p) if y == 1 else -math.log(1 - p)
    return total / len(probs)


def brier_score(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    """Mean squared error of the probabilities. A coin flip scores 0.25; perfect is 0."""
    _validate(probs, outcomes)
    return sum((p - y) ** 2 for p, y in zip(probs, outcomes, strict=True)) / len(probs)


def devig_two_way(home_decimal_odds: float, away_decimal_odds: float) -> tuple[float, float]:
    """Market-implied (home, away) win probabilities with the bookmaker margin removed.

    Uses proportional normalization: the raw implied probabilities 1/odds sum to slightly more
    than 1 (the "vig"), so each is divided by that sum.
    """
    if home_decimal_odds <= 1 or away_decimal_odds <= 1:
        raise ValueError("decimal odds must be greater than 1")
    raw_home, raw_away = 1 / home_decimal_odds, 1 / away_decimal_odds
    total = raw_home + raw_away
    return raw_home / total, raw_away / total
