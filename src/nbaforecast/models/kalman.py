"""A Bayesian team-strength model: a Kalman filter on game margins.

State (all in points, with a full covariance):
- one strength per franchise, drifting as a random walk between game days and partly reset
  between seasons
- home-court advantage, also drifting slowly (the Bayesian version of Elo's adaptive home court)
- coefficients for rest and travel, learned from the data like everything else

Each game observes  margin = s_home - s_away + home_court + b·x + noise,  with the margin capped
to limit garbage-time noise. The win probability is P(margin > 0) using both the observation
noise and the uncertainty in the state, so early-season forecasts are less confident.

As in the Elo model, every game on a date is predicted from start-of-day beliefs, and only then
are that date's results applied.
"""

import math
from dataclasses import asdict, dataclass
from datetime import date

import numpy as np
import pandas as pd

TEAMS = [str(i) for i in range(1, 31)]  # ESPN franchise ids
FEATURES = ["home_back_to_back", "away_back_to_back", "travel_gap_1000km"]
N_TEAMS = len(TEAMS)
HCA = N_TEAMS
N_STATE = N_TEAMS + 1 + len(FEATURES)
INDEX = {t: i for i, t in enumerate(TEAMS)}

# Home arena coordinates (lat, lon) by franchise, with the moves that matter since 2004.
ARENAS = {
    "1": (33.757, -84.396), "2": (42.366, -71.062), "3": (29.949, -90.082),
    "4": (41.881, -87.674), "5": (41.496, -81.688), "6": (32.790, -96.810),
    "7": (39.749, -105.008), "8": (42.341, -83.055), "9": (37.768, -122.388),
    "10": (29.751, -95.362), "11": (39.764, -86.155), "12": (33.945, -118.341),
    "13": (34.043, -118.267), "14": (25.781, -80.188), "15": (43.045, -87.917),
    "16": (44.980, -93.276), "17": (40.683, -73.976), "18": (40.751, -73.993),
    "19": (28.539, -81.384), "20": (39.901, -75.172), "21": (33.446, -112.071),
    "22": (45.532, -122.667), "23": (38.580, -121.500), "24": (29.427, -98.438),
    "25": (35.463, -97.515), "26": (40.768, -111.901), "27": (38.898, -77.021),
    "28": (43.643, -79.379), "29": (35.138, -90.051), "30": (35.225, -80.839),
}  # fmt: skip
SEATTLE = (47.622, -122.354)  # SuperSonics, through 2007-08
OKLAHOMA_CITY = (35.463, -97.515)  # Hornets after Hurricane Katrina, 2005-06 and 2006-07
ORLANDO_BUBBLE = (28.370, -81.560)


def home_city(team_id: str, season: str) -> tuple[float, float]:
    if team_id == "25" and season <= "2007-08":
        return SEATTLE
    if team_id == "3" and season in ("2005-06", "2006-07"):
        return OKLAHOMA_CITY
    return ARENAS[team_id]


def game_city(g) -> tuple[float, float]:
    if g.season == "2019-20" and g.game_date >= date(2020, 7, 30):
        return ORLANDO_BUBBLE
    # Other neutral sites (international games, NBA Cup in Las Vegas) are rare; the home
    # team's city stands in for them.
    return home_city(g.home_team_id, g.season)


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 6371 * 2 * math.asin(math.sqrt(h))


@dataclass(frozen=True)
class KalmanParams:
    sigma: float = 12.0  # margin noise (points)
    team_drift: float = 0.15  # sd of each team's daily random-walk step (points)
    season_var: float = 9.0  # variance added to every team at a new season (points²)
    carryover: float = 0.7  # share of a team's strength kept between seasons
    prior_var: float = 36.0  # initial team variance (points²)
    hca_prior: float = 3.0  # initial home-court advantage (points)
    hca_prior_var: float = 4.0
    hca_drift: float = 0.01  # sd of home court's daily random-walk step (points)
    feature_prior_var: float = 4.0  # prior variance of the rest/travel coefficients
    margin_cap: float = 30.0  # observed margins are clipped to ±cap
    use_features: bool = True

    def label(self) -> str:
        return (
            f"sigma={self.sigma:g} drift={self.team_drift:g} season_var={self.season_var:g} "
            f"carry={self.carryover:g} hca_drift={self.hca_drift:g} features={self.use_features}"
        )

    def as_dict(self) -> dict:
        return asdict(self)


def _phi(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


class KalmanState:
    def __init__(self, params: KalmanParams):
        self.params = params
        self.mean = np.zeros(N_STATE)
        self.mean[HCA] = params.hca_prior
        var = np.full(N_STATE, params.feature_prior_var)
        var[:N_TEAMS] = params.prior_var
        var[HCA] = params.hca_prior_var
        if not params.use_features:
            var[HCA + 1 :] = 0.0  # coefficients pinned at zero
        self.cov = np.diag(var)
        self.season: str | None = None
        self.day: date | None = None
        self.last_game: dict[str, tuple[date, tuple[float, float]]] = {}

    def advance_to(self, day: date, season: str) -> None:
        p = self.params
        if self.day is not None:
            days = (day - self.day).days
            self.cov[range(N_TEAMS), range(N_TEAMS)] += p.team_drift**2 * days
            self.cov[HCA, HCA] += p.hca_drift**2 * days
        if season != self.season:
            if self.season is not None:
                teams = slice(0, N_TEAMS)
                self.mean[teams] *= p.carryover
                self.cov[teams, :] *= p.carryover
                self.cov[:, teams] *= p.carryover
                self.cov[range(N_TEAMS), range(N_TEAMS)] += p.season_var
            self.season = season
        self.day = day

    def features(self, g) -> np.ndarray:
        city = game_city(g)
        x = np.zeros(len(FEATURES))
        travel = {}
        for side, team in (("home", g.home_team_id), ("away", g.away_team_id)):
            prev = self.last_game.get(team)
            rested_one_day = prev is not None and (g.game_date - prev[0]).days == 1
            x[0 if side == "home" else 1] = float(rested_one_day)
            travel[side] = (
                km(prev[1], city) if prev is not None and (g.game_date - prev[0]).days <= 3 else 0.0
            )
        x[2] = (travel["away"] - travel["home"]) / 1000
        return x

    def design(self, g, x: np.ndarray) -> np.ndarray:
        h = np.zeros(N_STATE)
        h[INDEX[g.home_team_id]] = 1.0
        h[INDEX[g.away_team_id]] = -1.0
        if not g.is_neutral_site:
            h[HCA] = 1.0
        h[HCA + 1 :] = x
        return h

    def predict(self, h: np.ndarray) -> tuple[float, float, float]:
        """(expected home margin, its sd including noise, P(home win))."""
        mu = float(h @ self.mean)
        sd = math.sqrt(float(h @ self.cov @ h) + self.params.sigma**2)
        return mu, sd, _phi(mu / sd)

    def update(self, h: np.ndarray, margin: float) -> None:
        cap = self.params.margin_cap
        y = max(-cap, min(cap, margin))
        ph = self.cov @ h
        s = float(h @ ph) + self.params.sigma**2
        self.mean = self.mean + ph * (y - float(h @ self.mean)) / s
        self.cov = self.cov - np.outer(ph, ph) / s

    def record_game(self, g) -> None:
        city = game_city(g)
        self.last_game[g.home_team_id] = (g.game_date, city)
        self.last_game[g.away_team_id] = (g.game_date, city)

    def ratings(self) -> dict[str, float]:
        return {t: float(self.mean[i]) for t, i in INDEX.items()}

    def coefficients(self) -> dict[str, float]:
        return {"home_court": float(self.mean[HCA])} | {
            name: float(self.mean[HCA + 1 + i]) for i, name in enumerate(FEATURES)
        }


def walk(games: pd.DataFrame, state: KalmanState) -> pd.DataFrame:
    """Advance `state` through `games` date by date; return pre-game predictions."""
    rows = []
    for game_date, day in games.sort_values(["tipoff_utc", "game_id"]).groupby("game_date"):
        day_games = list(day.itertuples(index=False))
        state.advance_to(game_date, day_games[0].season)
        prepared = []
        for g in day_games:
            x = state.features(g)
            h = state.design(g, x)
            mu, sd, p = state.predict(h)
            prepared.append((g, h))
            rows.append(
                {
                    "game_id": g.game_id,
                    "season": g.season,
                    "season_type": g.season_type,
                    "game_date": game_date,
                    "home_team_id": g.home_team_id,
                    "away_team_id": g.away_team_id,
                    "expected_margin": mu,
                    "margin_sd": sd,
                    "p_home": p,
                    "home_win": int(g.home_score > g.away_score),
                    "is_neutral_site": bool(g.is_neutral_site),
                    **dict(zip(FEATURES, x, strict=True)),
                }
            )
        for g, h in prepared:
            state.update(h, g.home_score - g.away_score)
        for g, _ in prepared:
            state.record_game(g)
    return pd.DataFrame(rows)


def run(
    games: pd.DataFrame, params: KalmanParams | None = None
) -> tuple[pd.DataFrame, KalmanState]:
    state = KalmanState(params or KalmanParams())
    return walk(games, state), state
