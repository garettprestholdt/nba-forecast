from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from nbaforecast.models import players
from nbaforecast.models.players import GAME_MINUTES, PlayerParams

ROSTERS = {"A": ["a1", "a2", "a3", "a4", "a5"], "B": ["b1", "b2", "b3", "b4", "b5"]}


def box(game_id, day, team, opp_home, *, gs=10.0, minutes=48, overrides=None, season="2020-21"):
    """One team's box score: five players, 48 minutes each unless overridden."""
    rows = []
    for pid in ROSTERS[team]:
        row = {
            "game_id": game_id, "season": season, "game_date": day,
            "tipoff_utc": datetime.combine(day, datetime.min.time(), UTC), "team_id": team,
            "is_home": opp_home, "player_id": pid, "player": pid, "played": True,
            "did_not_play": False, "is_starter": True, "stats_missing": False,
            "minutes": minutes, "game_score": gs, "plus_minus": 0, "available": True,
        }  # fmt: skip
        row.update((overrides or {}).get(pid, {}))
        rows.append(row)
    return rows


def schedule(days=4, a_gs=15.0, b_gs=5.0, overrides=None):
    start = date(2021, 1, 1)
    rows = []
    for i in range(days):
        day = start + timedelta(days=2 * i)
        rows += box(f"g{i}", day, "A", True, gs=a_gs, overrides=(overrides or {}).get(i))
        rows += box(f"g{i}", day, "B", False, gs=b_gs)
    return pd.DataFrame(rows)


def by_team(features):
    return features.set_index(["game_id", "team_id"])


def test_unknown_players_start_at_replacement_level():
    f = by_team(players.walk(schedule(days=1)))
    params = PlayerParams()
    # No history: availability is unknown (0), so realistic falls back to replacement level.
    assert f.loc[("g0", "A"), "production_real"] == pytest.approx(GAME_MINUTES * params.replacement)
    # Knows the lineup: five unknown players at new_player_minutes each, scaled to 240.
    assert f.loc[("g0", "A"), "production_lineup"] == pytest.approx(
        GAME_MINUTES * params.replacement
    )


def test_better_production_shows_up_in_later_games_only():
    f = by_team(players.walk(schedule()))
    assert f.loc[("g0", "A"), "production_real"] == f.loc[("g0", "B"), "production_real"]
    assert f.loc[("g3", "A"), "production_real"] > f.loc[("g3", "B"), "production_real"]


def test_no_leakage_from_the_game_being_predicted():
    base = by_team(players.walk(schedule()))
    changed = schedule(overrides={2: {"a1": {"game_score": 60.0, "minutes": 10}}})
    after = by_team(players.walk(changed))
    for game in ("g0", "g1", "g2"):
        assert after.loc[(game, "A")].equals(base.loc[(game, "A")])
    assert not after.loc[("g3", "A")].equals(base.loc[("g3", "A")])


def test_missing_star_lowers_lineup_strength_only():
    star = {"a1": {"game_score": 80.0}}
    out = {"a1": {"played": False, "did_not_play": True, "minutes": 0, "available": False}}
    healthy = by_team(players.walk(schedule(overrides={0: star, 1: star, 2: star})))
    hurt = by_team(players.walk(schedule(overrides={0: star, 1: star, 2: star, 3: out})))
    # Knowing the lineup, the star's absence costs value (their minutes go to lesser players)...
    a3 = ("g3", "A")
    assert hurt.loc[a3, "production_lineup"] < healthy.loc[a3, "production_lineup"]
    # ...while the realistic version, which uses nothing from game day, can't see it.
    assert hurt.loc[a3, "production_real"] == healthy.loc[a3, "production_real"]


def test_missing_stats_dont_count_as_zero():
    missing = {p: {"stats_missing": True, "minutes": None, "game_score": None}
               for p in ROSTERS["A"]}  # fmt: skip
    base = by_team(players.walk(schedule(days=3)))
    gap = by_team(players.walk(schedule(days=3, overrides={1: missing})))
    # Game 1's blank box score must not drag A's production or availability toward zero.
    assert gap.loc[("g2", "A"), "production_real"] >= base.loc[("g1", "A"), "production_real"]


def test_traded_player_moves_rosters():
    state = players.PlayerState(PlayerParams())
    rows_a = box("g0", date(2021, 1, 1), "A", True)
    state.record("A", [pd.Series(r) for r in rows_a])
    moved = {**rows_a[0], "team_id": "B", "game_id": "g1"}
    state.record("B", [pd.Series(moved)])
    assert "a1" in state.rosters["B"] and "a1" not in state.rosters["A"]


def test_continuity_counts_last_seasons_teammates():
    first = schedule(days=2)
    second = schedule(days=2).assign(
        season="2021-22", game_date=lambda d: d["game_date"] + timedelta(days=300),
        tipoff_utc=lambda d: d["tipoff_utc"] + timedelta(days=300),
        game_id=lambda d: d["game_id"] + "x",
    )  # fmt: skip
    f = by_team(players.walk(pd.concat([first, second])))
    assert f.loc[("g0", "A"), "continuity"] == 0
    assert f.loc[("g0x", "A"), "continuity"] == pytest.approx(1.0)
