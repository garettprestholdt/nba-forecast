import json
from datetime import UTC, date, datetime

import pytest

from nbaforecast import live, recap
from nbaforecast.models.elo import EloState

TODAY = date(2026, 10, 28)


def pred(
    game_id, day, home_id, home, away_id, away, p_home, market, home_elo=1550.0, away_elo=1500.0
):
    return {
        "game_id": game_id,
        "season": "2026-27",
        "season_type": "regular_season",
        "game_date": day,
        "tipoff_utc": f"{day}T23:30:00+00:00",
        "home_team_id": home_id,
        "away_team_id": away_id,
        "home_team": home,
        "away_team": away,
        "is_neutral_site": False,
        "p_home": p_home,
        "home_elo": home_elo,
        "away_elo": away_elo,
        "home_court": 31,
        "market_p_home": market,
        "market_books": 9 if market != "" else 0,
        "model": live.MODEL_NAME,
        "ratings_through": "2026-10-26",
        "predicted_at": f"{day}T17:00:00+00:00",
        "code_sha": "abc",
    }


def result(game_id, day, home_id, home, away_id, away, hs, as_):
    return {
        "game_id": game_id,
        "season": "2026-27",
        "season_type": "regular_season",
        "game_date": day,
        "tipoff_utc": f"{day}T23:30:00+00:00",
        "home_team_id": home_id,
        "away_team_id": away_id,
        "home_team": home,
        "away_team": away,
        "home_score": hs,
        "away_score": as_,
        "is_neutral_site": False,
    }


@pytest.fixture
def store(tmp_path):
    s = live.Store(tmp_path)
    s.append(
        s.predictions_path("2026-27"),
        live.PREDICTION_FIELDS,
        [
            # Last night: a correct pick and an upset.
            pred("a", "2026-10-27", "2", "Boston Celtics", "17", "Brooklyn Nets", 0.867, 0.823),
            pred(
                "b",
                "2026-10-27",
                "13",
                "Los Angeles Lakers",
                "22",
                "Portland Trail Blazers",
                0.70,
                0.58,
            ),
            # Tonight.
            pred("c", "2026-10-28", "18", "New York Knicks", "8", "Detroit Pistons", 0.52, 0.60),
            pred("d", "2026-10-28", "24", "San Antonio Spurs", "23", "Sacramento Kings", 0.88, ""),
        ],
    )
    s.append(
        s.results_path("2026-27"),
        live.RESULT_FIELDS,
        [
            result("a", "2026-10-27", "2", "Boston Celtics", "17", "Brooklyn Nets", 118, 101),
            result(
                "b",
                "2026-10-27",
                "13",
                "Los Angeles Lakers",
                "22",
                "Portland Trail Blazers",
                99,
                108,
            ),
        ],
    )
    s.save_state(
        EloState(
            live.LIVE_PARAMS,
            ratings={"2": 1560.0, "17": 1490.0, "13": 1530.0, "22": 1520.0},
            season="2026-27",
            last_date=date(2026, 10, 27),
        )
    )
    card = live.scorecard(s, "2026-27", datetime(2026, 10, 28, 14, tzinfo=UTC))
    s.scorecard_path("2026-27").parent.mkdir(parents=True)
    s.scorecard_path("2026-27").write_text(json.dumps(card))
    return s


@pytest.fixture
def facts(store):
    return recap.build_facts(store, store.load_state(), TODAY)


def test_facts_last_night(facts):
    s = facts["last_night_summary"]
    assert s == {
        "games": 2,
        "elo_correct": 1,
        "games_with_market": 2,
        "market_correct": 1,
        "upsets_by_elo": 1,
    }
    upset = next(g for g in facts["last_night"] if g["upset_by_elo"])
    assert upset["winner"] == "Portland Trail Blazers"
    assert upset["elo_chance_for_winner"] == "30%"
    assert upset["final_score"] == "Portland Trail Blazers 108, Los Angeles Lakers 99"
    celtics = next(g for g in facts["last_night"] if g["winner"] == "Boston Celtics")
    assert celtics["elo_rating_change"] == {"Boston Celtics": "+10", "Brooklyn Nets": "-10"}


def test_facts_tonight_highlights(facts):
    assert facts["tonight_summary"] == {"games": 2}
    assert facts["tonight_highlights"]["closest_game"] == "Detroit Pistons at New York Knicks"
    assert (
        facts["tonight_highlights"]["biggest_elo_market_disagreement"]
        == "Detroit Pistons at New York Knicks"
    )
    knicks = facts["tonight"][0]
    assert knicks["elo_minus_market_points"] == "-8"
    assert "_gap" not in knicks and "_p_home" not in knicks


def test_rating_changes_skipped_when_ratings_not_current(store):
    state = store.load_state()
    state.last_date = date(2026, 10, 25)
    facts = recap.build_facts(store, state, TODAY)
    assert all("elo_rating_change" not in g for g in facts["last_night"])


def test_no_facts_on_empty_day(store):
    assert recap.build_facts(store, store.load_state(), date(2026, 12, 1)) is None


GOOD = (
    "Elo picked 1 of 2 winners last night. The miss was the upset of the night: the Portland "
    "Trail Blazers won 108 to 99 at the Los Angeles Lakers despite a 30% Elo chance, and the "
    "market had them at 42%. The Boston Celtics handled the Brooklyn Nets as expected, 118 to "
    "101, and gained 10 rating points.\n\n"
    "Tonight the closest game by Elo is Detroit Pistons at New York Knicks, which is also where "
    "Elo and the market disagree most, by 8 points. Season to date, Elo's log-loss is ahead of a "
    "coin flip at 0.693."
)


def test_grounded_draft_passes(facts):
    assert recap.check_recap(GOOD, facts) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda t: t.replace("108 to 99", "112 to 99"), "number '112'"),
        (lambda t: t + " The Miami Heat await on Friday.", "team 'Heat'"),
        (lambda t: t + " The Knicks look like a lock tonight.", "betting language: 'lock'"),
        (lambda t: "Short recap.", "length"),
        (lambda t: t + " More at https://example.com", "links or HTML"),
        # Real numbers in the wrong place: 3 appears in the facts, but not as a count.
        (lambda t: t.replace("picked 1 of 2 winners", "picked 1 of 3 winners"), "count '3'"),
        (lambda t: t + " Tonight has 8 games.", "count '8'"),
        (lambda t: t.replace("despite a 30% Elo chance", "despite a 31% Elo chance"), "31%"),
    ],
)
def test_ungrounded_drafts_fail(facts, change, expected):
    problems = recap.check_recap(change(GOOD), facts)
    assert any(expected in p for p in problems), problems


def test_template_passes_its_own_fact_check(facts):
    text = recap.template_recap(facts)
    problems = [p for p in recap.check_recap(text, facts) if not p.startswith("length")]
    assert problems == []
    assert "Portland Trail Blazers beat Los Angeles Lakers" in text


def test_prepare_writes_facts_and_isolated_prompt(store, tmp_path):
    workdir = tmp_path / "agent"
    prompt = recap.prepare(store, store.load_state(), TODAY, workdir)
    assert prompt == workdir / "prompt.txt"
    assert sorted(p.name for p in workdir.iterdir()) == ["facts.json", "prompt.txt"]
    assert "facts.json" in prompt.read_text() and "recap_draft.md" in prompt.read_text()
    facts_path, _ = recap.recap_paths(store, TODAY)
    assert json.loads(facts_path.read_text())["recap_date"] == "2026-10-28"


def test_publish_accepts_grounded_draft(store, tmp_path):
    recap.prepare(store, store.load_state(), TODAY, tmp_path / "agent")
    outcome = recap.publish(store, TODAY, GOOD, model="claude-test")
    assert outcome["source"] == "agent" and outcome["problems"] == []
    source, body = recap.read_published(recap.recap_paths(store, TODAY)[1])
    assert source == "agent" and body.startswith("Elo picked 1 of 2")


def test_publish_falls_back_to_template(store, tmp_path):
    recap.prepare(store, store.load_state(), TODAY, tmp_path / "agent")
    outcome = recap.publish(store, TODAY, GOOD + " The Heat rolled.", model="claude-test")
    assert outcome["source"] == "template"
    assert any("Heat" in p for p in outcome["problems"])
    source, _ = recap.read_published(recap.recap_paths(store, TODAY)[1])
    assert source == "template"


def test_publish_without_draft_uses_template(store, tmp_path):
    recap.prepare(store, store.load_state(), TODAY, tmp_path / "agent")
    assert recap.publish(store, TODAY, None, model="x")["source"] == "template"


def test_prepare_skips_when_recap_already_published(store, tmp_path):
    recap.prepare(store, store.load_state(), TODAY, tmp_path / "a")
    recap.publish(store, TODAY, None, model="x")
    assert recap.prepare(store, store.load_state(), TODAY, tmp_path / "b") is None


def test_prepare_force_reuses_a_published_day(store, tmp_path):
    recap.prepare(store, store.load_state(), TODAY, tmp_path / "a")
    recap.publish(store, TODAY, None, model="x")
    assert recap.prepare(store, store.load_state(), TODAY, tmp_path / "b", force=True) is not None
