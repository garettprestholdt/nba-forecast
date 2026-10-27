import json
import re
from datetime import date

import pytest

from nbaforecast import config, live, web
from nbaforecast.models.elo import EloState

FRANCHISES = [str(i) for i in range(1, 31)]


def prediction(game_id, home, away, p_home, market, tip="2026-10-27T23:30:00+00:00"):
    return {
        "game_id": game_id,
        "season": "2026-27",
        "season_type": "regular_season",
        "game_date": "2026-10-27",
        "tipoff_utc": tip,
        "home_team_id": home,
        "away_team_id": away,
        "home_team": f"Home {home}",
        "away_team": f"Away {away}",
        "is_neutral_site": False,
        "p_home": p_home,
        "home_elo": 1550,
        "away_elo": 1500,
        "home_court": 31,
        "market_p_home": market,
        "market_books": 9 if market != "" else 0,
        "model": live.MODEL_NAME,
        "ratings_through": "2026-10-26",
        "predicted_at": "2026-10-27T17:07:10+00:00",
        "code_sha": "abc",
    }


@pytest.fixture
def data_dir(tmp_path):
    store = live.Store(tmp_path)
    state = EloState(
        live.LIVE_PARAMS,
        ratings={t: 1500 + 10 * (15 - i) for i, t in enumerate(FRANCHISES)},
        season="2026-27",
        last_date=date(2026, 10, 26),
    )
    store.save_state(state)
    store.append(
        store.predictions_path("2026-27"),
        live.PREDICTION_FIELDS,
        [
            prediction("g1", "2", "17", 0.867, 0.823),
            prediction("g2", "18", "8", 0.586, ""),
        ],
    )
    store.append(
        store.results_path("2026-27"),
        live.RESULT_FIELDS,
        [
            {
                "game_id": "g1",
                "season": "2026-27",
                "season_type": "regular_season",
                "game_date": "2026-10-27",
                "tipoff_utc": "2026-10-27T23:30:00+00:00",
                "home_team_id": "2",
                "away_team_id": "17",
                "home_team": "Home 2",
                "away_team": "Away 17",
                "home_score": 110,
                "away_score": 101,
                "is_neutral_site": False,
            }
        ],
    )
    store.scorecard_path("2026-27").parent.mkdir(parents=True)
    store.scorecard_path("2026-27").write_text(
        json.dumps(live.scorecard(store, "2026-27", __import__("datetime").datetime.now()))
    )
    return tmp_path


def specs(html: str) -> dict[str, dict]:
    pattern = r'<script type="application/json" id="([^"]+)">(.*?)</script>'
    return {m.group(1): json.loads(m.group(2)) for m in re.finditer(pattern, html, re.S)}


def fields(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "field" and isinstance(value, str):
                yield value
            yield from fields(value)
    elif isinstance(node, list):
        for item in node:
            yield from fields(item)


@pytest.fixture
def site(data_dir, tmp_path_factory):
    out = tmp_path_factory.mktemp("site")
    web.build(data_dir, out, published_dir=config.PUBLISHED_DIR)
    return out


def test_builds_every_page_and_assets(site):
    for name in [
        "index.html",
        "ratings.html",
        "model.html",
        "assets/style.css",
        "assets/charts.js",
        ".nojekyll",
    ]:
        assert (site / name).exists(), name


def test_today_page_shows_slate_result_and_market(site):
    html = (site / "index.html").read_text()
    assert "Predictions for Tue, Oct 27" in html
    assert "Away 17" in html and "Home 2" in html
    assert "7:30 PM ET" in html
    assert "+4 pts" in html  # 86.7% Elo vs 82.3% market
    assert "101–110" in html  # final score shown with a correct pick


def test_ratings_page_lists_all_teams_sorted(site):
    html = (site / "ratings.html").read_text()
    assert html.count('<td class="team">') == 30
    assert html.index("Atlanta Hawks") < html.index("Charlotte Hornets")  # ID 1 rated highest


def test_chart_specs_are_valid_json_with_safe_field_names(site):
    for page in ["index.html", "ratings.html", "model.html"]:
        for spec_id, spec in specs((site / page).read_text()).items():
            for field in fields(spec):
                # Vega reads ' . [ ] in a field name as path syntax.
                assert not re.search(r"['.\[\]]", field), f"{page}:{spec_id} field {field!r}"


def test_model_page_compares_both_elo_variants(site):
    html = (site / "model.html").read_text()
    calibration = specs(html)["spec-calibration"]
    series = {
        row["series"]
        for layer in calibration["layer"]
        for row in layer.get("data", {}).get("values", [])
        if "series" in row
    }
    assert series == {web.FIXED, web.ADAPTIVE}  # live joins only after 100 scored games
    assert "the live model" in html


def test_colors_are_tokens_not_hex(site):
    for page in ["index.html", "ratings.html", "model.html"]:
        for spec in specs((site / page).read_text()).values():
            assert not re.search(r"#[0-9a-fA-F]{6}", json.dumps(spec)), page


def test_latest_recap_shown_with_its_source(data_dir, tmp_path_factory):
    recaps = data_dir / "recaps"
    recaps.mkdir()
    (recaps / "2026-10-27.md").write_text("<!-- source: template; model: none -->\nOld recap.\n")
    (recaps / "2026-10-28.md").write_text(
        "<!-- source: agent; model: m -->\nElo went 1 of 1.\n\n- Away 17 101, Home 2 110\n"
    )
    out = tmp_path_factory.mktemp("site-recap")
    web.build(data_dir, out, published_dir=config.PUBLISHED_DIR)
    html = (out / "index.html").read_text()
    assert "Morning recap · Wed, Oct 28" in html
    assert "<p>Elo went 1 of 1.</p>" in html and "<li>Away 17 101, Home 2 110</li>" in html
    assert "Written by an AI agent" in html and "Old recap" not in html
    assert "/blob/data/recaps/2026-10-28.json" in html


def test_recap_text_is_escaped(data_dir, tmp_path_factory):
    recaps = data_dir / "recaps"
    recaps.mkdir()
    (recaps / "2026-10-28.md").write_text("<!-- source: agent; model: m -->\n<script>x</script>\n")
    out = tmp_path_factory.mktemp("site-escape")
    web.build(data_dir, out, published_dir=config.PUBLISHED_DIR)
    assert "<script>x</script>" not in (out / "index.html").read_text()


def test_injury_watch_card(data_dir, tmp_path_factory):
    from nbaforecast import availability

    store = live.Store(data_dir)
    log, results = availability.paths(store, "2026-27")
    store.append(
        log,
        availability.FIELDS,
        [
            {
                "game_date": "2026-10-27", "game_id": "g1",
                "tipoff_utc": "2026-10-27T23:30:00+00:00",
                "player_id": "p1", "player": "Derrick White", "team_id": "2", "team": "Home 2",
                "opponent": "Away 17", "status": "Day-To-Day", "note": "White is probable.",
                "note_updated": "", "baseline_p": 0.9, "agent_p": 0.93,
                "agent_quote": "White is probable", "minutes_restriction": False,
                "predicted_at": "2026-10-27T17:00:00+00:00",
            }
        ],
    )  # fmt: skip
    store.append(
        results,
        availability.RESULT_FIELDS,
        [
            {
                "game_id": "g1",
                "player_id": "p1",
                "played": True,
                "minutes": 31,
                "reason": "",
                "resolved_at": "",
            }
        ],
    )
    out = tmp_path_factory.mktemp("site-injury")
    web.build(data_dir, out, published_dir=config.PUBLISHED_DIR)
    html = (out / "index.html").read_text()
    assert "Injury watch" in html and "Derrick White" in html
    assert "93%" in html and "Played (31 min)" in html and "“White is probable”" in html
