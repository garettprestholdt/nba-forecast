"""Game pages (game-<id>.html): venue, team stats side by side, and both teams' shot zones."""

from pathlib import Path
from typing import Any

from nbaforecast.ingest import venues
from nbaforecast.web import fan
from nbaforecast.web.links import game_slug
from nbaforecast.web.players import Court, zone_payload
from nbaforecast.web.teams import TEAM_METRICS, ranks, team_stats

# --- game pages ---------------------------------------------------------------------------
TAPE = ["ortg", "efg", "tov", "oreb", "ftr", "three_rate",  # offense
        "drtg", "opp_efg", "opp_tov", "dreb", "opp_ftr",  # defense
        "pace"]  # fmt: skip


def _venue(game_id: str, home_id: str, known: dict[str, dict]) -> dict | None:
    """Where a game is played: ESPN's listing for it, else the home team's latest home arena."""
    if game_id in known:
        return known[game_id]
    usual = [v for v in known.values() if v.get("home_team_id") == home_id and not v["neutral"]]
    return {**usual[-1], "neutral": False, "assumed": True} if usual else None


def game_pages(
    root: Path, season: str, games: list, team_map: dict[str, fan.Team], cd: Court
) -> list[dict[str, Any]]:
    """One preview page per game on the slate: where it's played, the two teams' season stats
    side by side, and each team's shot zones (their shots, and the shots they allow) this
    season and last."""
    stats = team_stats(root, season)
    rk = ranks(stats) if len(stats) == len(team_map) else {}
    known = venues.load(root)
    meta = {m[0]: m for m in TEAM_METRICS}
    out = []
    for m in games:
        away, home = m.away.team_id, m.home.team_id
        tape = []
        for key in TAPE:
            if away not in stats or home not in stats:
                break
            _, label, side, higher, fmt = meta[key]
            a, h = stats[away][key], stats[home][key]
            better = None if key == "pace" or a == h else (
                "away" if (a > h) == higher else "home")  # fmt: skip
            tape.append({"key": key, "label": label, "side": side, "format": fmt, "away": a,
                         "home": h, "away_rank": rk.get(away, {}).get(key),
                         "home_rank": rk.get(home, {}).get(key), "better": better})  # fmt: skip
        chart = {"seasons": [s for s in cd.seasons if cd.league[s] and sum(
                    v[0] for v in cd.league[s].values())], "league": {}, "teams": {}}  # fmt: skip
        for s in chart["seasons"]:
            chart["league"][s] = zone_payload(cd.league[s])
        for tid in (away, home):
            chart["teams"][tid] = {
                s: {"off": zone_payload(cd.offense[s].get(tid)),
                    "def": zone_payload(cd.defense[s].get(tid))}
                for s in chart["seasons"]
            }  # fmt: skip
        out.append({
            "m": m, "slug": game_slug(m.game_id), "venue": _venue(m.game_id, home, known),
            "games_played": {tid: int(stats[tid]["games"]) if tid in stats else 0
                             for tid in (away, home)},
            "tape": tape, "chart": chart,
        })  # fmt: skip
    return out
