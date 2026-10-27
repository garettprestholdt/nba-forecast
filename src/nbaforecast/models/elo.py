"""Elo ratings for NBA teams, following FiveThirtyEight's NBA design with two additions.

- Expected result: P(home win) = 1 / (1 + 10^(-(R_home + HCA - R_away) / 400)).
- Update: both teams move by K * MOV multiplier * (result - expected), so ratings stay zero-sum.
- Margin of victory: blowouts move ratings more, but less so when the favorite wins (the
  autocorrelation correction that keeps strong teams' ratings from inflating).
- Between seasons, every rating regresses part of the way back to the mean.

Additions:
- Predictions for a date use the ratings as of the start of that day, so a game is never
  predicted with information from another game that may not have finished yet. This is
  exactly how the daily pipeline publishes predictions each morning.
- Optional adaptive home-court advantage: the Elo equivalent of the home win rate over the
  last N non-neutral games, because home court has been shrinking (60.5% in 2004-05, 54.4%
  in 2024-25). Neutral-site games (bubble, international, NBA Cup knockouts) get none.
"""

import math
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass

import pandas as pd

REQUIRED_COLUMNS = {
    "game_id",
    "season",
    "season_type",
    "game_date",
    "tipoff_utc",
    "home_team_id",
    "away_team_id",
    "home_score",
    "away_score",
    "is_neutral_site",
}


@dataclass(frozen=True)
class EloParams:
    k: float = 20.0
    home_advantage: float = 100.0  # Elo points; the prior when hca_window is set
    hca_window: int | None = None  # None = fixed home_advantage
    carryover: float = 0.75  # share of a rating kept between seasons
    mov: bool = True
    mean: float = 1500.0

    def label(self) -> str:
        hca = f"adaptive{self.hca_window}" if self.hca_window else f"{self.home_advantage:g}"
        return f"k={self.k:g} hca={hca} carry={self.carryover:g} mov={self.mov}"

    def as_dict(self) -> dict:
        return asdict(self)


def win_probability(rating_diff: float) -> float:
    """P(win) for a team rated `rating_diff` points above its opponent."""
    return 1.0 / (1.0 + 10.0 ** (-rating_diff / 400.0))


def elo_from_probability(p: float) -> float:
    """Inverse of win_probability: the rating gap that implies win probability p."""
    p = min(max(p, 1e-6), 1 - 1e-6)
    return 400.0 * math.log10(p / (1 - p))


def mov_multiplier(margin: int, winner_rating_diff: float) -> float:
    """FiveThirtyEight's margin-of-victory multiplier.

    `winner_rating_diff` is the winner's pre-game rating edge (including home court). The
    bigger the favorite, the less a win by the same margin counts.
    """
    return (abs(margin) + 3) ** 0.8 / (7.5 + 0.006 * winner_rating_diff)


class _HomeCourt:
    """Fixed or adaptive home-court advantage, in Elo points."""

    def __init__(self, params: EloParams):
        self.params = params
        self.results: deque[int] = deque(maxlen=params.hca_window or 1)

    def value(self) -> float:
        window = self.params.hca_window
        if not window or len(self.results) < window:
            return self.params.home_advantage
        return elo_from_probability(sum(self.results) / len(self.results))

    def record(self, home_won: bool) -> None:
        if self.params.hca_window:
            self.results.append(int(home_won))


def run(games: pd.DataFrame, params: EloParams | None = None) -> tuple[pd.DataFrame, dict]:
    """Walk forward through `games` and return (pre-game predictions, final ratings).

    Every prediction uses only games from earlier dates.
    """
    params = params or EloParams()
    missing = REQUIRED_COLUMNS - set(games.columns)
    if missing:
        raise ValueError(f"games is missing columns {sorted(missing)}")

    ordered = games.sort_values(["tipoff_utc", "game_id"])
    ratings: dict[str, float] = defaultdict(lambda: params.mean)
    home_court = _HomeCourt(params)
    season = None
    rows = []

    for game_date, day in ordered.groupby("game_date", sort=True):
        day_games = list(day.itertuples(index=False))
        day_season = day_games[0].season
        if day_season != season:
            if season is not None:
                for team in ratings:
                    ratings[team] = params.mean + params.carryover * (ratings[team] - params.mean)
            season = day_season

        # 1. Predict every game on this date from start-of-day ratings.
        hca_today = home_court.value()
        predictions = []
        for g in day_games:
            home_elo, away_elo = ratings[g.home_team_id], ratings[g.away_team_id]
            hca = 0.0 if g.is_neutral_site else hca_today
            p_home = win_probability(home_elo + hca - away_elo)
            predictions.append((g, home_elo, away_elo, hca, p_home))
            rows.append(
                {
                    "game_id": g.game_id,
                    "season": g.season,
                    "season_type": g.season_type,
                    "game_date": game_date,
                    "home_team_id": g.home_team_id,
                    "away_team_id": g.away_team_id,
                    "home_elo_pre": home_elo,
                    "away_elo_pre": away_elo,
                    "home_court": hca,
                    "p_home": p_home,
                    "home_win": int(g.home_score > g.away_score),
                    "is_neutral_site": bool(g.is_neutral_site),
                }
            )

        # 2. Then apply the results. `+=` so a team listed twice on one date (a known ESPN
        #    data issue) gets both updates.
        for g, home_elo, away_elo, hca, p_home in predictions:
            margin = int(g.home_score - g.away_score)
            home_won = margin > 0
            mult = 1.0
            if params.mov:
                edge = home_elo + hca - away_elo
                mult = mov_multiplier(margin, edge if home_won else -edge)
            delta = params.k * mult * (int(home_won) - p_home)
            ratings[g.home_team_id] += delta
            ratings[g.away_team_id] -= delta
            if not g.is_neutral_site:
                home_court.record(home_won)

    return pd.DataFrame(rows), dict(ratings)


def grid(**options: Iterable) -> list[EloParams]:
    """Every combination of the given parameter values, e.g. grid(k=[15, 20], carryover=[0.75])."""
    combos = [{}]
    for name, values in options.items():
        combos = [{**c, name: v} for c in combos for v in values]
    return [EloParams(**c) for c in combos]
