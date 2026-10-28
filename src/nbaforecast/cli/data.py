"""`nbaf` commands: build and refresh the local data (run locally, not in CI)."""

import argparse
import os
from collections import Counter

from nbaforecast import config
from nbaforecast.cli.common import _today_eastern

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


def _plays(args: argparse.Namespace) -> int:
    import pandas as pd
    import requests

    from nbaforecast.ingest import playbyplay
    from nbaforecast.ingest.injuries import fetch_summary

    data_dir = config.local_data_dir()
    session = requests.Session()
    for season in args.seasons:
        games = pd.read_parquet(data_dir / "raw" / "espn_games" / f"season={season}.parquet")
        games = games[(games["season_type"] == "regular_season") & games["completed"]]
        fetched, missing = playbyplay.fetch_plays(
            data_dir, games[["season", "game_id"]], lambda g: fetch_summary(g, session),
            pause=args.pause,
        )  # fmt: skip
        print(f"{season}: {fetched} games fetched, {missing} without plays, the rest cached")
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


def _careers(args: argparse.Namespace) -> int:
    import json
    from datetime import UTC, datetime
    from pathlib import Path

    from nbaforecast import warehouse
    from nbaforecast.ingest import rosters
    from nbaforecast.pipeline import boxlog

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
    from nbaforecast import warehouse
    from nbaforecast.pipeline import seasonelo

    state = seasonelo.season_start_state(warehouse.load_games(), args.season)
    path = seasonelo.write_start(state, args.season)
    print(f"Wrote {path}: {len(state.ratings)} teams, home court {state.home_court():.1f}")
    return 0


def _court_backfill(args: argparse.Namespace) -> int:
    import time
    from pathlib import Path

    import requests

    from nbaforecast import warehouse
    from nbaforecast.ingest.injuries import fetch_summary
    from nbaforecast.pipeline import court, live

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


def _coldcase_bank(args: argparse.Namespace) -> int:
    import gzip
    import json
    from collections import defaultdict

    import pandas as pd
    import requests

    from nbaforecast import config, warehouse
    from nbaforecast.games import coldcase
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


def _bootstrap(args: argparse.Namespace) -> int:
    from datetime import date, timedelta
    from pathlib import Path

    from nbaforecast import warehouse
    from nbaforecast.pipeline import live

    root = Path(args.out)
    through = date.fromisoformat(args.through) if args.through else _today_eastern() - timedelta(1)
    state = live.bootstrap(root, warehouse.load_games(), through)
    readme = root / "README.md"
    if not readme.exists():
        readme.write_text(DATA_BRANCH_README)
    print(f"Bootstrapped {root}: ratings through {state.last_date}, season {state.season}, ")
    print(f"home court now {state.home_court():.1f} Elo points ({live.MODEL_NAME})")
    return 0


def _shadow_bootstrap(args: argparse.Namespace) -> int:
    from datetime import date
    from pathlib import Path

    from nbaforecast import warehouse
    from nbaforecast.pipeline import live, shadow

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


def register(sub: argparse._SubParsersAction) -> None:
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

    pl = sub.add_parser(
        "plays", help="cache full play-by-play for whole seasons (local; ~5 min per season)"
    )
    pl.add_argument("--seasons", nargs="+", required=True, help="e.g. 2017-18 2018-19")
    pl.add_argument("--pause", type=float, default=0.15, help="seconds between requests")
    pl.set_defaults(func=_plays)

    dbt = sub.add_parser("dbt", help="run dbt against the local data directory")
    dbt.add_argument("dbt_args", nargs=argparse.REMAINDER, help="e.g. build, test, docs generate")
    dbt.set_defaults(func=_dbt)

    boot = sub.add_parser("bootstrap", help="create the first live state from local history")
    boot.add_argument("--out", required=True, help="checkout of the data branch")
    boot.add_argument("--through", help="last date to rate (default: yesterday, US Eastern)")
    boot.set_defaults(func=_bootstrap)

    sb = sub.add_parser("shadow-bootstrap", help="create the hybrid's first shadow state")
    sb.add_argument("--data-dir", required=True, help="checkout of the data branch")
    sb.add_argument("--through", help="last date to include (default: Elo's ratings_through)")
    sb.set_defaults(func=_shadow_bootstrap)

    cc = sub.add_parser("coldcase-bank", help="generate the Cold Case bank (local, ~5 min)")
    cc.add_argument("--seasons", nargs="+", default=["2024-25", "2025-26"])
    cc.add_argument("--count", type=int, default=365, help="cases to keep (one per day)")
    cc.set_defaults(func=_coldcase_bank)

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
