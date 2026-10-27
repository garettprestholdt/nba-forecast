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
