import gzip
import json
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from nbaforecast import config
from nbaforecast.games import coldcase
from nbaforecast.web import play

FIXTURES = Path(__file__).parent / "fixtures"
SLIM = json.loads(gzip.decompress((FIXTURES / "espn_plays_401810673.json.gz").read_bytes()))
GAME = {"game_id": "401810673", "season": "2025-26", "game_date": "2026-02-22",
        "venue": "State Farm Arena", "home_team_id": "1", "away_team_id": "17",
        "home_team_abbr": "ATL", "away_team_abbr": "BKN", "home_team_name": "Atlanta Hawks",
        "away_team_name": "Brooklyn Nets", "home_score": 115, "away_score": 104}  # fmt: skip
JOHNSON = "4701230"


def heights(pid):
    return {"4701230": ("6' 8\"", 80), "4278039": ("6' 10\"", 82)}.get(pid, ("6' 5\"", 77))


def test_the_hawks_24_2_run():
    case = coldcase.build_case(SLIM, GAME, {}, {}, heights)
    assert case is not None and case["team"]["abbr"] == "ATL"
    assert case["setup"]["run"] == [24, 2] and case["setup"]["before"] == -11
    assert case["setup"]["after"] == 11 and case["setup"]["period"] == 4
    sealed = coldcase.unseal(case["sealed"])
    assert sealed["culprit"] == JOHNSON and sealed["final"] == [115, 104]
    assert sealed["lines"][JOHNSON]["pts"] == 26
    ids = [s["id"] for s in case["suspects"]]
    assert len(ids) == coldcase.SUSPECTS and JOHNSON in ids
    assert set(sealed["alibis"]) == set(ids) - {JOHNSON}
    # nobody's name, and neither team's, survives in the scene
    names = [s["name"] for s in case["suspects"]] + ["Hawks", "Nets", "Atlanta", "Brooklyn"]
    assert not any(n in line["text"] for line in case["scene"] for n in names)
    assert case["scene"][-1]["score"] == [115, 104]
    assert "with 32.8 left in the 4th quarter" in sealed["story"]


def test_clues_are_checkable_and_single_out_the_culprit():
    case = coldcase.build_case(SLIM, GAME, {}, {}, heights)
    assert coldcase.MIN_CLUES <= len(case["clues"]) <= coldcase.MAX_CLUES
    assert {c["kind"] for c in case["clues"]} <= {"file", "witness"}
    culprit = next(s for s in case["suspects"] if s["id"] == JOHNSON)
    for c in case["clues"]:
        if c["kind"] == "witness":
            assert culprit["name"] not in c["text"]  # witnesses only ever clear the innocent


def test_schedule_clues_needs_to_clear_everyone():
    a = {"text": "A", "kind": "file", "clears": ["x"], "family": "a"}
    b = {"text": "B", "kind": "witness", "clears": ["y"], "family": "b"}
    assert [c["text"] for c in coldcase.schedule_clues([b, a], ["x", "y"])] == ["A", "B"]
    assert coldcase.schedule_clues([a], ["x", "y"]) is None


def test_runs_need_a_big_swing():
    def play(team, pts, text="makes shot"):
        return ({"period": 1, "clock": "5:00", "type": "Jump Shot", "text": text, "scoring": True,
                 "shooting": True, "attempted": pts, "team": team, "players": ["p"],
                 "home": 0, "away": 0}, {})  # fmt: skip

    plays = [play("h", 2), play("h", 3), play("a", 2), play("h", 3), play("h", 2), play("h", 2),
             play("h", 2), play("h", 2)]  # fmt: skip
    run = coldcase.find_run(plays, "h", "a")
    assert run["team"] == "h" and run["for"] == 16 and run["against"] == 2
    assert coldcase.find_run(plays[:4], "h", "a") is None


def test_the_published_bank_is_sound():
    folder = coldcase.bank_dir(config.PUBLISHED_DIR)
    index = json.loads((folder / "index.json").read_text())
    files = sorted(folder.glob("case-*.json"))
    assert len(files) == len(index["cases"]) >= 365
    assert index["start"] == coldcase.START.isoformat()
    teams = []
    for i, f in enumerate(files, start=1):
        case = json.loads(f.read_text())
        assert case["n"] == i and index["cases"][i - 1]["title"] == case["title"]
        sealed = coldcase.unseal(case["sealed"])
        names = {s["id"]: s["name"] for s in case["suspects"]}
        assert sealed["culprit"] in names and len(names) == coldcase.SUSPECTS
        assert coldcase.MIN_CLUES <= len(case["clues"]) <= coldcase.MAX_CLUES
        assert not any(re.search(re.escape(n), line["text"]) for line in case["scene"]
                       for n in [*names.values(), sealed["opp"]])  # fmt: skip
        assert "game_id" not in case and "_meta" not in case
        teams.append(case["team"]["id"])
    assert all(a != b for a, b in zip(teams, teams[1:], strict=False))  # never two days running
    levels = Counter(c["difficulty"] for c in index["cases"])
    assert levels["Hard"] > levels["Easy"]


def test_only_open_cases_are_published(tmp_path):
    today = coldcase.START + timedelta(days=2)  # case #3
    out = play.coldcase(config.PUBLISHED_DIR, tmp_path, today)
    assert out["today"] == 3 and len(out["cases"]) == 4  # through tomorrow's
    names = sorted(p.name for p in (tmp_path / "coldcase").iterdir())
    assert names == ["case-001.json", "case-002.json", "case-003.json", "case-004.json"]
    assert play.coldcase(config.PUBLISHED_DIR, None, coldcase.START - timedelta(days=5)) is None
    early = play.coldcase(config.PUBLISHED_DIR, None, coldcase.START - timedelta(days=1))
    assert early["today"] == 1 and len(early["cases"]) == 1  # #1 goes up the night before


def test_case_numbers():
    assert coldcase.case_number(coldcase.START) == 1
    assert coldcase.case_number(date(2027, 1, 1)) == (date(2027, 1, 1) - coldcase.START).days + 1
