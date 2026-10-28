"""Command-line entry point: `nbaf <command>`. Commands are added as each phase lands."""

import argparse
import logging
import os
from collections import Counter

from nbaforecast import __version__, config


def _backfill(args: argparse.Namespace) -> int:
    from nbaforecast.ingest.backfill import backfill

    data_dir = config.local_data_dir()
    print(f"Backfilling into {data_dir}")
    results = backfill(data_dir, args.first, args.last, force=args.force)
    counts = Counter(r.status for r in results)
    games = sum(r.games for r in results)
    print(f"Done: {dict(counts)}, {games:,} games written")
    return 0


def _boxscores(args: argparse.Namespace) -> int:
    from nbaforecast.ingest.boxscores import backfill

    data_dir = config.local_data_dir()
    print(f"Backfilling player box scores into {data_dir}")
    results = backfill(data_dir, args.first, args.last, limit=args.limit, pause=args.pause)
    for r in results:
        print(
            f"{r.season}: {r.status}, {r.games} games, {r.rows:,} player rows, {r.missing} missing"
        )
    return 0


def _pbp(args: argparse.Namespace) -> int:
    import requests

    from nbaforecast.ingest import playbyplay
    from nbaforecast.ingest.injuries import fetch_summary

    data_dir = config.local_data_dir()
    if not args.rebuild_only:
        games = playbyplay.select_games(data_dir, args.per_season)
        blank = int(games["blank"].sum())
        print(f"Caching plays for {blank:,} blank games + {len(games) - blank:,} to validate on")
        session = requests.Session()
        fetched, missing = playbyplay.fetch_plays(
            data_dir, games, lambda gid: fetch_summary(gid, session), pause=args.pause
        )
        print(f"Fetched {fetched:,} games ({missing} without plays)")
    written = playbyplay.rebuild(data_dir)
    for season, rows in written.items():
        print(f"{season}: {rows:,} rebuilt player rows")
    return 0


def _hybrid(args: argparse.Namespace) -> int:
    from nbaforecast import hybrid_backtest, warehouse

    inputs = hybrid_backtest.Inputs(
        warehouse.load_games(), warehouse.load_team_games(), warehouse.load_player_games()
    )
    print("Tuning the hybrid on training seasons (each player setting is a ~30 s walk)")
    best, trials = hybrid_backtest.tune(inputs)
    print(f"Chosen: {best['fit'].label()}; {best['players'].label()}; form={best['form_decay']}")
    result = hybrid_backtest.compare(inputs, best)
    out_dir = config.PUBLISHED_DIR / "hybrid"
    hybrid_backtest.write(result, trials, out_dir)
    cols = ["model", "train_log_loss", "log_loss", "brier", "accuracy", "reliability"]
    print(result["holdout"][cols].to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    for name, diff in result["paired"].items():
        lo, hi = diff["ci95"]
        print(f"{name}: {diff['mean_difference']:+.4f} (95% CI {lo:+.4f} to {hi:+.4f})")
    print(result["absences"].to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    print(f"Wrote {out_dir}")
    return 0


def _dbt(args: argparse.Namespace) -> int:
    """Run dbt with this repo's project and profile, pointed at the local data directory."""
    from dbt.cli.main import dbtRunner

    data_dir = config.local_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["NBAF_DATA_DIR"] = str(data_dir.resolve())
    dbt_dir = str(config.REPO_ROOT / "dbt")
    result = dbtRunner().invoke(
        [*args.dbt_args, "--project-dir", dbt_dir, "--profiles-dir", dbt_dir]
    )
    return 0 if result.success else 1


def _backtest(args: argparse.Namespace) -> int:
    from nbaforecast import backtest, warehouse

    games = warehouse.load_games()
    print(f"Backtesting {len(backtest.DEFAULT_GRID)} Elo configurations on {len(games):,} games")
    report = backtest.run(games)
    out_dir = config.PUBLISHED_DIR / "backtest"
    backtest.write(report, out_dir)
    # Full per-game predictions stay local (raw-sized data never goes in git).
    local = config.local_data_dir() / "backtest"
    local.mkdir(parents=True, exist_ok=True)
    report.predictions.to_parquet(local / "elo_predictions.parquet", index=False)
    print(f"Chosen: {report.champion.label()}")
    print(report.holdout.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    print(f"Wrote {out_dir} and {local / 'elo_predictions.parquet'}")
    return 0


DATA_BRANCH_README = """# nba-forecast: data branch

Written by the daily GitHub Actions job (`nbaf daily`) in
[garettprestholdt/nba-forecast](https://github.com/garettprestholdt/nba-forecast).
No code lives here.

- `predictions/<season>.csv`: **append-only.** One row per game, committed before tip-off.
  The git history of this branch is the timestamped record; force-pushes are blocked.
- `results/<season>.csv`: final scores, as ingested each morning
- `scorecard/<season>.json`: season-to-date log-loss, Brier score and accuracy for the model,
  the betting market (same games), and a coin flip
- `state/elo_state.json`: the ratings and settings the next run resumes from
- `availability/`, `injuries/`, `recaps/`: injury calls (append-only), the injury lists they
  were made from, and the morning recaps
- `shadow/hybrid/<season>.csv`: **append-only.** The challenger (team + player hybrid) model's
  predictions, logged before tip-off next to Elo's but not the published forecast
- `scorecard/<season>-hybrid.json`: the challenger vs. Elo (and the market) on the same games
- `state/hybrid_state.json`: player ratings and team form the challenger resumes from
- `projections/<season>.json`, `schedule/<season>.csv`: projected records and playoff odds from
  10,000 simulated seasons, and the schedule they were simulated from (rewritten daily)
- `players/directory.json`: current rosters and injury status, for the site's player cards
- `boxscores/<season>.csv`: **append-only.** Every player's box score this season
- `players/careers.json`: season-by-season stat lines for current players (earlier seasons)

A forecasting and calibration study, not betting advice.
"""


def _today_eastern():
    from datetime import datetime

    from nbaforecast.ingest.espn import EASTERN

    return datetime.now(EASTERN).date()


def _bootstrap(args: argparse.Namespace) -> int:
    from datetime import date, timedelta
    from pathlib import Path

    from nbaforecast import live, warehouse

    root = Path(args.out)
    through = date.fromisoformat(args.through) if args.through else _today_eastern() - timedelta(1)
    state = live.bootstrap(root, warehouse.load_games(), through)
    readme = root / "README.md"
    if not readme.exists():
        readme.write_text(DATA_BRANCH_README)
    print(f"Bootstrapped {root}: ratings through {state.last_date}, season {state.season}, ")
    print(f"home court now {state.home_court():.1f} Elo points ({live.MODEL_NAME})")
    return 0


def _daily(args: argparse.Namespace) -> int:
    import json
    import subprocess
    from datetime import UTC, datetime
    from pathlib import Path

    import requests

    from nbaforecast import live
    from nbaforecast.ingest import espn, odds

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


def _code_sha() -> str:
    import subprocess

    return (
        os.environ.get("GITHUB_SHA")
        or subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=config.REPO_ROOT
        ).stdout.strip()
    )[:12]


def _hybrid_fit(args: argparse.Namespace) -> int:
    import json

    from nbaforecast import shadow, warehouse

    chosen = json.loads((config.PUBLISHED_DIR / "hybrid" / "summary.json").read_text())["chosen"]
    model = shadow.fit_model(
        warehouse.load_games(), warehouse.load_team_games(), warehouse.load_player_games(),
        chosen, args.last_season,
    )  # fmt: skip
    shadow.MODEL_PATH.write_text(json.dumps(model.to_dict(), indent=2) + "\n")
    print(f"Wrote {shadow.MODEL_PATH}: fit on {model.meta['fit_seasons']}")
    print(json.dumps(model.fit.coefficients(), indent=2))
    return 0


def _shadow_bootstrap(args: argparse.Namespace) -> int:
    from datetime import date
    from pathlib import Path

    from nbaforecast import live, shadow, warehouse

    root = Path(args.data_dir)
    elo_through = live.Store(root).load_state().last_date
    through = date.fromisoformat(args.through) if args.through else elo_through
    state = shadow.bootstrap(
        root, shadow.Model.load(), warehouse.load_games(), warehouse.load_team_games(),
        warehouse.load_player_games(), through,
    )  # fmt: skip
    print(
        f"Bootstrapped {shadow.state_path(root)}: through {state.last_date}, "
        f"{len(state.to_dict()['players']['players'])} players kept"
    )
    return 0


def _shadow(args: argparse.Namespace) -> int:
    import json
    from datetime import UTC, datetime
    from pathlib import Path

    import requests

    from nbaforecast import shadow
    from nbaforecast.ingest.injuries import fetch_summary

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


def _project(args: argparse.Namespace) -> int:
    from pathlib import Path

    from nbaforecast import projections

    out = projections.run(Path(args.data_dir), sims=args.sims)
    top = sorted(out["teams"].items(), key=lambda kv: -kv[1]["title"])[:5]
    names = projections.franchises()
    print(f"Projected {out['season']} from {out['simulations']:,} simulations")
    for team, v in top:
        print(f"  {names[team]['abbr']:5} {v['proj_wins']:.1f} wins, title {v['title']:.1%}")
    return 0


def _rosters(args: argparse.Namespace) -> int:
    from pathlib import Path

    from nbaforecast import projections
    from nbaforecast.ingest import rosters

    count = rosters.run(Path(args.data_dir), sorted(projections.franchises()))
    print(f"Wrote {rosters.directory_path(Path(args.data_dir))}: {count} players")
    return 0


def _box_sync(args: argparse.Namespace) -> int:
    from pathlib import Path

    import requests

    from nbaforecast import boxlog, court, live
    from nbaforecast.ingest.injuries import fetch_summary

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

    from nbaforecast import boxlog, court, impact, live

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


def _coldcase_bank(args: argparse.Namespace) -> int:
    import gzip
    import json
    from collections import defaultdict

    import pandas as pd
    import requests

    from nbaforecast import coldcase, config, warehouse
    from nbaforecast.ingest import playbyplay
    from nbaforecast.ingest.athletes import Heights
    from nbaforecast.ingest.injuries import fetch_summary

    data_dir = config.local_data_dir()
    raw = data_dir / "raw" / "espn_games"
    games = pd.concat(pd.read_parquet(raw / f"season={s}.parquet") for s in args.seasons)
    games = games[(games["season_type"] == "regular_season") & games["completed"]]
    session = requests.Session()
    fetched, missing = playbyplay.fetch_plays(
        data_dir, games[["season", "game_id"]], lambda g: fetch_summary(g, session)
    )
    print(f"Plays: {fetched} games fetched, {missing} missing, the rest cached")

    box = warehouse.load_box_rows(min(args.seasons))
    box = box[box["season"].isin(args.seasons) & (box["minutes"].fillna(0) > 0)]
    lines = {}
    for (season, team, pid), g in box.groupby(["season", "team_id", "player_id"]):
        n = len(g)
        per_game = {k: round(float(g[k].sum()) / n, 1) for k in ("pts", "reb", "ast", "stl", "blk")}
        lines[(season, str(team), str(pid))] = {"g": n, **per_game}

    heights = Heights(data_dir, session)
    cases = []
    for season in args.seasons:
        folder = playbyplay.plays_dir(data_dir, season)
        rows = {str(r["game_id"]): r for r in games[games["season"] == season].to_dict("records")}
        slims = {
            f.name.split(".")[0]: json.loads(gzip.decompress(f.read_bytes()))
            for f in sorted(folder.glob("*.json.gz"))
            if f.name.split(".")[0] in rows
        }
        mix = coldcase.shot_profiles(slims.values())
        season_lines = defaultdict(dict)
        for (s, team, pid), line in lines.items():
            if s == season:
                season_lines[(team, pid)] = line
        found = 0
        for gid, slim in slims.items():
            r = rows[gid]
            game = {**r, "game_id": gid, "game_date": str(r["game_date"]),
                    "home_team_id": str(r["home_team_id"]),
                    "away_team_id": str(r["away_team_id"])}  # fmt: skip
            try:
                case = coldcase.build_case(slim, game, season_lines, mix, heights)
            except (ValueError, KeyError) as exc:  # odd clocks or rosters in a handful of games
                print(f"  skipped {gid}: {exc}")
                continue
            if case:
                cases.append(case)
                found += 1
        print(f"{season}: {found} usable cases from {len(slims)} games")
    heights.save()

    chosen = coldcase.select(cases, args.count)
    out = coldcase.bank_dir(config.PUBLISHED_DIR)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("case-*.json"):
        old.unlink()
    index = []
    for n, case in enumerate(chosen, start=1):
        coldcase.case_path(config.PUBLISHED_DIR, n).write_text(
            json.dumps(coldcase.public(case, n), separators=(",", ":"), ensure_ascii=False) + "\n"
        )
        index.append({"n": n, "title": case["title"], "difficulty": case["difficulty"]})
    (out / "index.json").write_text(json.dumps(
        {"start": coldcase.START.isoformat(), "cases": index}, indent=1) + "\n")  # fmt: skip
    levels = {d: sum(1 for c in chosen if c["difficulty"] == d) for d in ("Easy", "Medium", "Hard")}
    print(f"Wrote {len(chosen)} cases to {out} ({levels}), first on {coldcase.START}")
    return 0


def _impact_backtest(args: argparse.Namespace) -> int:
    import logging

    from nbaforecast import config, impact_backtest, warehouse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    games = warehouse.load_games()
    feats = impact_backtest.build(
        games, warehouse.load_team_games(), warehouse.load_player_games(),
        warehouse.load_box_rows("2016-17"), config.local_data_dir(),
    )  # fmt: skip
    result = impact_backtest.run(feats)
    impact_backtest.write(result)
    cols = ["model", "train_from", "features", "in_sample_log_loss", "log_loss", "brier",
            "reliability"]  # fmt: skip
    print(result["models"][cols].to_string(index=False))
    print(f"Wrote {impact_backtest.OUT_DIR}")
    return 0


def _hybrid_v2_fit(args: argparse.Namespace) -> int:
    import json
    import logging

    from nbaforecast import config, impact_backtest, shadow_v2, warehouse

    logging.basicConfig(level=logging.WARNING)
    feats = impact_backtest.build(
        warehouse.load_games(), warehouse.load_team_games(), warehouse.load_player_games(),
        warehouse.load_box_rows("2016-17"), config.local_data_dir(),
    )  # fmt: skip
    model = shadow_v2.fit_model(feats, impact_backtest.chosen())
    shadow_v2.MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    shadow_v2.MODEL_PATH.write_text(json.dumps(model, indent=1) + "\n")
    print(f"Wrote {shadow_v2.MODEL_PATH} (fit through {model['fit_through']})")
    for k, v in model["coefficients"].items():
        print(f"  {k:20s} {v:+.4f}")
    return 0


def _shadow_v2(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime
    from pathlib import Path

    from nbaforecast import shadow_v2

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


def _court_backfill(args: argparse.Namespace) -> int:
    import time
    from pathlib import Path

    import requests

    from nbaforecast import court, live, warehouse
    from nbaforecast.ingest.injuries import fetch_summary

    root = Path(args.data_dir)
    games = warehouse.load_games()
    games = games[(games["season"] == args.season) & (games["season_type"] == "regular_season")]
    done = {r["game_id"] for r in live.Store.read(court.stints_path(root, args.season))}
    session = requests.Session()
    added = missing = 0
    for g in games.to_dict("records"):
        g = {**g, "game_id": str(g["game_id"]), "home_team_id": str(g["home_team_id"]),
             "away_team_id": str(g["away_team_id"]), "game_date": str(g["game_date"])}  # fmt: skip
        if g["game_id"] in done:
            continue
        try:
            summary = fetch_summary(g["game_id"], session)
        except requests.HTTPError:
            missing += 1
            continue
        if court.write_game(root, args.season, summary, g):
            added += 1
        else:
            missing += 1
        if (added + missing) % 100 == 0:
            print(f"  {added + missing}/{len(games)} games", flush=True)
        time.sleep(args.pause)
    print(f"Shots and stints for {args.season}: {added} games added, {missing} missing")
    return 0


def _careers(args: argparse.Namespace) -> int:
    import json
    from datetime import UTC, datetime
    from pathlib import Path

    from nbaforecast import boxlog, warehouse
    from nbaforecast.ingest import rosters

    root = Path(args.data_dir)
    rows = warehouse.load_box_rows(args.first_season)
    rows = rows[rows["season"] <= args.last_season]
    keep = set(rows.loc[rows["season"] >= args.active_since, "player_id"])
    keep |= set(rosters.load_directory(root))
    careers = boxlog.build_careers(rows, keep)
    out = boxlog.careers_path(root)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "through_season": args.last_season,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "players": careers,
    }
    out.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
    print(f"Wrote {out}: {len(careers)} players, {out.stat().st_size // 1024} KB")
    return 0


def _season_start(args: argparse.Namespace) -> int:
    from nbaforecast import seasonelo, warehouse

    state = seasonelo.season_start_state(warehouse.load_games(), args.season)
    path = seasonelo.write_start(state, args.season)
    print(f"Wrote {path}: {len(state.ratings)} teams, home court {state.home_court():.1f}")
    return 0


def _verify(args: argparse.Namespace) -> int:
    import subprocess
    from pathlib import Path

    from nbaforecast import live

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


def _recap_date(args: argparse.Namespace):
    from datetime import date

    return date.fromisoformat(args.date) if args.date else _today_eastern()


def _github_output(**values: str) -> None:
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a") as f:
            for key, value in values.items():
                f.write(f"{key}={value}\n")


def _recap_prepare(args: argparse.Namespace) -> int:
    from pathlib import Path

    from nbaforecast import live, recap

    store = live.Store(Path(args.data_dir))
    prompt = recap.prepare(
        store, store.load_state(), _recap_date(args), Path(args.workdir), force=args.force
    )
    print(f"Recap prompt: {prompt}" if prompt else "Nothing to recap (or already published).")
    _github_output(ready="true" if prompt else "false")
    return 0


def _recap_publish(args: argparse.Namespace) -> int:
    import json
    from pathlib import Path

    from nbaforecast import live, recap

    draft_path = Path(args.workdir) / "recap_draft.md"
    draft = draft_path.read_text() if draft_path.exists() else None
    outcome = recap.publish(live.Store(Path(args.data_dir)), _recap_date(args), draft, args.model)
    print(json.dumps(outcome, indent=2))
    _, body = recap.read_published(Path(outcome["path"]))
    print(f"--- published recap ({outcome['source']}) ---\n{body}")
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a") as f:
            f.write(f"### Recap\n- published: **{outcome['source']}**\n")
            for problem in outcome["problems"]:
                f.write(f"- fact-check: {problem}\n")
            f.write("\n" + "\n".join(f"> {line}" for line in body.splitlines()) + "\n")
    if args.require_agent and outcome["source"] != "agent":
        print("The agent's draft wasn't published (see problems above).")
        return 1
    return 0


def _triage_prepare(args: argparse.Namespace) -> int:
    from pathlib import Path

    from nbaforecast import triage

    summary = triage.prepare(args.run_id, Path(args.workdir))
    print(f"Collected failure context for {summary.get('name')}: {summary.get('url')}")
    for step in triage.failed_steps(summary):
        print(f"  failed: {step}")
    return 0


def _triage_file(args: argparse.Namespace) -> int:
    import json
    from pathlib import Path

    from nbaforecast import triage

    workdir = Path(args.workdir)
    summary = json.loads((workdir / "run.json").read_text())
    outcome = triage.file_issue(summary, workdir, dry_run=args.dry_run)
    body = outcome.pop("body", None)
    print(json.dumps(outcome, indent=2))
    if body is not None:
        print(f"--- issue that would be filed ---\n# {outcome['title']}\n\n{body}")
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a") as f:
            f.write(f"### Triage\n- {outcome['action']}\n- title: {outcome['title']}\n")
            f.write(f"- agent diagnosis: {'yes' if outcome['agent_diagnosis'] else 'no'}\n")
            if body is not None:
                f.write(f"\n---\n\n{body}\n")
    if args.require_agent and not outcome["agent_diagnosis"]:
        print("The agent didn't write a diagnosis.")
        return 1
    return 0


def _availability_prepare(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime
    from pathlib import Path

    import requests

    from nbaforecast import availability, live
    from nbaforecast.ingest import injuries

    session = requests.Session()
    summary = availability.prepare(
        live.Store(Path(args.data_dir)),
        _recap_date(args),
        datetime.now(UTC),
        Path(args.workdir),
        fetch_injuries=lambda: injuries.fetch_injuries(session),
        fetch_summary=lambda game_id: injuries.fetch_summary(game_id, session),
    )
    print(summary)
    _github_output(ready="true" if summary["candidates"] else "false")
    return 0


def _availability_publish(args: argparse.Namespace) -> int:
    import json
    from datetime import UTC, datetime
    from pathlib import Path

    from nbaforecast import availability, live

    out = availability.publish(
        live.Store(Path(args.data_dir)), _recap_date(args), datetime.now(UTC), Path(args.workdir)
    )
    calls = out.pop("calls")
    print(json.dumps(out, indent=2))
    table = ["| Player | Team | Keyword rule | Agent | Agent's quote |", "|---|---|---|---|---|"]
    for r in calls:
        agent = f"{float(r['agent_p']):.0%}" if r["agent_p"] != "" else "–"
        table.append(
            f"| {r['player']} | {r['team']} | {float(r['baseline_p']):.0%} | {agent} "
            f"| {r['agent_quote'] or '–'} |"
        )
    print("\n".join(table))
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a") as f:
            f.write(
                f"### Injury calls\n- logged: {out['logged']} "
                f"({out['agent_answers']} with an accepted agent answer)\n"
            )
            for problem in out["problems"]:
                f.write(f"- validation: {problem}\n")
            f.write("\n" + "\n".join(table) + "\n\n")
            h2h = out["scorecard"]["head_to_head"]
            f.write(
                f"- season so far: {h2h['calls']} scored head-to-head; agent {h2h['agent']}, "
                f"keyword rule {h2h['keyword_rule']}\n"
            )
    if args.require_agent and out["logged"] and not out["agent_answers"]:
        print("No agent answers were accepted.")
        return 1
    return 0


def _challenger(args: argparse.Namespace) -> int:
    from nbaforecast import challenger, warehouse

    games = warehouse.load_games()
    print(f"Tuning the Kalman challenger on {len(games):,} games (two-stage grid, train seasons)")
    result = challenger.compare(games)
    out_dir = config.PUBLISHED_DIR / "challenger"
    challenger.write(result, out_dir)
    print(f"Chosen: {result['params'].label()}")
    print(result["holdout"].to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    for name, paired in result["paired"].items():
        lo, hi = paired["ci95"]
        print(
            f"Paired log-loss, {name}: {paired['mean_difference']:+.4f} "
            f"(95% CI {lo:+.4f} to {hi:+.4f})"
        )
    print("Learned coefficients now:", {k: round(v, 2) for k, v in result["coefficients"].items()})
    print(f"Wrote {out_dir}")
    return 0


def _site(args: argparse.Namespace) -> int:
    from pathlib import Path

    from nbaforecast import web

    written = web.build(Path(args.data_dir), Path(args.out))
    print(f"Built {len(written)} pages into {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nbaf", description=__doc__)
    parser.add_argument("--version", action="version", version=f"nbaf {__version__}")
    sub = parser.add_subparsers(dest="command")

    bf = sub.add_parser("backfill", help="download historical game logs (run locally)")
    bf.add_argument("--first", type=int, default=2004, help="first season start year")
    bf.add_argument("--last", type=int, default=None, help="last season start year")
    bf.add_argument("--force", action="store_true", help="re-download finished seasons")
    bf.set_defaults(func=_backfill)

    bs = sub.add_parser("boxscores", help="download player box scores (run locally)")
    bs.add_argument("--first", type=int, default=2004, help="first season start year")
    bs.add_argument("--last", type=int, default=None, help="last season start year")
    bs.add_argument("--limit", type=int, default=None, help="pilot: at most N new games/season")
    bs.add_argument("--pause", type=float, default=0.2, help="seconds between requests")
    bs.set_defaults(func=_boxscores)

    pb = sub.add_parser("pbp", help="rebuild blank box scores from play-by-play (run locally)")
    pb.add_argument("--per-season", type=int, default=150, help="validation games per season")
    pb.add_argument("--pause", type=float, default=0.2, help="seconds between requests")
    pb.add_argument("--rebuild-only", action="store_true", help="skip fetching; rebuild the cache")
    pb.set_defaults(func=_pbp)

    hy = sub.add_parser("hybrid", help="backtest the team + player hybrid model (~15 min)")
    hy.set_defaults(func=_hybrid)

    dbt = sub.add_parser("dbt", help="run dbt against the local data directory")
    dbt.add_argument("dbt_args", nargs=argparse.REMAINDER, help="e.g. build, test, docs generate")
    dbt.set_defaults(func=_dbt)

    bt = sub.add_parser("backtest", help="tune Elo on 2005-2019, score it on 2020-2026")
    bt.set_defaults(func=_backtest)

    ch = sub.add_parser("challenger", help="tune the Kalman model and compare it with Elo")
    ch.set_defaults(func=_challenger)

    boot = sub.add_parser("bootstrap", help="create the first live state from local history")
    boot.add_argument("--out", required=True, help="checkout of the data branch")
    boot.add_argument("--through", help="last date to rate (default: yesterday, US Eastern)")
    boot.set_defaults(func=_bootstrap)

    daily = sub.add_parser("daily", help="ingest results, update ratings, predict today")
    daily.add_argument("--data-dir", required=True, help="checkout of the data branch")
    daily.set_defaults(func=_daily)

    hf = sub.add_parser("hybrid-fit", help="fit the live hybrid on every completed season")
    hf.add_argument("--last-season", default="2025-26", help="last completed season to fit on")
    hf.set_defaults(func=_hybrid_fit)

    sb = sub.add_parser("shadow-bootstrap", help="create the hybrid's first shadow state")
    sb.add_argument("--data-dir", required=True, help="checkout of the data branch")
    sb.add_argument("--through", help="last date to include (default: Elo's ratings_through)")
    sb.set_defaults(func=_shadow_bootstrap)

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

    ib = sub.add_parser(
        "impact-backtest", help="challenger with RAPM + availability: walk-forward and diagnostics"
    )
    ib.set_defaults(func=_impact_backtest)

    hv = sub.add_parser("hybrid-v2-fit", help="fit hybrid v2 (RAPM + who's missing) (local)")
    hv.set_defaults(func=_hybrid_v2_fit)

    sv = sub.add_parser("shadow-v2", help="hybrid v2 shadow predictions for today's games")
    sv.add_argument("--data-dir", required=True, help="checkout of the data branch")
    sv.set_defaults(func=_shadow_v2)

    cc = sub.add_parser("coldcase-bank", help="generate the Cold Case bank (local, ~5 min)")
    cc.add_argument("--seasons", nargs="+", default=["2024-25", "2025-26"])
    cc.add_argument("--count", type=int, default=365, help="cases to keep (one per day)")
    cc.set_defaults(func=_coldcase_bank)

    ve = sub.add_parser("venues", help="where today's and the next two days' games are played")
    ve.add_argument("--data-dir", required=True, help="checkout of the data branch")
    ve.set_defaults(func=_venues)

    cb = sub.add_parser(
        "court-backfill", help="shots and stints for a past season (local, ~10 min per season)"
    )
    cb.add_argument("--data-dir", required=True, help="checkout of the data branch")
    cb.add_argument("--season", required=True, help="e.g. 2025-26")
    cb.add_argument("--pause", type=float, default=0.2, help="seconds between requests")
    cb.set_defaults(func=_court_backfill)

    ca = sub.add_parser("careers", help="season-by-season player stats from the warehouse (local)")
    ca.add_argument("--data-dir", required=True, help="checkout of the data branch")
    ca.add_argument("--first-season", default="2004-05")
    ca.add_argument("--last-season", default="2025-26", help="last completed season")
    ca.add_argument("--active-since", default="2023-24", help="keep players active since")
    ca.set_defaults(func=_careers)

    ss = sub.add_parser("season-start", help="publish ratings at the start of a season (local)")
    ss.add_argument("--season", default="2026-27")
    ss.set_defaults(func=_season_start)

    ver = sub.add_parser("verify", help="check the data branch before committing")
    ver.add_argument("--data-dir", required=True, help="checkout of the data branch")
    ver.set_defaults(func=_verify)

    for name, func, helptext in [
        ("recap-prepare", _recap_prepare, "write recap facts and the agent's prompt"),
        ("recap-publish", _recap_publish, "fact-check the agent's draft and publish the recap"),
    ]:
        rp = sub.add_parser(name, help=helptext)
        rp.add_argument("--data-dir", required=True, help="checkout of the data branch")
        rp.add_argument("--workdir", required=True, help="isolated folder the agent works in")
        rp.add_argument("--date", help="recap date (default: today, US Eastern)")
        rp.add_argument("--model", default="", help="model the agent ran with (for the record)")
        rp.add_argument(
            "--force", action="store_true", help="recap test only: ignore an existing recap"
        )
        rp.add_argument(
            "--require-agent",
            action="store_true",
            help="recap test only: fail if the template is used",
        )
        rp.set_defaults(func=func)

    for name, func, helptext in [
        ("availability-prepare", _availability_prepare, "score past injury calls; pick today's"),
        ("availability-publish", _availability_publish, "validate the agent's calls and log them"),
    ]:
        ap = sub.add_parser(name, help=helptext)
        ap.add_argument("--data-dir", required=True, help="checkout of the data branch")
        ap.add_argument("--workdir", required=True, help="isolated folder the agent works in")
        ap.add_argument("--date", help="game date (default: today, US Eastern)")
        ap.add_argument(
            "--require-agent", action="store_true", help="test only: fail without answers"
        )
        ap.set_defaults(func=func)

    tp = sub.add_parser("triage-prepare", help="collect a failed run's context for the agent")
    tp.add_argument("--run-id", required=True, help="GitHub Actions run id that failed")
    tp.add_argument("--workdir", required=True, help="isolated folder the agent works in")
    tp.set_defaults(func=_triage_prepare)

    tf = sub.add_parser("triage-file", help="file (or update) the triage issue")
    tf.add_argument("--workdir", required=True, help="folder from triage-prepare")
    tf.add_argument("--dry-run", action="store_true", help="print the issue; file nothing")
    tf.add_argument("--require-agent", action="store_true", help="fail without a diagnosis")
    tf.set_defaults(func=_triage_file)

    site = sub.add_parser("site", help="build the static site from the data branch")
    site.add_argument("--data-dir", required=True, help="checkout of the data branch")
    site.add_argument("--out", required=True, help="output directory")
    site.set_defaults(func=_site)

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
