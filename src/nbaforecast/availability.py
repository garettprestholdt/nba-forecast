"""Will a Day-To-Day player play tonight? An AI agent vs. a keyword rule, scored by box scores.

Each morning:
1. `prepare` scores yesterday's calls against box scores, snapshots ESPN's injury list, and
   picks today's candidates: every player not listed "Out" whose team plays today, before
   tip-off. Each gets a keyword-rule probability (the baseline) and goes to the agent.
2. Claude Code reads each player's injury note and estimates the chance they play, quoting
   the sentence it relied on. See `prompts/availability.md`.
3. `publish` validates the agent's answers (known player, probability in range, quote really
   in the note) and appends one row per candidate to an append-only log.
The next morning, box scores decide who played; both methods get the same scoring rules.
"""

import json
import re
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from nbaforecast import config, live, metrics
from nbaforecast.ingest import injuries
from nbaforecast.ingest.espn import EASTERN

PROMPT_TEMPLATE = config.REPO_ROOT / "prompts" / "availability.md"
MIN_QUOTE_CHARS = 10

FIELDS = [
    "game_date",
    "game_id",
    "tipoff_utc",
    "player_id",
    "player",
    "team_id",
    "team",
    "opponent",
    "status",
    "note",
    "note_updated",
    "baseline_p",
    "agent_p",
    "agent_quote",
    "minutes_restriction",
    "predicted_at",
]
RESULT_FIELDS = ["game_id", "player_id", "played", "minutes", "reason", "resolved_at"]

# The baseline: the first matching phrase wins. Deliberately simple and date-blind; the
# agent's job is to beat it, especially on notes about a different day's game.
KEYWORD_RULES = [
    (
        r"ruled out|will not play|won't play|will miss|\bis out\b"
        r"|not expected to play|sidelined",
        0.05,
    ),
    (r"\bdoubtful\b", 0.20),
    (r"\bquestionable\b", 0.50),
    (
        r"\bprobable\b|expected to play|will play|is available"
        r"|cleared to play|upgraded to available",
        0.90,
    ),
]  # fmt: skip
DEFAULT_P = 0.50

Fetch = Callable[..., dict]


def keyword_baseline(note: str) -> float:
    text = note.lower()
    for pattern, p in KEYWORD_RULES:
        if re.search(pattern, text):
            return p
    return DEFAULT_P


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def paths(store: live.Store, season: str) -> tuple[Path, Path]:
    return (
        store.root / "availability" / f"{season}.csv",
        store.root / "availability" / f"{season}-results.csv",
    )


def todays_games(store: live.Store, day: date, now: datetime) -> dict[str, dict[str, Any]]:
    """team_id -> the game it plays today (not yet started), from today's predictions."""
    season = live.season_for(day)
    games = {}
    for p in store.read(store.predictions_path(season)):
        if p["game_date"] != day.isoformat() or datetime.fromisoformat(p["tipoff_utc"]) <= now:
            continue
        for team_id, team, opp in [
            (p["home_team_id"], p["home_team"], p["away_team"]),
            (p["away_team_id"], p["away_team"], p["home_team"]),
        ]:
            games[team_id] = {
                "game_id": p["game_id"],
                "tipoff_utc": p["tipoff_utc"],
                "team": team,
                "opponent": opp,
            }
    return games


def candidates(injury_rows: list[dict], games: dict[str, dict]) -> list[dict[str, Any]]:
    out = []
    for r in injury_rows:
        game = games.get(r["team_id"])
        if game is None or r["status"] == "Out" or not r["note"]:
            continue
        tip = datetime.fromisoformat(game["tipoff_utc"]).astimezone(EASTERN)
        out.append(
            {
                **{k: r[k] for k in ("player_id", "player", "team_id", "status", "note")},
                "note_detail": r["note_detail"],
                "note_updated": r["note_updated"],
                "injury": r["injury"],
                "team": game["team"],
                "opponent": game["opponent"],
                "game_id": game["game_id"],
                "tipoff_utc": game["tipoff_utc"],
                "tip_off_et": tip.strftime("%a %b %-d, %-I:%M %p ET"),
                "baseline_p": keyword_baseline(r["note"]),
            }
        )
    return out


def resolve(store: live.Store, season: str, fetch_summary: Fetch, now: datetime) -> int:
    """Record who actually played, for logged calls whose game is now final."""
    log_path, results_path = paths(store, season)
    finals = {r["game_id"] for r in store.read(store.results_path(season))}
    done = {(r["game_id"], r["player_id"]) for r in store.read(results_path)}
    pending: dict[str, list[dict]] = {}
    for row in store.read(log_path):
        if row["game_id"] in finals and (row["game_id"], row["player_id"]) not in done:
            pending.setdefault(row["game_id"], []).append(row)
    new = []
    for game_id, rows in pending.items():
        box = injuries.parse_participation(fetch_summary(game_id))
        for row in rows:
            entry = box.get(row["player_id"])
            new.append(
                {
                    "game_id": game_id,
                    "player_id": row["player_id"],
                    "played": bool(entry and entry["played"]),
                    "minutes": entry["minutes"] if entry else 0,
                    "reason": (entry["reason"] if entry else "inactive (not in box score)"),
                    "resolved_at": now.isoformat(timespec="seconds"),
                }
            )
    return store.append(results_path, RESULT_FIELDS, new)


def prepare(
    store: live.Store,
    day: date,
    now: datetime,
    workdir: Path,
    fetch_injuries: Fetch,
    fetch_summary: Fetch,
) -> dict[str, Any]:
    season = live.season_for(day)
    resolved = resolve(store, season, fetch_summary, now)
    rows = injuries.parse_injuries(fetch_injuries(), now)
    snapshot = store.root / "injuries" / f"{day.isoformat()}.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(json.dumps(rows, indent=1) + "\n")

    log_path, _ = paths(store, season)
    logged = {(r["game_id"], r["player_id"]) for r in store.read(log_path)}
    todo = [
        c
        for c in candidates(rows, todays_games(store, day, now))
        if (c["game_id"], c["player_id"]) not in logged
    ]
    # The agent works in workdir/agent; the keyword rule's answers stay one level up, where the
    # agent can't read them.
    agent_dir = workdir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "candidates.json").write_text(
        json.dumps(
            {
                "today": day.isoformat(),
                "today_weekday": day.strftime("%A"),
                "players": [{k: v for k, v in c.items() if k != "baseline_p"} for c in todo],
            },
            indent=1,
        )
        + "\n"
    )
    (workdir / "baseline.json").write_text(json.dumps(todo, indent=1) + "\n")
    (agent_dir / "prompt.txt").write_text(PROMPT_TEMPLATE.read_text())
    return {"resolved": resolved, "injured": len(rows), "candidates": len(todo)}


def validate(answer_text: str | None, todo: list[dict]) -> tuple[dict[str, dict], list[str]]:
    """Accepted agent answers by player_id, plus problems. Bad entries are dropped, not fixed."""
    if not answer_text:
        return {}, ["no agent output"]
    try:
        players = json.loads(answer_text)["players"]
    except (ValueError, KeyError, TypeError) as exc:
        return {}, [f"output isn't the expected JSON: {exc}"]
    by_id = {c["player_id"]: c for c in todo}
    accepted, problems = {}, []
    for item in players:
        pid = str(item.get("player_id", ""))
        if pid not in by_id:
            problems.append(f"unknown player_id {pid!r}")
            continue
        if pid in accepted:
            problems.append(f"{pid}: answered twice")
            continue
        p = item.get("p_play")
        if not isinstance(p, int | float) or not 0 <= p <= 1:
            problems.append(f"{by_id[pid]['player']}: p_play {p!r} isn't a probability")
            continue
        quote = str(item.get("quote", ""))
        sources = _norm(by_id[pid]["note"] + " " + by_id[pid]["note_detail"])
        if len(quote.strip()) < MIN_QUOTE_CHARS or _norm(quote) not in sources:
            problems.append(f"{by_id[pid]['player']}: quote isn't from the player's note")
            continue
        accepted[pid] = {
            "p": float(p),
            "quote": quote.strip(),
            "minutes_restriction": bool(item.get("minutes_restriction", False)),
        }
    return accepted, problems


def publish(store: live.Store, day: date, now: datetime, workdir: Path) -> dict[str, Any]:
    season = live.season_for(day)
    todo = json.loads((workdir / "baseline.json").read_text())
    answer_path = workdir / "agent" / "availability.json"
    answer = answer_path.read_text() if answer_path.exists() else None
    accepted, problems = validate(answer, todo)

    rows = []
    for c in todo:
        if datetime.fromisoformat(c["tipoff_utc"]) <= now:
            continue  # tipped off while we worked: never log a late call
        a = accepted.get(c["player_id"])
        rows.append(
            {
                "game_date": day.isoformat(),
                "game_id": c["game_id"],
                "tipoff_utc": c["tipoff_utc"],
                "player_id": c["player_id"],
                "player": c["player"],
                "team_id": c["team_id"],
                "team": c["team"],
                "opponent": c["opponent"],
                "status": c["status"],
                "note": c["note"],
                "note_updated": c["note_updated"],
                "baseline_p": c["baseline_p"],
                "agent_p": round(a["p"], 3) if a else "",
                "agent_quote": a["quote"] if a else "",
                "minutes_restriction": a["minutes_restriction"] if a else "",
                "predicted_at": now.isoformat(timespec="seconds"),
            }
        )
    log_path, _ = paths(store, season)
    store.append(log_path, FIELDS, rows)
    card = scorecard(store, season, now)
    card_path = store.root / "scorecard" / f"{season}-availability.json"
    card_path.parent.mkdir(parents=True, exist_ok=True)
    card_path.write_text(json.dumps(card, indent=2) + "\n")
    return {
        "logged": len(rows),
        "agent_answers": sum(1 for r in rows if r["agent_p"] != ""),
        "problems": problems,
        "scorecard": card,
    }


def _scores(probs: list[float], outcomes: list[int]) -> dict[str, float] | None:
    if not probs:
        return None
    return {
        "log_loss": round(metrics.log_loss(probs, outcomes), 4),
        "brier": round(metrics.brier_score(probs, outcomes), 4),
    }


def scorecard(store: live.Store, season: str, now: datetime) -> dict[str, Any]:
    log_path, results_path = paths(store, season)
    outcomes = {(r["game_id"], r["player_id"]): r for r in store.read(results_path)}
    scored = [
        (row, int(outcomes[k]["played"] == "True"))
        for row in store.read(log_path)
        if (k := (row["game_id"], row["player_id"])) in outcomes
    ]
    both = [(row, y) for row, y in scored if row["agent_p"] != ""]
    return {
        "season": season,
        "updated_at": now.isoformat(timespec="seconds"),
        "calls_logged": len(store.read(log_path)),
        "calls_scored": len(scored),
        "played_rate": round(sum(y for _, y in scored) / len(scored), 3) if scored else None,
        "keyword_rule_all": _scores(
            [float(r["baseline_p"]) for r, _ in scored], [y for _, y in scored]
        ),
        "head_to_head": {
            "calls": len(both),
            "agent": _scores([float(r["agent_p"]) for r, _ in both], [y for _, y in both]),
            "keyword_rule": _scores(
                [float(r["baseline_p"]) for r, _ in both], [y for _, y in both]
            ),
        },
    }
