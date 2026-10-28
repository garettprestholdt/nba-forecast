"""Cold Case: a daily NBA whodunit built from real play-by-play.

Each case is a real scoring run (14+ points while allowing 6 or fewer) with one clear culprit:
the player who produced the most of it (points + 2 per assist), with 40%+ of the run's points
produced and a 4+ lead on the next teammate. The player sees:

- the scene: the run's play-by-play with every name blacked out (shot types, distances,
  assists and steals left in) and the score flow;
- six suspects (the team's six biggest minute-getters that night, culprit included), each
  with a file: position, height, jersey, starter or bench, that season's per-game line and
  where they took their shots;
- clues on request: true facts about the culprit from the play-by-play and the files, one
  family each (height, assists in the run, an alibi...), scheduled so every clue clears at
  least one more suspect and together they leave exactly one.

Date, opponent, arena, final score, everyone's line that night and the alibis are "sealed"
(base64 JSON): enough that the answer isn't sitting in plain text, not real secrecy.

Cases are generated locally once (`nbaf coldcase-bank`) into `data/published/coldcase/`; the
site publishes one per day starting at `START` (Central time), never ahead of time.
"""

import base64
import json
import logging
import random
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any

from nbaforecast.ingest import playbyplay as pbp
from nbaforecast.ingest.boxscores import parse_boxscore

log = logging.getLogger(__name__)

START = date(2026, 10, 28)  # case #1
MIN_SWING, MAX_AGAINST = 14, 6
MIN_SHARE, MIN_LEAD = 0.4, 4
SUSPECTS = 6
MIN_CLUES, MAX_CLUES = 3, 5
REDACT = "█" * 8
QUARTERS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}


def bank_dir(published: Path) -> Path:
    return Path(published) / "coldcase"


def case_path(published: Path, n: int) -> Path:
    return bank_dir(published) / f"case-{n:03d}.json"


def case_number(day: date) -> int:
    """Case #1 on START, one more each day."""
    return (day - START).days + 1


# --- reading a game ---------------------------------------------------------------------
def ordered_plays(
    slim: dict[str, Any], game: dict[str, Any]
) -> tuple[list[tuple[dict, dict]], list]:
    """Every non-substitution play in order, with the set of players on the floor for each
    team when it happened (the box-score rebuild's lineup helpers), and the box-score roster."""
    roster = parse_boxscore(slim, game)
    team_of = {r["player_id"]: r["team_id"] for r in roster}
    home, away = str(game["home_team_id"]), str(game["away_team_id"])
    on: dict[str, set[str]] = {home: set(), away: set()}
    for r in roster:
        if r["starter"]:
            on[r["team_id"]].add(r["player_id"])
    by_period: dict[int, list[dict]] = defaultdict(list)
    for p in slim["plays"]:
        if p["period"]:
            by_period[p["period"]].append(p)
    out = []
    for period in sorted(by_period):
        plays = by_period[period]
        if period > 1:
            on = pbp._period_starters(plays, team_of, on)
        for p in pbp._subs_last(plays):
            if pbp._kind(p) == "sub":
                entering, leaving = pbp._sub_sides(p)
                t = team_of.get(entering or "") or team_of.get(leaving or "")
                if t in on:
                    on[t].discard(leaving or "")
                    if entering:
                        on[t].add(entering)
                continue
            out.append((p, {t: frozenset(s) for t, s in on.items()}))
    return out, roster


def find_run(plays: list[tuple[dict, dict]], home: str, away: str) -> dict[str, Any] | None:
    """The biggest run in a game: from one of the run team's baskets to another, outscoring
    the other team by MIN_SWING+ while allowing MAX_AGAINST or fewer."""
    scoring = [
        (i, p) for i, (p, _) in enumerate(plays) if pbp._points(p) and p["team"] in (home, away)
    ]
    best = None
    for a in range(len(scoring)):
        pts = {home: 0, away: 0}
        team = scoring[a][1]["team"]
        other = away if team == home else home
        for b in range(a, len(scoring)):
            i, p = scoring[b]
            pts[p["team"]] += pbp._points(p)
            if pts[other] > MAX_AGAINST:
                break
            swing = pts[team] - pts[other]
            if p["team"] == team and swing >= MIN_SWING and (best is None or swing > best["swing"]):
                best = {"team": team, "start": scoring[a][0], "end": i, "for": pts[team],
                        "against": pts[other], "swing": swing}  # fmt: skip
    return best


def _produced(run: list[dict], team: str) -> Counter:
    out: Counter = Counter()
    for p in run:
        if p["team"] == team and p["players"] and pbp._points(p):
            out[p["players"][0]] += pbp._points(p)
            if len(p["players"]) > 1 and "assist" in p["text"].lower():
                out[p["players"][1]] += 2
    return out


def _second(p: dict) -> str | None:
    return p["players"][1] if len(p["players"]) > 1 and p["players"][1] else None


def _shot_zone(text: str) -> str | None:
    """Coarse zone from a play's text: three, midrange (14+ ft two) or inside."""
    t = text.lower()
    if "three point" in t:
        return "three"
    m = re.search(r"(\d+)-foot", t)
    if m:
        return "mid" if int(m.group(1)) >= 14 else "inside"
    if re.search(r"dunk|layup|tip|hook|alley", t):
        return "inside"
    return None


def shot_profiles(games: Iterable[dict[str, Any]]) -> dict[str, Counter]:
    """player id -> attempts by coarse zone, over a season of slim summaries."""
    out: dict[str, Counter] = defaultdict(Counter)
    for slim in games:
        for p in slim["plays"]:
            if pbp._kind(p) == "fg" and p["players"] and p["players"][0]:
                z = _shot_zone(p["text"])
                if z:
                    out[p["players"][0]][z] += 1
    return out


# --- building a case ----------------------------------------------------------------------
def _facts(pid: str, run: list[tuple[dict, dict]], team: str, roster_row: dict) -> dict[str, Any]:
    mine = [p for p, _ in run if p["team"] == team and p["players"] and p["players"][0] == pid]
    made = [p for p in mine if pbp._kind(p) == "fg" and p["scoring"]]
    sat = [(p["period"], p["clock"]) for p, floor in run if pid not in floor.get(team, ())]
    second = [(p, _second(p)) for p, _ in run]
    return {
        "pts": sum(pbp._points(p) for p in mine),
        "threes": sum(1 for p in made if pbp._shot_value(p) == 3),
        "fga3": sum(1 for p in mine if pbp._kind(p) == "fg" and pbp._shot_value(p) == 3),
        "rim": sum(1 for p in made if re.search(r"dunk|layup", p["text"], re.I)),
        "ast": sum(1 for p, s in second if s == pid and "assist" in p["text"].lower()),
        "stl": sum(1 for p, s in second if s == pid and "steal" in p["text"].lower()),
        "blk": sum(1 for p, s in second if s == pid and "block" in p["text"].lower()),
        "fed": sorted(
            {p["players"][0] for p, s in second if s == pid and "assist" in p["text"].lower()}
        ),  # fmt: skip
        "whole_run": not sat,
        "sat": sat,
        "starter": bool(roster_row["starter"]),
        "game": {k: int(roster_row[k]) for k in ("minutes", "pts", "reb", "ast")},
    }


def _clue_candidates(c: dict, others: list[dict]) -> list[dict]:
    """True statements about the culprit `c`, each with the suspects it clears."""
    out = []

    def add(text: str, kind: str, pred: Callable[[dict], bool], family: str | None = None):
        if pred(c):
            cleared = [o["id"] for o in others if not pred(o)]
            if cleared:
                out.append(
                    {"text": text, "kind": kind, "clears": cleared, "family": family or text}
                )

    f = c["run"]
    pos = {"G": "guard", "F": "forward", "C": "center"}.get(c["pos"])
    if pos:
        add(f"The culprit is listed as a {pos}.", "file", lambda s: s["pos"] == c["pos"])
    if c["inches"]:
        for cut in range(72, 87):
            label = f"{cut // 12}'{cut % 12}\""
            if c["inches"] >= cut:
                add(f"The culprit stands {label} or taller.", "file",
                    lambda s, x=cut: (s["inches"] or 0) >= x, "height")  # fmt: skip
            else:
                add(f"The culprit is shorter than {label}.", "file",
                    lambda s, x=cut: (s["inches"] or 99) < x, "height")  # fmt: skip
    if f["starter"]:
        add("The culprit was in the starting five.", "file", lambda s: s["run"]["starter"])
    else:
        add("The culprit came off the bench.", "file", lambda s: not s["run"]["starter"])
    if c["jersey"].isdigit():
        odd = int(c["jersey"]) % 2
        add(f"The culprit's jersey number is {'odd' if odd else 'even'}.", "file",
            lambda s: s["jersey"].isdigit() and int(s["jersey"]) % 2 == odd)  # fmt: skip
    if c.get("season"):
        ppg = c["season"]["pts"]
        cut = 10 if ppg >= 10 else None
        if cut:
            add("The culprit averaged double figures that season.", "file",
                lambda s: (s.get("season") or {}).get("pts", 0) >= 10)  # fmt: skip
        else:
            add("The culprit averaged single digits that season.", "file",
                lambda s: (s.get("season") or {}).get("pts", 99) < 10)  # fmt: skip
    for o in others:  # witnesses: each clears one named suspect, with the tape as the reason
        add(f"{o['name']} is in the clear: {_reason(o['run'])}.", "witness",
            lambda s, x=o["id"]: s["id"] != x, f"witness-{o['id']}")  # fmt: skip
    return out


ORDER = {"file": 0, "witness": 1}


def _clue_cost(c: dict, left: frozenset, kinds: Counter) -> tuple:
    new = len(set(c["clears"]) & left)
    return (new + 0.6 * kinds[c["kind"]], ORDER[c["kind"]], c["text"])


def schedule_clues(candidates: list[dict], others: list[str]) -> list[dict] | None:
    """Vague to sharp, with variety: each clue clears as few new suspects as possible (at least
    one), a kind of clue already used costs a little more, and the suspect files come before
    the witnesses. One clue per family. None if the clues can't single out the culprit.

    Every clue is checkable on the page: file clues against the suspect files, witnesses by
    name. (Facts like "the culprit had a steal in the run" aren't, since the scene is redacted.)"""
    left, chosen, used, kinds = set(others), [], set(), Counter()
    while left:
        useful = [c for c in candidates if set(c["clears"]) & left and c["family"] not in used]
        if not useful:
            return None
        cost = partial(_clue_cost, left=frozenset(left), kinds=Counter(kinds))
        pick = min(useful, key=cost)
        chosen.append(pick)
        used.add(pick["family"])
        kinds[pick["kind"]] += 1
        left -= set(pick["clears"])
    return chosen


def _redact(text: str, names: list[str]) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    for name in sorted(names, key=len, reverse=True):
        if name:
            text = text.replace(name, REDACT)
    return text


def _title(run: dict, before: int, after: int, period: int) -> str:
    score = f"{run['for']}–{run['against']}"
    if before < 0 < after and period >= 4:
        return f"The fourth-quarter {score} heist"
    if before < 0 < after:
        return f"The {score} turnaround"
    return f"The {score} job"


def seal(obj: Any) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def unseal(text: str) -> Any:
    return json.loads(base64.b64decode(text))


def build_case(
    slim: dict[str, Any],
    game: dict[str, Any],
    season_lines: dict[tuple[str, str], dict],
    shot_mix: dict[str, Counter],
    heights: Callable[[str], tuple[str, int | None]],
) -> dict[str, Any] | None:
    """One case from one game, or None if it has no clean run with a clear culprit.

    `game` is a games row (ids, names, abbrs, date, venue, scores); `season_lines` maps
    (team id, player id) -> that season's per-game line for the team ({"g","pts","reb","ast",
    "stl","blk"});
    `heights` returns (display height, inches) for a player id."""
    plays, roster = ordered_plays(slim, game)
    home, away = str(game["home_team_id"]), str(game["away_team_id"])
    run = find_run(plays, home, away)
    if run is None or not roster:
        return None
    team = run["team"]
    span = plays[run["start"] : run["end"] + 1]
    produced = _produced([p for p, _ in span], team)
    ranked = produced.most_common()
    if len(ranked) < 2:
        return None
    (culprit, top), (_, second) = ranked[0], ranked[1]
    if top < MIN_SHARE * run["for"] or top - second < MIN_LEAD:
        return None
    if any(len(floor.get(team, ())) != 5 for _, floor in span):
        return None  # a dropped substitution: the floor clues wouldn't be trustworthy
    mine = sorted(
        (r for r in roster if r["team_id"] == team and r["minutes"]), key=lambda r: -r["minutes"]
    )
    ids = [r["player_id"] for r in mine[:SUSPECTS]]
    if culprit not in ids:
        if culprit not in {r["player_id"] for r in mine}:
            return None
        ids[-1] = culprit
    if len(ids) < SUSPECTS:
        return None
    rows = {r["player_id"]: r for r in roster}
    jerseys = _jerseys(slim)
    suspects = []
    for pid in ids:
        ht, inches = heights(pid)
        mix = shot_mix.get(pid, Counter())
        n = sum(mix.values())
        suspects.append({
            "id": pid, "name": rows[pid]["player"], "pos": rows[pid]["position"], "ht": ht,
            "inches": inches, "jersey": jerseys.get(pid, ""),
            "season": season_lines.get((team, pid)),
            "shots": ({z: round(mix[z] / n, 2) for z in ("inside", "mid", "three")}
                      if n >= 100 else None),
            "run": _facts(pid, span, team, rows[pid]),
        })  # fmt: skip
    c = next(s for s in suspects if s["id"] == culprit)
    others = [s for s in suspects if s["id"] != culprit]
    clues = schedule_clues(_clue_candidates(c, others), [o["id"] for o in others])
    if clues is None or not MIN_CLUES <= len(clues) <= MAX_CLUES:
        return None

    first, last = span[0][0], span[-1][0]
    us_home = team == home
    # the margin before the run started: undo the first basket
    before = (first["home"] - first["away"]) * (1 if us_home else -1) - pbp._points(first)
    after = (last["home"] - last["away"]) * (1 if us_home else -1)
    names = [r["player"] for r in roster] + [game["home_team_name"], game["away_team_name"],
             game["home_team_name"].split()[-1], game["away_team_name"].split()[-1]]  # fmt: skip
    scene = []
    for p, _ in span:
        kind = pbp._kind(p)
        if kind not in ("fg", "ft", "tov") or (kind == "ft" and not p["scoring"]):
            continue
        us, them = (p["home"], p["away"]) if us_home else (p["away"], p["home"])
        scene.append({"q": p["period"], "clock": p["clock"], "us": p["team"] == team,
                      "made": bool(p["scoring"]), "text": _redact(p["text"], names),
                      "score": [us, them]})  # fmt: skip
    team_name = game["home_team_name"] if us_home else game["away_team_name"]
    opp_name = game["away_team_name"] if us_home else game["home_team_name"]
    finals = (
        (game["home_score"], game["away_score"])
        if us_home
        else (game["away_score"], game["home_score"])
    )
    c_name = c["name"]
    story = _story(
        c, team_name, opp_name, run, before, first, last, plays_of=[p for p, _ in span], team=team
    )
    return {
        "game_id": str(game["game_id"]),
        "title": _title(run, before, after, first["period"]),
        "team": {
            "id": team,
            "name": team_name,
            "abbr": game["home_team_abbr"] if us_home else game["away_team_abbr"],
        },  # fmt: skip
        "setup": {
            "period": first["period"],
            "clock": first["clock"],
            "before": before,
            "after": after,
            "run": [run["for"], run["against"]],
            "season": game["season"],
        },  # fmt: skip
        "scene": scene,
        "suspects": [
            {k: s[k] for k in ("id", "name", "pos", "ht", "jersey", "season", "shots")}
            | {"starter": s["run"]["starter"]}
            for s in suspects
        ],  # fmt: skip
        "clues": [{"text": x["text"], "kind": x["kind"]} for x in clues],
        "sealed": seal(
            {
                "culprit": culprit,
                "name": c_name,
                "date": str(game["game_date"]),
                "opp": opp_name,
                "venue": game.get("venue") or "",
                "final": [int(finals[0]), int(finals[1])],
                "story": story,
                "lines": {s["id"]: s["run"]["game"] for s in suspects},
                "alibis": {o["id"]: _alibi(o["run"]) for o in others},
            }
        ),  # fmt: skip
        "_meta": {
            "culprit": culprit,
            "swing": run["swing"],
            "flipped": before < 0 < after,
            "q4": first["period"] >= 4,
        },  # fmt: skip
    }


def _jerseys(slim: dict[str, Any]) -> dict[str, str]:
    out = {}
    for team in slim.get("boxscore", {}).get("players", []):
        for block in team.get("statistics", [])[:1]:
            for a in block.get("athletes", []):
                athlete = a.get("athlete") or {}
                if athlete.get("id"):
                    out[str(athlete["id"])] = str(athlete.get("jersey", ""))
    return out


def _reason(f: dict) -> str:
    """What a cleared suspect did in the run, from the tape."""
    bits = [
        f"{f['pts']} point{'s' if f['pts'] != 1 else ''} in the run"
        if f["pts"]
        else "no points in the run"
    ]
    if f["threes"]:
        bits.append(f"{f['threes']} three{'s' if f['threes'] > 1 else ''}")
    if f["ast"]:
        bits.append(f"{f['ast']} assist{'s' if f['ast'] > 1 else ''}")
    if f["stl"]:
        bits.append(f"{f['stl']} steal{'s' if f['stl'] > 1 else ''}")
    if f["blk"]:
        bits.append(f"{f['blk']} block{'s' if f['blk'] > 1 else ''}")
    text = ", ".join(bits)
    if not f["whole_run"]:
        q, clock = f["sat"][len(f["sat"]) // 2]
        when = f"{clock} of the {QUARTERS[q]}" if q in QUARTERS else f"{clock} of overtime"
        text += f", and he was on the bench at {when}"
    return text


def _alibi(f: dict) -> str:
    return f"Alibi checks out: {_reason(f)}."


def _story(c, team_name, opp_name, run, before, first, last, plays_of, team) -> str:
    f = c["run"]
    nick, opp = team_name.split()[-1], opp_name.split()[-1]
    unit = "quarter" if first["period"] <= 4 else "period"
    when = f"the {QUARTERS.get(first['period'], 'extra')} {unit}"
    lead = (f"The {nick} were down {-before} to the {opp}" if before < 0
            else f"The {nick} led the {opp} by {before}" if before > 0
            else f"The {nick} and the {opp} were tied")  # fmt: skip
    bits = [f"{f['pts']} points"]
    if f["ast"]:
        bits.append(f"{f['ast']} assist{'s' if f['ast'] > 1 else ''}")
    if f["rim"]:
        bits.append(f"{f['rim']} finish{'es' if f['rim'] > 1 else ''} at the rim")
    if f["threes"]:
        bits.append(f"{f['threes']} three{'s' if f['threes'] > 1 else ''}")
    if f["stl"]:
        bits.append(f"{f['stl']} steal{'s' if f['stl'] > 1 else ''}")
    surname = c["name"].split(" ", 1)[-1]
    mine = [
        p
        for p in plays_of
        if p["team"] == team and p["players"] and p["players"][0] == c["id"] and pbp._points(p)
    ]
    end = ""
    if mine and mine[-1] is last:
        q = last["period"]
        in_q = f"the {QUARTERS[q]} quarter" if q in QUARTERS else "overtime"
        end = f" The last basket was his too, with {last['clock']} left in {in_q}."
    g = f["game"]
    return (
        f"{lead} with {first['clock']} to play in {when}. What followed was a "
        f"{run['for']}–{run['against']} run, and {c['name']} was at the center of it: "
        f"{', '.join(bits)}.{end} {surname} finished with {g['pts']} points, {g['reb']} "
        f"rebounds and {g['ast']} assists in {g['minutes']} minutes."
    )


# --- the bank -------------------------------------------------------------------------------
def difficulty(case: dict[str, Any]) -> str:
    """How obvious the culprit is: Easy if he was his team's leading scorer that season."""
    c = case["_meta"]["culprit"]
    ppg = sorted(((s.get("season") or {}).get("pts", 0), s["id"]) for s in case["suspects"])[::-1]
    rank = [pid for _, pid in ppg].index(c) + 1
    return "Easy" if rank == 1 else "Medium" if rank == 2 else "Hard"


def select(cases: list[dict[str, Any]], count: int, seed: int = 2026) -> list[dict[str, Any]]:
    """A year of cases: mostly Medium and Hard, the most dramatic of each, shuffled so the
    same team never appears two days running."""
    by = defaultdict(list)
    for c in cases:
        c["difficulty"] = difficulty(c)
        by[c["difficulty"]].append(c)
    want = {"Hard": round(count * 0.4), "Medium": round(count * 0.4)}
    want["Easy"] = count - want["Hard"] - want["Medium"]

    def drama(c: dict) -> tuple:
        m = c["_meta"]
        return (m["swing"] + 6 * m["flipped"] + 4 * m["q4"], c["game_id"])

    picked = []
    for level, n in want.items():
        picked += sorted(by[level], key=drama, reverse=True)[:n]
    short = count - len(picked)
    if short > 0:  # not enough of one level: fill with the next most dramatic
        rest = sorted((c for c in cases if c not in picked), key=drama, reverse=True)
        picked += rest[:short]
    rng = random.Random(seed)
    rng.shuffle(picked)
    out: list[dict] = []
    while picked:
        i = next(
            (
                k
                for k, c in enumerate(picked)
                if not out or c["team"]["id"] != out[-1]["team"]["id"]
            ),
            0,
        )
        out.append(picked.pop(i))
    return out


def public(case: dict[str, Any], n: int) -> dict[str, Any]:
    """What gets published: everything but the generator's notes."""
    return {"n": n, **{k: v for k, v in case.items() if not k.startswith("_") and k != "game_id"}}
