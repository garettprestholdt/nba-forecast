"""Daily recap: an AI agent writes it; a fact-checker decides whether it gets published.

1. `build_facts` computes everything the recap is allowed to say: last night's results against
   the logged predictions, upsets, Elo-vs-market disagreements, rating moves, the season
   scorecard, and tonight's slate. The facts are saved next to the recap.
2. Claude Code (headless, in GitHub Actions) reads the facts in an isolated folder and writes a
   draft. See `prompts/recap.md`.
3. `check_recap` rejects a draft that mentions any number or team not in the facts, uses
   betting language, or is the wrong length; `template_recap` is published instead.
"""

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from nbaforecast import config, live
from nbaforecast.models.elo import EloState, win_probability

UPSET_THRESHOLD = 0.35  # the winner's pre-game Elo chance was below this
MIN_WORDS, MAX_WORDS = 60, 260
BANNED = re.compile(
    r"\b(bet|bets|betting|bettor|wager|parlay|lock|guarantee[ds]?|sure thing)\b", re.I
)
NUMBER = re.compile(r"\d+(?:\.\d+)?")
PROMPT_TEMPLATE = config.REPO_ROOT / "prompts" / "recap.md"


def _pct(p: float) -> str:
    return f"{p:.0%}"


def _signed(x: float) -> str:
    return f"{x:+.0f}"


def recap_paths(store: live.Store, day: date) -> tuple[Path, Path]:
    base = store.root / "recaps" / day.isoformat()
    return base.with_suffix(".json"), base.with_suffix(".md")


def nickname(team_name: str) -> str:
    """'Boston Celtics' -> 'Celtics', 'Portland Trail Blazers' -> 'Trail Blazers'."""
    return "Trail Blazers" if team_name.endswith("Trail Blazers") else team_name.split()[-1]


def franchise_names() -> list[str]:
    import csv

    with open(config.REPO_ROOT / "dbt" / "seeds" / "franchises.csv") as f:
        return [row["name"] for row in csv.DictReader(f)]


def build_facts(store: live.Store, state: EloState, day: date) -> dict[str, Any] | None:
    """Facts for the recap published on `day`: last night's games and tonight's slate."""
    season = live.season_for(day)
    predictions = {p["game_id"]: p for p in store.read(store.predictions_path(season))}
    yesterday = (day - timedelta(days=1)).isoformat()
    ratings_current = state.last_date is not None and state.last_date.isoformat() == yesterday

    last_night = []
    for r in store.read(store.results_path(season)):
        p = predictions.get(r["game_id"])
        if r["game_date"] != yesterday or p is None:
            continue
        home, away = r["home_team"], r["away_team"]
        hs, as_ = int(r["home_score"]), int(r["away_score"])
        home_won = hs > as_
        p_home = float(p["p_home"])
        market = float(p["market_p_home"]) if p["market_p_home"] else None
        winner, loser = (home, away) if home_won else (away, home)
        winner_elo = p_home if home_won else 1 - p_home
        game = {
            "matchup": f"{away} at {home}",
            "final_score": f"{away} {as_}, {home} {hs}",
            "winner": winner,
            "loser": loser,
            "margin": abs(hs - as_),
            "elo_pick": home if p_home >= 0.5 else away,
            "elo_chance_for_winner": _pct(winner_elo),
            "elo_correct": winner_elo >= 0.5,
            "upset_by_elo": winner_elo < UPSET_THRESHOLD,
        }
        if market is not None:
            winner_market = market if home_won else 1 - market
            game["market_chance_for_winner"] = _pct(winner_market)
            game["market_correct"] = winner_market >= 0.5
            game["elo_minus_market_for_winner_points"] = _signed((winner_elo - winner_market) * 100)
        if ratings_current:
            game["elo_rating_change"] = {
                home: _signed(state.rating(r["home_team_id"]) - float(p["home_elo"])),
                away: _signed(state.rating(r["away_team_id"]) - float(p["away_elo"])),
            }
        last_night.append(game)

    tonight = []
    for p in predictions.values():
        if p["game_date"] != day.isoformat():
            continue
        p_home = float(p["p_home"])
        entry = {
            "matchup": f"{p['away_team']} at {p['home_team']}",
            "tip_off": datetime.fromisoformat(p["tipoff_utc"])
            .astimezone(live.espn.EASTERN)
            .strftime("%-I:%M %p ET"),
            "elo_pick": p["home_team"] if p_home >= 0.5 else p["away_team"],
            "elo_chance_for_pick": _pct(max(p_home, 1 - p_home)),
            "_p_home": p_home,
        }
        if p["market_p_home"]:
            market = float(p["market_p_home"])
            entry["market_chance_for_elo_pick"] = _pct(market if p_home >= 0.5 else 1 - market)
            entry["elo_minus_market_points"] = _signed(
                (p_home - market) * 100 * (1 if p_home >= 0.5 else -1)
            )
            entry["_gap"] = abs(p_home - market)
        tonight.append(entry)

    if not last_night and not tonight:
        return None

    highlights = {}
    if tonight:
        highlights["closest_game"] = min(tonight, key=lambda g: abs(g["_p_home"] - 0.5))["matchup"]
        with_gap = [g for g in tonight if "_gap" in g]
        if with_gap:
            highlights["biggest_elo_market_disagreement"] = max(with_gap, key=lambda g: g["_gap"])[
                "matchup"
            ]
    for g in tonight:
        g.pop("_p_home", None)
        g.pop("_gap", None)

    with_market = [g for g in last_night if "market_correct" in g]
    card_path = store.scorecard_path(season)
    card = json.loads(card_path.read_text()) if card_path.exists() else {}
    elo_card = card.get("elo") or {}
    h2h = card.get("head_to_head_with_market") or {}
    hca = state.home_court()
    return {
        "recap_date": day.isoformat(),
        "recap_date_label": day.strftime("%A, %B %-d"),
        "last_night_date": yesterday,
        "last_night": last_night,
        "last_night_summary": {
            "games": len(last_night),
            "elo_correct": sum(g["elo_correct"] for g in last_night),
            "games_with_market": len(with_market),
            "market_correct": sum(g["market_correct"] for g in with_market),
            "upsets_by_elo": sum(g["upset_by_elo"] for g in last_night),
        },
        "tonight": tonight,
        "tonight_summary": {"games": len(tonight)},
        "tonight_highlights": highlights,
        "season_to_date": {
            "games_scored": card.get("games_scored", 0),
            "elo_log_loss": f"{elo_card['log_loss']:.3f}" if "log_loss" in elo_card else None,
            "elo_pick_accuracy": _pct(elo_card["accuracy"]) if "accuracy" in elo_card else None,
            "coin_flip_log_loss": "0.693",
            "games_with_market": h2h.get("games", 0),
            "elo_log_loss_on_market_games": f"{h2h['elo']['log_loss']:.3f}"
            if h2h.get("elo")
            else None,
            "market_log_loss_on_market_games": f"{h2h['market']['log_loss']:.3f}"
            if h2h.get("market")
            else None,
        },
        "home_court": {
            "elo_points": f"{hca:.0f}",
            "home_chance_between_equal_teams": _pct(win_probability(hca)),
        },
    }


def allowed_numbers(facts: dict) -> set[str]:
    return set(NUMBER.findall(json.dumps(facts)))


def _strings(node) -> list[str]:
    if isinstance(node, dict):
        return [s for v in node.values() for s in _strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in _strings(v)]
    return [str(node)]


def typed_numbers(facts: dict) -> dict[str, set[str]]:
    """Numbers in the facts by kind, so a real number used in the wrong place is still caught."""
    values = _strings(facts)
    counts = {
        str(v)
        for section in ("last_night_summary", "tonight_summary")
        for v in facts.get(section, {}).values()
    }
    season = facts.get("season_to_date", {})
    counts |= {str(season.get("games_scored", "")), str(season.get("games_with_market", ""))}
    return {
        "percent": {v[:-1] for v in values if re.fullmatch(r"\d+%", v)},
        "decimal": {v for v in values if re.fullmatch(r"\d+\.\d+", v)},
        "count": counts - {""},
    }


# "87%", "0.612", "4 games", "3 of 4": each kind must match a number of the same kind.
PERCENT_IN_TEXT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent)")
COUNT_IN_TEXT = re.compile(r"\b(\d+)\s+(?:games?|winners?|picks?|upsets?|matchups?)\b", re.I)
OF_IN_TEXT = re.compile(r"\b(\d+)\s+(?:of|out of)\s+(\d+)\b", re.I)


def facts_teams(facts: dict) -> set[str]:
    text = json.dumps(facts)
    return {name for name in franchise_names() if name in text}


def check_recap(text: str, facts: dict) -> list[str]:
    """Problems that block publishing. An empty list means the draft can go out."""
    problems = []
    words = len(text.split())
    if not MIN_WORDS <= words <= MAX_WORDS:
        problems.append(f"length {words} words (allowed {MIN_WORDS}-{MAX_WORDS})")
    allowed = allowed_numbers(facts)
    for token in sorted(set(NUMBER.findall(text)) - allowed):
        problems.append(f"number {token!r} is not in the facts")
    kinds = typed_numbers(facts)
    for token in sorted(set(PERCENT_IN_TEXT.findall(text)) - kinds["percent"]):
        problems.append(f"percentage '{token}%' is not a percentage in the facts")
    decimals = {t for t in NUMBER.findall(text) if "." in t}
    for token in sorted(decimals - kinds["decimal"] - {f"{p}" for p in kinds["percent"]}):
        problems.append(f"decimal {token!r} is not a decimal in the facts")
    counted = set(COUNT_IN_TEXT.findall(text)) | {
        n for pair in OF_IN_TEXT.findall(text) for n in pair
    }
    for token in sorted(counted - kinds["count"]):
        problems.append(f"count {token!r} doesn't match any count in the facts")
    mentioned = facts_teams(facts)
    for name in franchise_names():
        if name not in mentioned and re.search(rf"\b{re.escape(nickname(name))}\b", text):
            problems.append(f"team {nickname(name)!r} is not in the facts")
    for match in sorted({m.group(0).lower() for m in BANNED.finditer(text)}):
        problems.append(f"betting language: {match!r}")
    if "http" in text or "<" in text:
        problems.append("links or HTML are not allowed")
    return problems


def template_recap(facts: dict) -> str:
    """Plain, deterministic recap: published when the agent's draft fails the fact-check."""
    paragraphs = []
    s = facts["last_night_summary"]
    if facts["last_night"]:
        line = f"Last night Elo picked {s['elo_correct']} of {s['games']} winners"
        if s["games_with_market"]:
            line += f"; the market picked {s['market_correct']} of {s['games_with_market']}"
        upsets = [g for g in facts["last_night"] if g["upset_by_elo"]]
        line += "."
        if upsets:
            u = upsets[0]
            line += (
                f" The biggest upset: {u['winner']} beat {u['loser']}"
                f" with an Elo chance of {u['elo_chance_for_winner']}."
            )
        paragraphs.append(line)
        paragraphs.append("\n".join(f"- {g['final_score']}" for g in facts["last_night"]))
    if facts["tonight"]:
        h = facts["tonight_highlights"]
        games = facts["tonight_summary"]["games"]
        line = f"Tonight: {games} games. The closest by Elo is {h['closest_game']}."
        if "biggest_elo_market_disagreement" in h:
            line += f" Elo and the market disagree most on {h['biggest_elo_market_disagreement']}."
        paragraphs.append(line)
    season = facts["season_to_date"]
    if season["elo_log_loss"]:
        paragraphs.append(
            f"Season to date: Elo log-loss {season['elo_log_loss']} over"
            f" {season['games_scored']} games (coin flip: {season['coin_flip_log_loss']})."
        )
    return "\n\n".join(paragraphs)


def render_prompt(facts_file: str, draft_file: str) -> str:
    return PROMPT_TEMPLATE.read_text().format(facts_file=facts_file, draft_file=draft_file)


def prepare(
    store: live.Store, state: EloState, day: date, workdir: Path, force: bool = False
) -> Path | None:
    """Write the facts (data branch + agent workdir) and the agent prompt.

    Returns the prompt path, or None when there's nothing to recap or today's recap exists.
    `force` ignores an existing recap; only the recap test uses it, on a throwaway copy.
    """
    facts_path, recap_path = recap_paths(store, day)
    if recap_path.exists() and not force:
        return None
    facts = build_facts(store, state, day)
    if facts is None:
        return None
    text = json.dumps(facts, indent=2) + "\n"
    facts_path.parent.mkdir(parents=True, exist_ok=True)
    facts_path.write_text(text)
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "facts.json").write_text(text)
    prompt = workdir / "prompt.txt"
    prompt.write_text(render_prompt("facts.json", "recap_draft.md"))
    return prompt


def publish(store: live.Store, day: date, draft: str | None, model: str) -> dict[str, Any]:
    """Fact-check the agent's draft and write today's recap (the draft, or the template)."""
    facts_path, recap_path = recap_paths(store, day)
    facts = json.loads(facts_path.read_text())
    problems = ["no draft (agent didn't run or failed)"] if not draft else check_recap(draft, facts)
    if problems:
        body, source = template_recap(facts), "template"
    else:
        body, source = draft.strip(), "agent"
    header = f"<!-- source: {source}; model: {model if source == 'agent' else 'none'} -->\n"
    recap_path.write_text(header + body + "\n")
    return {"source": source, "problems": problems, "path": str(recap_path)}


def read_published(path: Path) -> tuple[str, str]:
    """(source, body) of a published recap file."""
    text = path.read_text()
    m = re.match(r"<!-- source: (\w+);[^>]*-->\n", text)
    return (m.group(1) if m else "unknown"), text[m.end() if m else 0 :].strip()
