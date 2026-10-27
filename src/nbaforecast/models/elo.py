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
from collections import deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date

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
    rest_penalty: float = 0.0  # Elo points lost on the second night of a back-to-back

    def label(self) -> str:
        hca = f"adaptive{self.hca_window}" if self.hca_window else f"{self.home_advantage:g}"
        rest = f" rest={self.rest_penalty:g}" if self.rest_penalty else ""
        return f"k={self.k:g} hca={hca} carry={self.carryover:g} mov={self.mov}{rest}"

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


@dataclass
class GameForecast:
    """A pre-game prediction, kept so the matching update uses exactly the same numbers."""

    home_team_id: str
    away_team_id: str
    home_elo: float
    away_elo: float
    home_court: float
    p_home: float
    is_neutral_site: bool
    rest_adjust: float = 0.0  # Elo points added to the home side for back-to-backs


class EloState:
    """Everything needed to continue rating games: resumable from JSON between daily runs.

    Use it one date at a time: `start_day`, then `predict` every game on that date, then
    `update` with each result. Predictions never see results from the same date.
    """

    def __init__(
        self,
        params: EloParams,
        ratings: dict[str, float] | None = None,
        season: str | None = None,
        home_results: Iterable[int] = (),
        last_date: date | None = None,
        last_played: dict[str, date] | None = None,
    ):
        self.params = params
        self.last_played: dict[str, date] = dict(last_played or {})
        self.ratings: dict[str, float] = dict(ratings or {})
        self.season = season
        self.home_results: deque[int] = deque(home_results, maxlen=params.hca_window or 1)
        self.last_date = last_date

    def rating(self, team_id: str) -> float:
        return self.ratings.get(team_id, self.params.mean)

    def home_court(self) -> float:
        """Fixed bonus, or the Elo equivalent of the recent home win rate once the window fills."""
        window = self.params.hca_window
        if not window or len(self.home_results) < window:
            return self.params.home_advantage
        return elo_from_probability(sum(self.home_results) / len(self.home_results))

    def start_day(self, season: str) -> None:
        """Regress every rating toward the mean when a new season starts."""
        if season != self.season:
            if self.season is not None:
                p = self.params
                self.ratings = {
                    team: p.mean + p.carryover * (r - p.mean) for team, r in self.ratings.items()
                }
            self.season = season

    def back_to_back(self, team_id: str, game_date: date | None) -> bool:
        last = self.last_played.get(team_id)
        return game_date is not None and last is not None and (game_date - last).days == 1

    def predict(
        self,
        home_team_id: str,
        away_team_id: str,
        is_neutral_site: bool,
        game_date: date | None = None,
    ) -> GameForecast:
        home_elo, away_elo = self.rating(home_team_id), self.rating(away_team_id)
        hca = 0.0 if is_neutral_site else self.home_court()
        rest = self.params.rest_penalty * (
            int(self.back_to_back(away_team_id, game_date))
            - int(self.back_to_back(home_team_id, game_date))
        )
        return GameForecast(
            home_team_id,
            away_team_id,
            home_elo,
            away_elo,
            hca,
            win_probability(home_elo + hca - away_elo + rest),
            bool(is_neutral_site),
            rest,
        )

    def record_played(self, team_ids: Iterable[str], game_date: date) -> None:
        for team in team_ids:
            self.last_played[team] = game_date

    def update(self, forecast: GameForecast, home_score: int, away_score: int) -> None:
        """Apply a result. `+=` so a team listed twice on one date gets both updates."""
        f = forecast
        margin = int(home_score - away_score)
        home_won = margin > 0
        mult = 1.0
        if self.params.mov:
            edge = f.home_elo + f.home_court - f.away_elo + f.rest_adjust
            mult = mov_multiplier(margin, edge if home_won else -edge)
        delta = self.params.k * mult * (int(home_won) - f.p_home)
        self.ratings[f.home_team_id] = self.rating(f.home_team_id) + delta
        self.ratings[f.away_team_id] = self.rating(f.away_team_id) - delta
        if not f.is_neutral_site and self.params.hca_window:
            self.home_results.append(int(home_won))

    def to_dict(self) -> dict:
        return {
            "params": self.params.as_dict(),
            "season": self.season,
            "last_date": self.last_date.isoformat() if self.last_date else None,
            "home_court_now": self.home_court(),
            "home_results": "".join(map(str, self.home_results)),
            "last_played": {t: d.isoformat() for t, d in sorted(self.last_played.items())},
            "ratings": dict(
                sorted(
                    self.ratings.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else kv[0]
                )
            ),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EloState":
        return cls(
            params=EloParams(**data["params"]),
            ratings=data["ratings"],
            season=data["season"],
            home_results=[int(c) for c in data["home_results"]],
            last_date=date.fromisoformat(data["last_date"]) if data["last_date"] else None,
            last_played={t: date.fromisoformat(d) for t, d in data.get("last_played", {}).items()},
        )


def walk(games: pd.DataFrame, state: EloState) -> pd.DataFrame:
    """Advance `state` through `games` date by date; return the pre-game predictions."""
    missing = REQUIRED_COLUMNS - set(games.columns)
    if missing:
        raise ValueError(f"games is missing columns {sorted(missing)}")

    rows = []
    for game_date, day in games.sort_values(["tipoff_utc", "game_id"]).groupby("game_date"):
        day_games = list(day.itertuples(index=False))
        state.start_day(day_games[0].season)
        # 1. Predict every game on this date from start-of-day ratings...
        forecasts = [
            state.predict(g.home_team_id, g.away_team_id, g.is_neutral_site, game_date)
            for g in day_games
        ]
        # 2. ...then apply the results.
        for g, f in zip(day_games, forecasts, strict=True):
            state.update(f, g.home_score, g.away_score)
            rows.append(
                {
                    "game_id": g.game_id,
                    "season": g.season,
                    "season_type": g.season_type,
                    "game_date": game_date,
                    "home_team_id": g.home_team_id,
                    "away_team_id": g.away_team_id,
                    "home_elo_pre": f.home_elo,
                    "away_elo_pre": f.away_elo,
                    "home_court": f.home_court,
                    "p_home": f.p_home,
                    "home_win": int(g.home_score > g.away_score),
                    "is_neutral_site": f.is_neutral_site,
                }
            )
        for g in day_games:
            state.record_played((g.home_team_id, g.away_team_id), game_date)
        state.last_date = game_date
    return pd.DataFrame(rows)


def run(games: pd.DataFrame, params: EloParams | None = None) -> tuple[pd.DataFrame, dict]:
    """Walk forward through `games` from scratch: (pre-game predictions, final ratings)."""
    state = EloState(params or EloParams())
    preds = walk(games, state)
    return preds, dict(state.ratings)


def grid(**options: Iterable) -> list[EloParams]:
    """Every combination of the given parameter values, e.g. grid(k=[15, 20], carryover=[0.75])."""
    combos = [{}]
    for name, values in options.items():
        combos = [{**c, name: v} for c in combos for v in values]
    return [EloParams(**c) for c in combos]
