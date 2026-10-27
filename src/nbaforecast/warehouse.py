"""Read modeled tables from the local DuckDB warehouse built by `nbaf dbt build`."""

from pathlib import Path

import duckdb
import pandas as pd

from nbaforecast import config

GAME_COLUMNS = """
    game_id, season, season_type, game_date, tipoff_utc,
    home_team_id, home_team_abbr, home_team_name,
    away_team_id, away_team_abbr, away_team_name,
    home_score, away_score, is_neutral_site
"""


def warehouse_path(data_dir: Path | None = None) -> Path:
    return (data_dir or config.local_data_dir()) / "warehouse.duckdb"


def load_games(data_dir: Path | None = None) -> pd.DataFrame:
    """Every completed competitive game (fct_games), oldest first."""
    path = warehouse_path(data_dir)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found: run `nbaf dbt build` first")
    with duckdb.connect(str(path), read_only=True) as con:
        df = con.sql(f"select {GAME_COLUMNS} from fct_games order by tipoff_utc, game_id").df()
    df["game_date"] = pd.to_datetime(df["game_date"]).dt.date
    return df


def _query(sql: str, data_dir: Path | None = None) -> pd.DataFrame:
    path = warehouse_path(data_dir)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found: run `nbaf dbt build` first")
    with duckdb.connect(str(path), read_only=True) as con:
        return con.sql(sql).df()


def load_player_games(data_dir: Path | None = None) -> pd.DataFrame:
    """One row per player per game (fct_player_games), only the columns the models use."""
    df = _query(
        """
        select p.game_id, p.season, p.game_date, g.tipoff_utc, p.team_id, p.is_home,
               p.player_id, p.player, p.played, p.did_not_play, p.is_starter, p.stats_missing,
               -- Available before tip-off: played, or a healthy scratch (coach's decision; blank
               -- reasons in 2004-08 were also active players). Injured, resting, suspended, or
               -- away from the team = unavailable.
               p.played or (p.did_not_play and (coalesce(p.dnp_reason, '') = ''
                   or upper(p.dnp_reason) like '%COACH%')) as available,
               p.minutes, p.game_score::double as game_score, p.plus_minus
        from fct_player_games as p
        inner join fct_games as g using (game_id)
        order by g.tipoff_utc, p.game_id, p.team_id, p.player_id
        """,
        data_dir,
    )
    df["game_date"] = pd.to_datetime(df["game_date"]).dt.date
    return df


def load_team_games(data_dir: Path | None = None) -> pd.DataFrame:
    """Per-team box-score efficiency and four factors (fct_team_games), oldest first."""
    df = _query(
        """
        select game_id, team_id, season, game_date, net_rating, possessions, minutes,
               efg_pct - opp_efg_pct as efg_diff, opp_tov_rate - tov_rate as tov_diff,
               oreb_rate - opp_oreb_rate as oreb_diff, ft_rate - opp_ft_rate as ft_diff
        from fct_team_games
        order by game_date, game_id, team_id
        """,
        data_dir,
    )
    df["game_date"] = pd.to_datetime(df["game_date"]).dt.date
    return df
