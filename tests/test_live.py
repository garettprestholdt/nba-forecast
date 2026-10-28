import json
import random
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from nbaforecast.ingest import odds
from nbaforecast.models import elo
from nbaforecast.models.elo import EloState
from nbaforecast.pipeline import live

PARAMS = live.LIVE_PARAMS.__class__(k=20, hca_window=6, carryover=0.7)
TEAMS = [str(i) for i in range(1, 11)]  # real ESPN franchise IDs
START = date(2026, 10, 21)


def make_games(days=8, seed=0):
    """Synthetic schedule: every team plays every day, 7:30pm ET (23:30 UTC) tip-offs."""
    rng = random.Random(seed)
    strength = {t: rng.gauss(0, 6) for t in TEAMS}
    rows = []
    for d in range(days):
        day = START + timedelta(days=d)
        ids = TEAMS[:]
        rng.shuffle(ids)
        for i, (home, away) in enumerate(zip(ids[::2], ids[1::2], strict=True)):
            margin = round(strength[home] - strength[away] + 3 + rng.gauss(0, 11)) or 1
            rows.append(
                {
                    "game_id": f"{day:%m%d}{i}",
                    "season": "2026-27",
                    "season_type": "regular_season",
                    "game_date": day,
                    "tipoff_utc": datetime(day.year, day.month, day.day, 23, 30 + i, tzinfo=UTC),
                    "home_team_id": home,
                    "away_team_id": away,
                    "home_team_name": f"Team {home}",
                    "away_team_name": f"Team {away}",
                    "home_score": 100 + margin,
                    "away_score": 100,
                    "is_neutral_site": False,
                }
            )
    return pd.DataFrame(rows)


def board(games: pd.DataFrame, day: date, status="STATUS_FINAL", completed=True, overrides=None):
    """An ESPN-shaped scoreboard for one date."""
    overrides = overrides or {}
    events = []
    for g in games[games.game_date == day].itertuples(index=False):
        st, done = overrides.get(g.game_id, (status, completed))
        sides = []
        for side, tid, name, score in [
            ("home", g.home_team_id, g.home_team_name, g.home_score),
            ("away", g.away_team_id, g.away_team_name, g.away_score),
        ]:
            sides.append(
                {
                    "homeAway": side,
                    "score": str(score) if done else "0",
                    "linescores": [{"value": 1}] * 4 if done else [],
                    "team": {"id": tid, "abbreviation": f"T{tid}", "displayName": name},
                }
            )
        events.append(
            {
                "id": g.game_id,
                "date": g.tipoff_utc.isoformat().replace("+00:00", "Z"),
                "season": {"year": 2027, "type": 2},
                "status": {"type": {"completed": done, "name": st}},
                "competitions": [
                    {
                        "startDate": g.tipoff_utc.isoformat().replace("+00:00", "Z"),
                        "neutralSite": False,
                        "venue": {"fullName": "Arena"},
                        "competitors": sides,
                    }
                ],
            }
        )
    return {"leagues": [{"calendarType": "day", "calendar": []}], "events": events}


def morning(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 14, 0, tzinfo=UTC)


def run_daily(root, games, day, **kw):
    def fetch(d):
        # Past dates are final; today's games haven't started.
        if d < day:
            return board(games, d)
        return board(games, d, status="STATUS_SCHEDULED", completed=False)

    return live.daily(
        root, today=day, now=morning(day), fetch=fetch, odds_events=kw.get("odds"), code_sha="abc"
    )


@pytest.fixture
def setup(tmp_path):
    games = make_games()
    cutoff = START + timedelta(days=2)
    live.bootstrap(tmp_path, games, through=cutoff, params=PARAMS)
    return tmp_path, games, cutoff


def test_daily_matches_batch_backtest_exactly(setup):
    root, games, cutoff = setup
    last = games.game_date.max()
    day = cutoff + timedelta(days=1)
    while day <= last + timedelta(days=1):
        run_daily(root, games, day)
        day += timedelta(days=1)

    batch_preds, batch_ratings = elo.run(games, PARAMS)
    state = live.Store(root).load_state()
    for team, rating in batch_ratings.items():
        assert state.ratings[team] == pytest.approx(rating, abs=1e-9)

    logged = pd.DataFrame(live.Store.read(root / "predictions" / "2026-27.csv"))
    batch = batch_preds.set_index("game_id")["p_home"]
    assert len(logged) == (games.game_date > cutoff).sum()
    for row in logged.itertuples():
        assert float(row.p_home) == pytest.approx(batch[row.game_id], abs=1e-6)


def test_bootstrap_refuses_to_overwrite(setup):
    root, games, cutoff = setup
    with pytest.raises(FileExistsError):
        live.bootstrap(root, games, through=cutoff, params=PARAMS)


def test_rerun_same_day_does_not_duplicate(setup):
    root, games, cutoff = setup
    day = cutoff + timedelta(days=1)
    first = run_daily(root, games, day)
    second = run_daily(root, games, day)
    assert first["predicted"] == 5 and second["predicted"] == 0
    assert live.verify(root, lambda rel: None) == []


def test_ingest_waits_for_unfinished_games(setup):
    root, games, cutoff = setup
    day = cutoff + timedelta(days=2)
    target = cutoff + timedelta(days=1)
    unfinished = games[games.game_date == target].game_id.iloc[0]

    def fetch(d):
        if d == target:
            return board(games, d, overrides={unfinished: ("STATUS_IN_PROGRESS", False)})
        return board(games, d, status="STATUS_SCHEDULED", completed=False)

    summary = live.daily(
        root, today=day, now=morning(day), fetch=fetch, odds_events=None, code_sha="x"
    )
    assert summary["results_ingested"] == 0
    assert summary["ratings_through"] == cutoff.isoformat()  # didn't skip past the open date


def test_postponed_games_dont_block(setup):
    root, games, cutoff = setup
    target = cutoff + timedelta(days=1)
    postponed = games[games.game_date == target].game_id.iloc[0]

    def fetch(d):
        if d == target:
            return board(games, d, overrides={postponed: ("STATUS_POSTPONED", False)})
        return board(games, d, status="STATUS_SCHEDULED", completed=False)

    day = target + timedelta(days=1)
    summary = live.daily(
        root, today=day, now=morning(day), fetch=fetch, odds_events=None, code_sha="x"
    )
    assert summary["results_ingested"] == 4
    assert summary["ratings_through"] == target.isoformat()


def test_started_games_are_not_predicted(setup):
    root, games, cutoff = setup
    day = cutoff + timedelta(days=1)
    first = games[games.game_date == day].sort_values("tipoff_utc").iloc[0]

    def fetch(d):
        if d < day:
            return board(games, d)
        return board(games, d, status="STATUS_SCHEDULED", completed=False)

    late = first.tipoff_utc + timedelta(seconds=30)  # only the first game has tipped off
    summary = live.daily(root, today=day, now=late, fetch=fetch, odds_events=None, code_sha="x")
    assert summary["already_started"] == 1 and summary["predicted"] == 4


def test_market_probability_attached(setup):
    root, games, cutoff = setup
    day = cutoff + timedelta(days=1)
    g = games[games.game_date == day].iloc[0]
    event = {
        "commence_time": g.tipoff_utc.isoformat(),
        "home_team": g.home_team_name,
        "away_team": g.away_team_name,
        "bookmakers": [
            {
                "key": "book",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": g.home_team_name, "price": 1.5},
                            {"name": g.away_team_name, "price": 2.6},
                        ],
                    }
                ],
            }
        ],
    }
    summary = run_daily(root, games, day, odds=[event])
    assert summary["with_market"] == 1
    row = next(
        r
        for r in live.Store.read(root / "predictions" / "2026-27.csv")
        if r["game_id"] == g.game_id
    )
    assert float(row["market_p_home"]) == pytest.approx(odds.devig_two_way(1.5, 2.6)[0], abs=1e-6)


def test_scorecard_counts_scored_games(setup):
    root, games, cutoff = setup
    for offset in (1, 2):
        run_daily(root, games, cutoff + timedelta(days=offset))
    card = json.loads((root / "scorecard" / "2026-27.json").read_text())
    assert card["games_predicted"] == 10
    assert card["games_scored"] == 5  # day 1's games have results by day 2's run
    assert card["coin_flip"]["log_loss"] == pytest.approx(0.69315, abs=1e-5)
    assert card["head_to_head_with_market"]["games"] == 0


def test_verify_catches_edits_and_late_predictions(setup):
    root, games, cutoff = setup
    run_daily(root, games, cutoff + timedelta(days=1))
    path = root / "predictions" / "2026-27.csv"
    committed = path.read_text()

    assert live.verify(root, lambda rel: committed) == []

    # Editing an existing prediction breaks append-only.
    path.write_text(committed.replace(",0.", ",0.9", 1))
    assert any("append-only" in p for p in live.verify(root, lambda rel: committed))

    # A prediction stamped after tip-off is rejected.
    path.write_text(committed)
    rows = live.Store.read(path)
    rows[0]["predicted_at"] = "2099-01-01T00:00:00+00:00"
    path.unlink()
    live.Store.append(path, live.PREDICTION_FIELDS, rows)
    assert any("after tip-off" in p for p in live.verify(root, lambda rel: None))


def test_state_round_trips_through_json(setup):
    root, _, cutoff = setup
    state = live.Store(root).load_state()
    again = EloState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert again.ratings == state.ratings
    assert again.last_date == cutoff
    assert list(again.home_results) == list(state.home_results)
