import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from nbaforecast import availability as av
from nbaforecast import live

FIXTURES = Path(__file__).parent / "fixtures"
INJURIES = json.loads((FIXTURES / "espn_injuries.json").read_text())
TODAY = date(2026, 10, 27)
MORNING = datetime(2026, 10, 27, 14, tzinfo=UTC)


def pred(game_id, day, tip, home_id, home, away_id, away):
    return {
        "game_id": game_id,
        "season": "2026-27",
        "season_type": "regular_season",
        "game_date": day,
        "tipoff_utc": tip,
        "home_team_id": home_id,
        "away_team_id": away_id,
        "home_team": home,
        "away_team": away,
        "is_neutral_site": False,
        "p_home": 0.6,
        "home_elo": 1550,
        "away_elo": 1500,
        "home_court": 31,
        "market_p_home": "",
        "market_books": 0,
        "model": live.MODEL_NAME,
        "ratings_through": "2026-10-26",
        "predicted_at": "2026-10-27T14:00:00+00:00",
        "code_sha": "abc",
    }


@pytest.fixture
def store(tmp_path):
    s = live.Store(tmp_path)
    s.append(
        s.predictions_path("2026-27"),
        live.PREDICTION_FIELDS,
        [
            pred("g1", "2026-10-27", "2026-10-27T23:30:00+00:00",
                 "2", "Boston Celtics", "17", "Brooklyn Nets"),
            pred("g2", "2026-10-27", "2026-10-28T00:00:00+00:00",
                 "14", "Miami Heat", "4", "Chicago Bulls"),
        ],
    )  # fmt: skip
    return s


def box(players: dict[str, tuple[int, bool]]) -> dict:
    """A minimal ESPN summary: player_id -> (minutes, didNotPlay)."""
    return {
        "boxscore": {
            "players": [
                {
                    "team": {"id": "2", "displayName": "Boston Celtics"},
                    "statistics": [
                        {
                            "names": ["MIN", "PTS"],
                            "athletes": [
                                {"athlete": {"id": pid}, "didNotPlay": dnp, "stats": [str(m), "0"]}
                                for pid, (m, dnp) in players.items()
                            ],
                        }
                    ],
                }
            ]
        }
    }


def run_prepare(store, workdir, summaries=None, now=MORNING, day=TODAY):
    summaries = summaries or {}
    return av.prepare(
        store,
        day,
        now,
        workdir,
        fetch_injuries=lambda: INJURIES,
        fetch_summary=lambda g: summaries[g],
    )


@pytest.mark.parametrize(
    ("note", "p"),
    [
        ("Williams (groin) has been ruled out for Monday's game.", 0.05),
        ("Smith is doubtful for Friday.", 0.20),
        ("Jones is questionable for Tuesday's game.", 0.50),
        ("White (right hip) is probable for Tuesday's game against the Nets.", 0.90),
        ("Brown will be re-evaluated in a week.", 0.50),
    ],
)
def test_keyword_baseline(note, p):
    assert av.keyword_baseline(note) == p


def test_prepare_picks_non_out_players_whose_team_plays(store, tmp_path):
    summary = run_prepare(store, tmp_path / "w")
    assert summary == {"resolved": 0, "injured": 6, "candidates": 2}
    todo = json.loads((tmp_path / "w" / "baseline.json").read_text())
    by_name = {c["player"]: c for c in todo}
    assert set(by_name) == {"Derrick White", "Patrick Williams"}  # Hawks don't play; "Out" skipped
    assert by_name["Derrick White"]["baseline_p"] == 0.90
    assert (
        by_name["Patrick Williams"]["baseline_p"] == 0.05
    )  # stale "Monday" note: the rule's blind spot
    assert by_name["Patrick Williams"]["opponent"] == "Miami Heat"
    agent_view = json.loads((tmp_path / "w" / "agent" / "candidates.json").read_text())
    assert agent_view["today_weekday"] == "Tuesday"
    assert all("baseline_p" not in p for p in agent_view["players"])  # agent doesn't see the rule
    assert sorted(p.name for p in (tmp_path / "w" / "agent").iterdir()) == [
        "candidates.json",
        "prompt.txt",
    ]
    assert (store.root / "injuries" / "2026-10-27.json").exists()


def test_started_games_are_not_candidates(store, tmp_path):
    after_first_tip = datetime(2026, 10, 27, 23, 45, tzinfo=UTC)
    assert run_prepare(store, tmp_path / "w", now=after_first_tip)["candidates"] == 1


def answer(**players):
    return json.dumps({"players": [{"player_id": pid, **a} for pid, a in players.items()]})


WHITE, WILLIAMS = "3078576", "4431687"
GOOD = {
    WHITE: {
        "p_play": 0.92,
        "quote": "is probable for Tuesday's game",
        "minutes_restriction": False,
    },
    WILLIAMS: {
        "p_play": 0.55,
        "quote": "ruled out for Monday's game",
        "minutes_restriction": False,
    },
}


def test_validate_accepts_grounded_answers(store, tmp_path):
    run_prepare(store, tmp_path / "w")
    todo = json.loads((tmp_path / "w" / "baseline.json").read_text())
    accepted, problems = av.validate(answer(**GOOD), todo)
    assert problems == [] and accepted[WILLIAMS]["p"] == 0.55


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({WHITE: {**GOOD[WHITE], "quote": "is fully healthy and starting"}}, "quote isn't from"),
        ({WHITE: {**GOOD[WHITE], "p_play": 1.4}}, "isn't a probability"),
        ({"999": {**GOOD[WHITE]}}, "unknown player_id"),
        ({WHITE: {**GOOD[WHITE], "quote": "probable"}}, "quote isn't from"),  # too short
    ],
)
def test_validate_drops_bad_answers(store, tmp_path, change, expected):
    run_prepare(store, tmp_path / "w")
    todo = json.loads((tmp_path / "w" / "baseline.json").read_text())
    accepted, problems = av.validate(answer(**change), todo)
    assert any(expected in p for p in problems), problems
    assert WHITE not in accepted


def test_validate_handles_garbage_output():
    assert av.validate("not json", [])[1][0].startswith("output isn't the expected JSON")
    assert av.validate(None, []) == ({}, ["no agent output"])


def test_publish_logs_every_candidate_with_both_methods(store, tmp_path):
    w = tmp_path / "w"
    run_prepare(store, w)
    (w / "agent" / "availability.json").write_text(
        answer(**{WHITE: GOOD[WHITE]})
    )  # agent skipped Williams
    out = av.publish(store, TODAY, MORNING, w)
    assert out["logged"] == 2 and out["agent_answers"] == 1
    rows = {r["player"]: r for r in store.read(av.paths(store, "2026-27")[0])}
    assert rows["Derrick White"]["agent_p"] == "0.92"
    assert rows["Patrick Williams"]["agent_p"] == ""  # rule still logged; agent left blank
    assert rows["Patrick Williams"]["baseline_p"] == "0.05"


def test_rerun_does_not_relog(store, tmp_path):
    run_prepare(store, tmp_path / "a")
    av.publish(store, TODAY, MORNING, tmp_path / "a")
    assert run_prepare(store, tmp_path / "b")["candidates"] == 0


def test_next_morning_scoring(store, tmp_path):
    w = tmp_path / "w"
    run_prepare(store, w)
    (w / "agent" / "availability.json").write_text(answer(**GOOD))
    av.publish(store, TODAY, MORNING, w)
    # Both games go final; White played 28 minutes, Williams was inactive (absent from the box).
    store.append(
        store.results_path("2026-27"),
        live.RESULT_FIELDS,
        [{"game_id": g, "season": "2026-27", "game_date": "2026-10-27"} for g in ("g1", "g2")],
    )
    summaries = {"g1": box({WHITE: (28, False)}), "g2": box({})}
    tomorrow = datetime(2026, 10, 28, 14, tzinfo=UTC)
    out = run_prepare(
        store, tmp_path / "next", summaries=summaries, now=tomorrow, day=date(2026, 10, 28)
    )
    assert out["resolved"] == 2
    results = {r["player_id"]: r for r in store.read(av.paths(store, "2026-27")[1])}
    assert results[WHITE]["played"] == "True" and results[WHITE]["minutes"] == "28"
    assert results[WILLIAMS]["played"] == "False"
    assert results[WILLIAMS]["reason"].startswith("inactive")

    card = av.scorecard(store, "2026-27", tomorrow)
    assert card["calls_scored"] == 2 and card["played_rate"] == 0.5
    h2h = card["head_to_head"]
    assert h2h["calls"] == 2
    # The rule was right on both (0.90 played, 0.05 sat); the agent hedged on Williams.
    assert h2h["keyword_rule"]["brier"] < h2h["agent"]["brier"]


def test_verify_guards_the_availability_log(store, tmp_path):
    w = tmp_path / "w"
    run_prepare(store, w)
    (w / "agent" / "availability.json").write_text(answer(**GOOD))
    av.publish(store, TODAY, MORNING, w)
    log = av.paths(store, "2026-27")[0]
    committed = log.read_text()
    assert (
        live.verify(
            store.root, lambda rel: committed if rel == log.relative_to(store.root) else None
        )
        == []
    )
    log.write_text(committed.replace("0.92", "0.99"))  # edit a past call
    problems = live.verify(
        store.root, lambda rel: committed if rel == log.relative_to(store.root) else None
    )
    assert any("append-only" in p and "availability" in p for p in problems)
