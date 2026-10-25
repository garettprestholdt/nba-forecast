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


def accuracy(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    """Share of games where the favored side won. A 50/50 forecast gets half credit."""
    _validate(probs, outcomes)
    credit = 0.0
    for p, y in zip(probs, outcomes, strict=True):
        credit += 0.5 if p == 0.5 else float((p > 0.5) == (y == 1))
    return credit / len(probs)


def _bin_index(p: float, bins: int) -> int:
    return min(int(p * bins), bins - 1)


def calibration_table(
    probs: Sequence[float], outcomes: Sequence[int], bins: int = 10
) -> list[dict[str, float]]:
    """Forecasts grouped into equal-width probability bins: mean forecast vs. observed rate.

    A calibrated model's games forecast at ~70% are won ~70% of the time.
    """
    _validate(probs, outcomes)
    groups: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for p, y in zip(probs, outcomes, strict=True):
        groups[_bin_index(p, bins)].append((p, y))
    table = []
    for i, group in enumerate(groups):
        if not group:
            continue
        table.append(
            {
                "bin_low": i / bins,
                "bin_high": (i + 1) / bins,
                "n": len(group),
                "mean_forecast": sum(p for p, _ in group) / len(group),
                "observed_rate": sum(y for _, y in group) / len(group),
            }
        )
    return table


def brier_decomposition(
    probs: Sequence[float], outcomes: Sequence[int], bins: int = 10
) -> dict[str, float]:
    """Murphy's decomposition: Brier ≈ reliability - resolution + uncertainty.

    - reliability: how far forecasts are from observed rates (lower is better; 0 = calibrated)
    - resolution: how much forecasts separate games with different outcomes (higher is better)
    - uncertainty: base-rate variance, the same for every model on the same games

    Binning makes this approximate; `within_bin` is the leftover term so the parts add up.
    """
    table = calibration_table(probs, outcomes, bins)
    n = len(probs)
    base_rate = sum(outcomes) / n
    reliability = sum(r["n"] * (r["mean_forecast"] - r["observed_rate"]) ** 2 for r in table) / n
    resolution = sum(r["n"] * (r["observed_rate"] - base_rate) ** 2 for r in table) / n
    uncertainty = base_rate * (1 - base_rate)
    brier = brier_score(probs, outcomes)
    return {
        "brier": brier,
        "reliability": reliability,
        "resolution": resolution,
        "uncertainty": uncertainty,
        "within_bin": brier - (reliability - resolution + uncertainty),
    }


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
