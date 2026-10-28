from datetime import UTC, datetime

import pytest

from nbaforecast import live, projections
from nbaforecast.models.elo import EloState

TEAMS = projections.franchises()
CONF = {t: row["conference"] for t, row in TEAMS.items()}


def round_robin(games_per_team=10):
    """A small schedule: every team plays its neighbors in id order, home and away."""
    ids = sorted(TEAMS, key=int)
    games = []
    for i, home in enumerate(ids):
        for k in range(1, games_per_team // 2 + 1):
            away = ids[(i + k) % len(ids)]
            games.append({"game_id": f"{home}-{away}", "home_team_id": home,
                          "away_team_id": away, "neutral": False, "completed": False})  # fmt: skip
    return games


def run(ratings, standings=None, remaining=None, sims=2000):
    standings = standings or {t: {"wins": 0, "losses": 0} for t in TEAMS}
    return projections.simulate(
        ratings, 60.0, 20.0, standings, remaining if remaining is not None else round_robin(),
        CONF, 1505.0, sims=sims, seed=1,
    )  # fmt: skip


def test_totals_add_up():
    out = run({t: 1505.0 for t in TEAMS})
    assert sum(v["proj_wins"] for v in out.values()) == pytest.approx(30 * 82 / 2, abs=1)
    assert sum(v["playoffs"] for v in out.values()) == pytest.approx(16)
    assert sum(v["playin"] for v in out.values()) == pytest.approx(8)
    assert sum(v["finals"] for v in out.values()) == pytest.approx(2)
    assert sum(v["title"] for v in out.values()) == pytest.approx(1)
    assert sum(v["top_seed"] for v in out.values()) == pytest.approx(2)


def test_better_teams_project_better():
    ratings = {t: 1505.0 for t in TEAMS}
    ratings["2"], ratings["17"] = 1750.0, 1300.0  # a juggernaut and a doormat
    out = run(ratings)
    assert out["2"]["proj_wins"] > 60 and out["17"]["proj_wins"] < 25
    assert out["2"]["title"] > 0.2 and out["17"]["title"] < 0.01
    assert out["2"]["rating"] == pytest.approx((1750 - 1505) / projections.ELO_PER_POINT, abs=0.01)
    assert out["2"]["wins_p10"] <= out["2"]["proj_wins"] <= out["2"]["wins_p90"]


def test_finished_season_matches_the_standings():
    standings = {t: {"wins": 41, "losses": 41} for t in TEAMS}
    standings["2"] = {"wins": 70, "losses": 12}
    out = run({t: 1505.0 for t in TEAMS}, standings, remaining=[])
    assert out["2"]["proj_wins"] == 70 and out["2"]["wins_p10"] == out["2"]["wins_p90"] == 70
    assert out["2"]["top_seed"] == 1.0 and out["2"]["playoffs"] == 1.0


def test_unscheduled_games_fill_the_season_to_82():
    standings = {t: {"wins": 1, "losses": 1} for t in TEAMS}
    out = run({t: 1505.0 for t in TEAMS}, standings, remaining=[])
    assert sum(v["proj_wins"] for v in out.values()) == pytest.approx(30 * 82 / 2, abs=1)


def test_project_writes_files(tmp_path):
    store = live.Store(tmp_path)
    state = EloState(live.LIVE_PARAMS, ratings={t: 1505.0 for t in TEAMS}, season="2026-27")
    store.save_state(state)
    schedule = [{**g, "tipoff_utc": "2026-11-01T23:30:00Z"} for g in round_robin(4)]
    payload = projections.project(
        tmp_path, {t: {"wins": 1, "losses": 1} for t in TEAMS}, schedule,
        datetime(2026, 10, 28, 14, tzinfo=UTC), sims=500,
    )  # fmt: skip
    assert projections.projections_path(tmp_path, "2026-27").exists()
    assert len(live.Store.read(projections.schedule_path(tmp_path, "2026-27"))) == len(schedule)
    assert payload["teams"]["2"]["wins"] == 1 and payload["simulations"] == 500
