import json
from datetime import UTC, datetime
from pathlib import Path

from nbaforecast.ingest import injuries

FIXTURES = Path(__file__).parent / "fixtures"
INJ = json.loads((FIXTURES / "espn_injuries.json").read_text())
SUMMARY = json.loads((FIXTURES / "espn_summary_401909868.json").read_text())
NOW = datetime(2026, 10, 27, 14, tzinfo=UTC)


def test_parse_injuries_rows():
    rows = injuries.parse_injuries(INJ, NOW)
    assert len(rows) == sum(len(t["injuries"]) for t in INJ["injuries"])
    white = next(r for r in rows if r["player"] == "Derrick White")
    assert white["player_id"] == "3078576"
    assert white["status"] == "Day-To-Day"
    assert white["note"].startswith("White (right hip) is probable")
    assert white["injury"] == "Hip" and white["return_date"] == "2026-10-27"
    assert white["team_id"] == "2"


def test_player_id_from_profile_link():
    data = {
        "injuries": [
            {
                "id": "1",
                "displayName": "Team",
                "injuries": [
                    {
                        "status": "Out",
                        "athlete": {
                            "displayName": "P",
                            "links": [{"href": "https://www.espn.com/nba/player/_/id/123/p"}],
                        },
                    }
                ],
            }
        ]
    }
    assert injuries.parse_injuries(data, NOW)[0]["player_id"] == "123"


def test_participation_from_box_score():
    played = injuries.parse_participation(SUMMARY)
    davis = next(v for k, v in played.items() if v["minutes"] == 36)
    assert davis["played"] is True
    dnp = [v for v in played.values() if not v["played"]]
    assert dnp and all(v["minutes"] == 0 for v in dnp)
    assert any(v["reason"] == "COACH'S DECISION" for v in dnp)
    assert len({v["team_id"] for v in played.values()}) == 2
