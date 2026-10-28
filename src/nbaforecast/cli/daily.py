"""`nbaf` commands: what the daily GitHub Actions job runs against the data branch."""

import argparse
import logging
import os

from nbaforecast import config
from nbaforecast.cli.common import _code_sha, _today_eastern


def _daily(args: argparse.Namespace) -> int:
    import json
    import subprocess
    from datetime import UTC, datetime
    from pathlib import Path

    import requests

    from nbaforecast.ingest import espn, odds
    from nbaforecast.pipeline import live

    session = requests.Session()
    odds_events = None
    if key := os.environ.get("ODDS_API_KEY"):
        try:
            odds_events = odds.fetch_odds(key, session)
        except requests.RequestException as exc:
            # The model's predictions matter more than the market comparison: carry on.
            logging.warning("odds unavailable, predicting without market: %s", exc)
    else:
        logging.warning("ODDS_API_KEY not set; predicting without market odds")

    sha = (
        os.environ.get("GITHUB_SHA")
        or subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=config.REPO_ROOT
        ).stdout.strip()
    )
    summary = live.daily(
        Path(args.data_dir),
        today=_today_eastern(),
        now=datetime.now(UTC),
        fetch=lambda day: espn.fetch_scoreboard(day, session),
        odds_events=odds_events,
        code_sha=sha[:12],
    )
    print(json.dumps(summary, indent=2))
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        card = summary["scorecard"]
        with open(step_summary, "a") as f:
            f.write(
                f"### Daily run\n- results ingested: {summary['results_ingested']}\n"
                f"- ratings through: {summary['ratings_through']}\n"
                f"- games predicted: {summary['predicted']} "
                f"({summary['with_market']} with market odds)\n"
                f"- season so far: {card['games_scored']} games scored, Elo {card['elo']}\n"
            )
    return 0


def _shadow(args: argparse.Namespace) -> int:
    import json
    from datetime import UTC, datetime
    from pathlib import Path

    import requests

    from nbaforecast.ingest.injuries import fetch_summary
    from nbaforecast.pipeline import shadow

    session = requests.Session()
    summary = shadow.run(
        Path(args.data_dir),
        today=_today_eastern(),
        now=datetime.now(UTC),
        fetch_summary=lambda game_id: fetch_summary(game_id, session),
        model=shadow.Model.load(),
        code_sha=_code_sha(),
    )
    print(json.dumps(summary, indent=2))
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        card = summary["scorecard"]
        with open(step_summary, "a") as f:
            f.write(
                f"### Hybrid shadow run\n"
                f"- box scores ingested: {summary['ingested_box_scores']} "
                f"({summary['ingested_missing']} missing)\n"
                f"- games predicted: {summary['predicted']}\n"
                f"- season so far: {card['games_scored']} games, hybrid {card['hybrid']}, "
                f"Elo {card['elo']}\n"
            )
    return 0


def _shadow_v2(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime
    from pathlib import Path

    from nbaforecast.pipeline import shadow_v2

    out = shadow_v2.predict(Path(args.data_dir), _today_eastern(), datetime.now(UTC), _code_sha())
    for r in out["rows"]:
        missing = "; ".join(x for x in (r["missing_away"], r["missing_home"]) if x)
        print(
            f"{r['away_team']} @ {r['home_team']}: home {float(r['p_home']):.1%} "
            f"(Elo {float(r['elo_p_home']):.1%})" + (f" · out: {missing}" if missing else "")
        )
    if out.get("skipped"):
        print(f"Hybrid v2: no predictions today ({out['skipped']})")
    print(f"Hybrid v2: {out['predicted']} predicted, {out['already_started']} already started")
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a") as f:
            f.write(f"### Hybrid v2 shadow\n- predicted: {out['predicted']}\n")
    return 0


def _project(args: argparse.Namespace) -> int:
    from pathlib import Path

    from nbaforecast.pipeline import projections

    out = projections.run(Path(args.data_dir), sims=args.sims)
    top = sorted(out["teams"].items(), key=lambda kv: -kv[1]["title"])[:5]
    names = projections.franchises()
    print(f"Projected {out['season']} from {out['simulations']:,} simulations")
    for team, v in top:
        print(f"  {names[team]['abbr']:5} {v['proj_wins']:.1f} wins, title {v['title']:.1%}")
    return 0


def _rosters(args: argparse.Namespace) -> int:
    from pathlib import Path

    from nbaforecast.ingest import rosters
    from nbaforecast.pipeline import projections

    count = rosters.run(Path(args.data_dir), sorted(projections.franchises()))
    print(f"Wrote {rosters.directory_path(Path(args.data_dir))}: {count} players")
    return 0


def _box_sync(args: argparse.Namespace) -> int:
    from pathlib import Path

    import requests

    from nbaforecast.ingest.injuries import fetch_summary
    from nbaforecast.pipeline import boxlog, court, live

    store = live.Store(Path(args.data_dir))
    state = store.load_state()
    session = requests.Session()
    cache: dict[str, dict] = {}

    def fetch(game_id: str) -> dict:  # box scores, shots and stints share one download
        if game_id not in cache:
            cache[game_id] = fetch_summary(game_id, session)
        return cache[game_id]

    out = boxlog.sync(store, state.season, fetch, state.last_date)
    print(f"Box scores for {state.season}: {out['games_added']} games added, "
          f"{out['missing']} not available yet")  # fmt: skip
    out = court.sync(store, state.season, fetch, state.last_date)
    print(f"Shots and stints for {state.season}: {out['games_added']} games added, "
          f"{out['missing']} without play-by-play yet")  # fmt: skip
    return 0


def _impact(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime
    from pathlib import Path

    from nbaforecast.models import impact
    from nbaforecast.pipeline import boxlog, court, live

    root = Path(args.data_dir)
    season = live.Store(root).load_state().season
    seasons = [court.previous_season(season), season]
    stints = [r for s in seasons for r in live.Store.read(court.stints_path(root, s))]
    if not stints:
        print("No stints yet: nothing to fit.")
        return 0
    # box rates for the prior: last season's career lines plus this season's box scores
    lines = [
        {"player_id": pid, "minutes": s["min"] * s["g"], "blk": s["blk"] * s["g"],
         "stl": s["stl"] * s["g"], "reb": s["reb"] * s["g"]}
        for pid, career in boxlog.load_careers(root).items()
        for s in career
        if s["season"] == seasons[0]
    ]  # fmt: skip
    lines += [r for r in live.Store.read(boxlog.path(root, season))
              if r["stats_missing"] != "True" and r["minutes"]]  # fmt: skip
    result = impact.fit(stints, impact.per36_rates(lines))
    path = impact.write(root, result, seasons, datetime.now(UTC))
    print(f"Wrote {path}: {len(result['players'])} players from {len(stints)} stint ends, "
          f"home court {result['home_court']:+.2f} per 100")  # fmt: skip
    return 0


def _venues(args: argparse.Namespace) -> int:
    from datetime import datetime
    from pathlib import Path
    from zoneinfo import ZoneInfo

    import requests

    from nbaforecast.ingest import espn, venues

    session = requests.Session()
    today = datetime.now(ZoneInfo("America/New_York")).date()
    n = venues.run(Path(args.data_dir), today, lambda d: espn.fetch_scoreboard(d, session))
    print(f"Venues: {n} games from {today} on")
    return 0


def _verify(args: argparse.Namespace) -> int:
    import subprocess
    from pathlib import Path

    from nbaforecast.pipeline import live

    root = Path(args.data_dir)

    def committed(rel: Path) -> str | None:
        out = subprocess.run(
            ["git", "-C", str(root), "show", f"HEAD:{rel.as_posix()}"],
            capture_output=True,
            text=True,
        )
        return out.stdout if out.returncode == 0 else None

    problems = live.verify(root, committed)
    for problem in problems:
        print(f"verify failed: {problem}")
    print("verify: ok" if not problems else f"verify: {len(problems)} problem(s)")
    return 1 if problems else 0


def register(sub: argparse._SubParsersAction) -> None:
    daily = sub.add_parser("daily", help="ingest results, update ratings, predict today")
    daily.add_argument("--data-dir", required=True, help="checkout of the data branch")
    daily.set_defaults(func=_daily)

    sh = sub.add_parser("shadow", help="hybrid shadow run: ingest box scores, predict today")
    sh.add_argument("--data-dir", required=True, help="checkout of the data branch")
    sh.set_defaults(func=_shadow)

    pj = sub.add_parser("project", help="simulate the season: projected records, playoff odds")
    pj.add_argument("--data-dir", required=True, help="checkout of the data branch")
    pj.add_argument("--sims", type=int, default=10_000, help="number of simulated seasons")
    pj.set_defaults(func=_project)

    ro = sub.add_parser("rosters", help="refresh the player directory (names, injury status)")
    ro.add_argument("--data-dir", required=True, help="checkout of the data branch")
    ro.set_defaults(func=_rosters)

    bx = sub.add_parser(
        "box-sync", help="add this season's new box scores, shots and stints to the data branch"
    )
    bx.add_argument("--data-dir", required=True, help="checkout of the data branch")
    bx.set_defaults(func=_box_sync)

    im = sub.add_parser("impact", help="fit player impact ratings (RAPM) from the stint logs")
    im.add_argument("--data-dir", required=True, help="checkout of the data branch")
    im.set_defaults(func=_impact)

    sv = sub.add_parser("shadow-v2", help="hybrid v2 shadow predictions for today's games")
    sv.add_argument("--data-dir", required=True, help="checkout of the data branch")
    sv.set_defaults(func=_shadow_v2)

    ve = sub.add_parser("venues", help="where today's and the next two days' games are played")
    ve.add_argument("--data-dir", required=True, help="checkout of the data branch")
    ve.set_defaults(func=_venues)

    ver = sub.add_parser("verify", help="check the data branch before committing")
    ver.add_argument("--data-dir", required=True, help="checkout of the data branch")
    ver.set_defaults(func=_verify)
