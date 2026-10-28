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
        "standings.html",
        "model.html",
        "ratings.html",
        "assets/style.css",
        "assets/charts.js",
        "assets/court.svg",
        "assets/mark.svg",
        ".nojekyll",
    ]:
        assert (site / name).exists(), name


def test_today_page_shows_slate_result_and_market(site):
    html = (site / "index.html").read_text()
    assert "Tuesday, October 27" in html and "2 games" in html
    assert "Celtics" in html and "Nets" in html  # franchise 2 hosts franchise 17
    assert "6:30 PM CT" in html
    assert "Betting market:</span> <b>BOS 82%" in html  # the market's view of the favorite
    assert '<div class="pct score" data-score="home">110</div>' in html  # final replaces %
    assert 'aria-label="Brooklyn Nets 13%, Boston Celtics 87%"' in html  # the split bar


def test_standings_page_power_ratings_without_projections(site):
    html = (site / "standings.html").read_text()
    assert "Projected standings appear after the next morning run" in html
    power = html[html.index('<ol class="power">') :]
    assert power.count("<li>") == 30
    assert power.index("Atlanta Hawks") < power.index("Charlotte Hornets")  # ID 1 rated highest


def test_old_ratings_page_redirects(site):
    assert "url=standings.html#power" in (site / "ratings.html").read_text()


def test_chart_specs_are_valid_json_with_safe_field_names(site):
    for page in ["index.html", "standings.html", "model.html"]:
        for spec_id, spec in specs((site / page).read_text()).items():
            for field in fields(spec):
                # Vega reads ' . [ ] in a field name as path syntax.
                assert not re.search(r"['.\[\]]", field), f"{page}:{spec_id} field {field!r}"


def test_model_page_is_plain_and_simple(site):
    html = (site / "model.html").read_text()
    calibration = specs(html)["spec-calibration"]
    series = {
        row["series"]
        for layer in calibration["layer"]
        for row in layer.get("data", {}).get("values", [])
        if "series" in row
    }
    assert series == {web.ADAPTIVE}  # one model on the honesty chart; live joins after 100 games
    for approach in ("Coin flip", "Always back the home team", "Our model (published)",
                     "Team + player model"):  # fmt: skip
        assert approach in html
    assert "For the curious" in html  # the technical tables are tucked away, not gone
    market = specs(html)["spec-market"]
    assert market["width"] == market["height"]  # same scale on both axes
    for layer in market["layer"]:
        enc = layer["encoding"]
        assert enc["x"]["scale"]["domain"] == enc["y"]["scale"]["domain"] == [0, 1]


def test_new_pages_are_built(site):
    for name in ("history.html", "play.html", "team-bos.html", "team-bkn.html",
                 "assets/app.js", "assets/coldcase.js"):  # fmt: skip
        assert (site / name).exists(), name
    team = (site / "team-bos.html").read_text()
    assert "Boston Celtics" in team and "Why +" in team
    history = (site / "history.html").read_text()
    assert "1 of 1 picks right" in history  # the fixture's one finished, predicted game
    index = (site / "index.html").read_text()
    assert "team-bos.html" in index and 'id="player-dialog"' in index
    assert "6:30 PM CT" in index


def test_colors_are_tokens_not_hex(site):
    for page in ["index.html", "standings.html", "model.html"]:
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
    assert "<strong>93%</strong> chance of playing" in html and "Played (31 min)" in html
    assert "<q>White is probable</q>" in html


def test_shadow_card_only_when_the_challenger_has_a_scorecard(data_dir, tmp_path_factory):
    out = tmp_path_factory.mktemp("site_no_shadow")
    web.build(data_dir, out, published_dir=config.PUBLISHED_DIR)
    assert "Challenger" not in (out / "index.html").read_text()

    card = {
        "season": "2026-27", "model": "hybrid-realistic-v1", "updated_at": "",
        "games_predicted": 12, "games_scored": 9,
        "hybrid": {"log_loss": 0.61234, "brier": 0.21, "accuracy": 0.66667},
        "elo": {"log_loss": 0.64321, "brier": 0.22, "accuracy": 0.55556},
        "head_to_head_with_market": {"games": 0, "hybrid": None, "elo": None, "market": None},
        "promotion_check": {"status": "in progress", "games": 9, "mean_difference": 0.031,
                            "ci95": [-0.02, 0.082]},
    }  # fmt: skip
    path = data_dir / "scorecard" / "2026-27-hybrid.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(card))
    out = tmp_path_factory.mktemp("site_shadow")
    web.build(data_dir, out, published_dir=config.PUBLISHED_DIR)
    html = (out / "index.html").read_text()
    assert "Challenger: team + player model" in html and "0.612" in html and "0.643" in html
    assert "promotion-bar-hybrid.md" in html and "in progress" in html
    path.unlink()


def test_live_scores_are_wired_and_display_only(site):
    index = (site / "index.html").read_text()
    assert 'data-slate="20261027"' in index and 'data-game="g1"' in index
    assert 'data-tip="2026-10-27T23:30:00+00:00"' in index
    assert 'id="box-dialog"' in index and "assets/live.js" in index
    assert 'data-score="home"' in index and 'data-box="g1"' in index
    live_js = (site / "assets" / "live.js").read_text()
    assert "45 * 1000" in live_js  # the scoreboard refresh the maintainer asked for
    assert "This is ESPN's win probability, not ours." in live_js  # always labeled as ESPN's
    assert "Win probability (ESPN)" in live_js
    assert "fetch(" in live_js and "localStorage" not in live_js  # reads ESPN, writes nothing


def test_game_pages_and_player_profiles(data_dir, tmp_path_factory):
    from datetime import UTC, datetime

    from nbaforecast import court, impact
    from nbaforecast.ingest import rosters, venues

    rosters.write_directory(data_dir, {
        "p1": {"name": "Big Wing", "short": "B. Wing", "position": "F", "jersey": "7",
               "team_id": "2", "status": "", "dob": "1999-11-01", "height_in": "80",
               "weight_lb": "225"},
    }, datetime(2026, 10, 27, tzinfo=UTC))  # fmt: skip
    rows = []
    for team, opp, pid in (("2", "17", "p1"), ("17", "2", "x9")):
        for z, (fga, fgm) in zip(
            court.ZONES,
            [(30, 18), (5, 2), (6, 3), (4, 1), (8, 3), (9, 4), (7, 2), (12, 4), (6, 2)],
            strict=True,
        ):
            rows.append({"game_id": "g1", "game_date": "2026-10-27", "team_id": team,
                         "opponent_id": opp, "player_id": pid, "zone": z, "fga": fga,
                         "fgm": fgm})  # fmt: skip
    live.Store.append(court.shots_path(data_dir, "2026-27"), court.SHOT_FIELDS, rows)
    fit = {"lambda": 3000, "home_court": 1.3, "mean_ppp": 115, "prior": {}, "players": {
        "p1": {"o": 1.0, "d": 2.0, "o_prior": 1.2, "d_prior": 2.4, "poss": 6000}}}  # fmt: skip
    impact.write(data_dir, fit, ["2025-26", "2026-27"], datetime(2026, 10, 27, tzinfo=UTC))
    venues.path(data_dir).parent.mkdir(parents=True, exist_ok=True)
    venues.path(data_dir).write_text(json.dumps({"g1": {
        "home_team_id": "2", "arena": "TD Garden", "city": "Boston", "state": "MA",
        "country": "USA", "neutral": False}}))  # fmt: skip
    out = tmp_path_factory.mktemp("site")
    web.build(data_dir, out, published_dir=config.PUBLISHED_DIR)

    index = (out / "index.html").read_text()
    assert 'href="game-g1.html"' in index and 'href="game-g2.html"' in index
    page = (out / "game-g1.html").read_text()
    assert "TD Garden" in page and "Boston, MA" in page and "Shot tendencies" in page
    chart = specs(page)["court-data"]
    assert chart["seasons"] == ["2026-27"]
    assert chart["teams"]["2"]["2026-27"]["off"]["inside"] == [30, 18]
    assert chart["teams"]["2"]["2026-27"]["def"]["inside"] == [30, 18]  # what team 17 shot
    assert "Venue not listed" in (out / "game-g2.html").read_text()  # no listing, no home arena
    assert (out / "assets/court.js").exists()

    p = json.loads((out / "players/p1.json").read_text())
    assert p["bio"] == {"age": 26, "height": "6' 8\"", "weight": 225}
    assert p["impact"]["d"] == impact.blend(fit["players"]["p1"])[1] == 2.2
    assert p["shots"]["2026-27"]["inside"] == [30, 18] and p["league"]["2026-27"]["inside"][0] == 60
