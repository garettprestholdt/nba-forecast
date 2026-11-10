"""Hybrid v2 on fresh seasons: play-by-play back to 2004-05.

The v2 features were chosen on the 2020-21..2025-26 holdout (`impact_backtest.py`), after
looking at ten candidates there, so that holdout's score is optimistic. With stints from every
season since 2004-05, RAPM exists from 2005-06, and 2007-08..2016-17 are seasons no v2 choice
ever saw. Everything is fit walk-forward from 2005-06 exactly as in `impact_backtest.walk`:
each season predicted by a fit on every earlier season.

1. Fresh seasons: v2 vs v1 (realistic and knows-the-lineup), the team stack and Elo.
2. The usual holdout: v2 trained from 2005-06 instead of 2018-19.
3. RAPM window: the last 2 seasons' stints (as live) vs 3 and 4, on both.

The hybrid's player and form settings were tuned on walk-forward predictions for 2007-08..
2018-19 (`hybrid_backtest.py`), so the fresh seasons are fresh for v2's features, not for those.
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from nbaforecast import config
from nbaforecast.models import hybrid, impact, players
from nbaforecast.pipeline import shadow_v2
from nbaforecast.pipeline.live import LIVE_PARAMS
from nbaforecast.research import backtest, impact_backtest, rapm_walk
from nbaforecast.research.challenger import paired_log_loss_difference

log = logging.getLogger(__name__)

OUT_DIR = config.PUBLISHED_DIR / "impact_history"
STINT_SEASONS = backtest.seasons(2004, 2025)  # 2004-05 .. 2025-26
FIRST = "2005-06"  # the first season with a previous season's stints
FRESH = backtest.seasons(2007, 2016)  # 2007-08 .. 2016-17
HOLDOUT = backtest.HOLDOUT_SEASONS
WINDOWS = (2, 3, 4)

TEAM = hybrid.TEAM_FEATURES
V1 = TEAM + impact_backtest.CANDIDATES["Hybrid v1, realistic (current challenger)"]
V1_LINEUP = TEAM + impact_backtest.CANDIDATES["Hybrid v1, knows the lineup"]
ELO = "Elo, adaptive home court (published)"
V1_NAME = "Hybrid v1, realistic"
V2_LIVE = "Hybrid v2, trained from 2018-19 (as live)"


def build(games, team_games, player_games, box_rows, data_dir: Path) -> dict[int, pd.DataFrame]:
    """Features for every game from 2005-06, once per RAPM window."""
    best = impact_backtest.chosen()
    stints = rapm_walk.load_stints(data_dir, STINT_SEASONS, games)
    seasons = [s for s in STINT_SEASONS if s >= FIRST]
    out = {}
    for window in WINDOWS:
        ratings = rapm_walk.walk_ratings(stints, box_rows, games, seasons, window)
        pf = players.walk(player_games, best["players"], ratings=ratings, unknown=impact.UNKNOWN)
        f = hybrid.build_features(games, team_games, pf, LIVE_PARAMS, best["form_decay"])
        out[window] = f.assign(
            elo_x_turnover_c=f["elo_strength"] * (f["turnover"] - hybrid.TURNOVER_CENTER)
        )
        log.info("features with a %d-season RAPM window", window)
    out[WINDOWS[0]].attrs["stint_games"] = stints.groupby("season")["game_id"].nunique().to_dict()
    return out


def _walk(feats: pd.DataFrame, names: list[str], first: str = FIRST) -> pd.DataFrame:
    p, _ = impact_backtest.walk(feats, names, impact_backtest.chosen()["fit"], first)
    return p.set_index("game_id")


def compare(preds: dict[str, pd.DataFrame], seasons: list[str], base: str) -> dict:
    """Scores on the games every model predicted in `seasons`, each paired against `base`
    (positive = better than `base`), and log-loss by season."""
    ids = sorted(set.intersection(
        *(set(p.index[p["season"].isin(seasons)]) for p in preds.values())))  # fmt: skip
    y = preds[base].loc[ids, "home_win"]
    models, by_season = [], []
    for name, p in preds.items():
        h = p.loc[ids]
        models.append({"model": name, **backtest.score(h)})
        by_season += [{"season": s, "model": name, **backtest.score(g)}
                      for s, g in h.groupby("season")]  # fmt: skip
    paired = {
        name: paired_log_loss_difference(preds[base].loc[ids, "p_home"], p.loc[ids, "p_home"], y)
        for name, p in preds.items() if name != base
    }  # fmt: skip
    return {"games": len(ids), "models": pd.DataFrame(models),
            "by_season": pd.DataFrame(by_season), "paired": paired}  # fmt: skip


def run(feats: dict[int, pd.DataFrame]) -> dict:
    live = feats[2]
    rows = live[live["season"] >= FIRST]
    v2 = {w: _walk(f, shadow_v2.FEATURES) for w, f in feats.items()}
    fresh = {
        ELO: rows.set_index("game_id"),
        "Team stack (Elo + rest + form)": _walk(rows, TEAM),
        V1_NAME: _walk(rows, V1),
        "Hybrid v1, knows the lineup": _walk(rows, V1_LINEUP),
        "Hybrid v2": v2[2],
    }
    fresh |= {f"Hybrid v2, {w}-season RAPM": v2[w] for w in WINDOWS[1:]}
    holdout = {
        ELO: rows.set_index("game_id"),
        V2_LIVE: _walk(live, shadow_v2.FEATURES, impact_backtest.FIRST_RAPM_SEASON),
        "Hybrid v2, trained from 2005-06": v2[2],
    }
    holdout |= {f"Hybrid v2, trained from 2005-06, {w}-season RAPM": v2[w] for w in WINDOWS[1:]}
    return {
        "fresh": compare(fresh, FRESH, V1_NAME),
        "fresh_vs_elo": compare(fresh, FRESH, ELO)["paired"],
        "holdout": compare(holdout, HOLDOUT, V2_LIVE),
        "stint_games": live.attrs.get("stint_games"),
    }


def write(result: dict, out_dir: Path = OUT_DIR) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("fresh", "holdout"):
        result[name]["models"].to_csv(out_dir / f"{name}.csv", index=False)
        result[name]["by_season"].to_csv(out_dir / f"{name}_by_season.csv", index=False)
    summary = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "fresh_seasons": FRESH, "fresh_games": result["fresh"]["games"],
        "holdout_seasons": HOLDOUT, "holdout_games": result["holdout"]["games"],
        "stint_games": result["stint_games"],
        "paired_vs_v1_fresh": result["fresh"]["paired"],
        "paired_vs_elo_fresh": result["fresh_vs_elo"],
        "paired_vs_live_v2_holdout": result["holdout"]["paired"],
    }  # fmt: skip
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
