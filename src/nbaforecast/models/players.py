"""Pre-game player ratings and the team strengths they add up to.

Walks through every player box score date by date, like the Elo model: features for a game use
only games on earlier dates. For each player it keeps exponentially weighted sums of

* Game Score and minutes  -> production per minute, shrunk toward a replacement-level player;
* plus-minus and minutes  -> on-court margin per minute (from 2008-09; shrunk toward 0);
* minutes in each of their team's games, with 0 for games they sat or missed -> how much they
  are likely to play (availability), and minutes in games they played -> their usual role.

A team's strength before a game is its players' per-minute values weighted by projected minutes,
scaled to the 240 minutes every game has. Two versions:

* **realistic**: everyone on the roster, weighted by availability. Uses nothing from game day.
* **knows the lineup**: only the players available for this game (they played, or sat by the
  coach's choice; not injured, resting, or suspended), weighted by their usual minutes. That is
  known shortly before tip-off from injury reports and the inactive list. Nothing about how the
  game went is used: not who got minutes, not how many. The gap between the two versions shows
  what game-day availability is worth.
"""

from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

GAME_MINUTES = 240


@dataclass(frozen=True)
class PlayerParams:
    decay: float = 0.97  # weight a player's older games keep, per game they play
    carryover: float = 0.7  # weight last season's games keep at the start of a new season
    prior_minutes: float = 300.0  # shrinkage: minutes of replacement-level play to start from
    replacement: float = 0.20  # Game Score per minute of a replacement-level player
    pm_prior_minutes: float = 1500.0  # shrinkage for plus-minus per minute (toward 0)
    minutes_decay: float = 0.85  # weight older team games keep in projected minutes
    new_player_minutes: float = 10.0  # usual minutes for a player we've never seen play

    def label(self) -> str:
        return ", ".join(f"{k}={v:g}" for k, v in asdict(self).items())


class _Player:
    __slots__ = ("gs", "gs_min", "pm", "pm_min", "avail_num", "avail_den", "role_num",
                 "role_den", "team", "season_team", "last_season_team")  # fmt: skip

    def __init__(self) -> None:
        self.gs = self.gs_min = self.pm = self.pm_min = 0.0
        self.avail_num = self.avail_den = self.role_num = self.role_den = 0.0
        self.team: str | None = None
        self.season_team: dict[str, float] = defaultdict(float)  # minutes by team, this season
        self.last_season_team: str | None = None


class PlayerState:
    def __init__(self, params: PlayerParams) -> None:
        self.params = params
        self.players: dict[str, _Player] = defaultdict(_Player)
        self.rosters: dict[str, set[str]] = defaultdict(set)
        self.season: str | None = None

    # --- values ---------------------------------------------------------------------------
    def production(self, pid: str) -> float:
        """Game Score per minute, shrunk toward replacement level."""
        p, k = self.players[pid], self.params.prior_minutes
        return (p.gs + k * self.params.replacement) / (p.gs_min + k)

    def impact(self, pid: str) -> float:
        """Plus-minus per minute, shrunk toward 0."""
        p = self.players[pid]
        return p.pm / (p.pm_min + self.params.pm_prior_minutes)

    def availability(self, pid: str) -> float:
        p = self.players[pid]
        return p.avail_num / p.avail_den if p.avail_den else 0.0

    def role(self, pid: str) -> float:
        p = self.players[pid]
        return p.role_num / p.role_den if p.role_den else self.params.new_player_minutes

    # --- team strength --------------------------------------------------------------------
    def _strength(self, weights: dict[str, float], team: str) -> dict[str, float]:
        total = sum(weights.values())
        if total <= 0:
            return {"production": GAME_MINUTES * self.params.replacement, "impact": 0.0,
                    "continuity": 0.0}  # fmt: skip
        scale = GAME_MINUTES / total
        production = sum(self.production(p) * m for p, m in weights.items()) * scale
        impact = sum(self.impact(p) * m for p, m in weights.items()) * scale / 5
        stayed = sum(m for p, m in weights.items() if self.players[p].last_season_team == team)
        return {"production": production, "impact": impact, "continuity": stayed / total}

    def realistic(self, team: str) -> dict[str, float]:
        return self._strength({p: self.availability(p) for p in self.rosters[team]}, team)

    def knows_lineup(self, team: str, available: list[str]) -> dict[str, float]:
        return self._strength({p: self.role(p) for p in available}, team)

    # --- updates --------------------------------------------------------------------------
    def start_season(self, season: str) -> None:
        if season == self.season:
            return
        c = self.params.carryover
        for p in self.players.values():
            p.gs *= c
            p.gs_min *= c
            p.pm *= c
            p.pm_min *= c
            p.avail_num *= c
            p.avail_den *= c
            p.role_num *= c
            p.role_den *= c
            if p.season_team:
                p.last_season_team = max(p.season_team, key=p.season_team.get)
            p.season_team = defaultdict(float)
        self.season = season

    def record(self, team: str, rows: list[Any]) -> None:
        """Apply one team's box score (rows: namedtuples from load_player_games)."""
        d, md = self.params.decay, self.params.minutes_decay
        listed = set()
        for r in rows:
            pid = r.player_id
            listed.add(pid)
            p = self.players[pid]
            if p.team != team:
                if p.team is not None:
                    self.rosters[p.team].discard(pid)
                self.rosters[team].add(pid)
                p.team = team
            if r.stats_missing or not r.played or not r.minutes:
                continue
            p.gs = p.gs * d + r.game_score
            p.gs_min = p.gs_min * d + r.minutes
            if not pd.isna(r.plus_minus):
                p.pm = p.pm * d + r.plus_minus
                p.pm_min = p.pm_min * d + r.minutes
            p.role_num = p.role_num * md + r.minutes
            p.role_den = p.role_den * md + 1
            p.season_team[team] += r.minutes
        if any(r.stats_missing for r in rows):
            return  # minutes unknown: don't count this game toward availability
        minutes = {r.player_id: (r.minutes or 0) if r.played else 0 for r in rows}
        for pid in self.rosters[team] | listed:
            p = self.players[pid]
            p.avail_num = p.avail_num * md + minutes.get(pid, 0)
            p.avail_den = p.avail_den * md + 1


def walk(player_games: pd.DataFrame, params: PlayerParams | None = None) -> pd.DataFrame:
    """Pre-game team strengths for every team in every game: one row per (game_id, team_id)."""
    state = PlayerState(params or PlayerParams())
    out = []
    ordered = player_games.sort_values(["game_date", "tipoff_utc", "game_id", "team_id"])
    for _, day in ordered.groupby("game_date", sort=True):
        state.start_season(day["season"].iloc[0])
        team_games = [
            (game_id, team, list(rows.itertuples(index=False)))
            for (game_id, team), rows in day.groupby(["game_id", "team_id"], sort=False)
        ]
        # Features from start-of-day state for every game on this date...
        for game_id, team, rows in team_games:
            available = [r.player_id for r in rows if r.available]
            real, lineup = state.realistic(team), state.knows_lineup(team, available)
            out.append(
                {
                    "game_id": game_id,
                    "team_id": team,
                    "production_real": real["production"],
                    "impact_real": real["impact"],
                    "continuity": real["continuity"],
                    "production_lineup": lineup["production"],
                    "impact_lineup": lineup["impact"],
                }
            )
        # ...then the day's box scores.
        for _, team, rows in team_games:
            state.record(team, rows)
    return pd.DataFrame(out)
