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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nbaf", description=__doc__)
    parser.add_argument("--version", action="version", version=f"nbaf {__version__}")
    sub = parser.add_subparsers(dest="command")

    bf = sub.add_parser("backfill", help="download historical game logs (run locally)")
    bf.add_argument("--first", type=int, default=2004, help="first season start year")
    bf.add_argument("--last", type=int, default=None, help="last season start year")
    bf.add_argument("--force", action="store_true", help="re-download finished seasons")
    bf.set_defaults(func=_backfill)

    dbt = sub.add_parser("dbt", help="run dbt against the local data directory")
    dbt.add_argument("dbt_args", nargs=argparse.REMAINDER, help="e.g. build, test, docs generate")
    dbt.set_defaults(func=_dbt)

    bt = sub.add_parser("backtest", help="tune Elo on 2005-2019, score it on 2020-2026")
    bt.set_defaults(func=_backtest)

    boot = sub.add_parser("bootstrap", help="create the first live state from local history")
    boot.add_argument("--out", required=True, help="checkout of the data branch")
    boot.add_argument("--through", help="last date to rate (default: yesterday, US Eastern)")
    boot.set_defaults(func=_bootstrap)

    daily = sub.add_parser("daily", help="ingest results, update ratings, predict today")
    daily.add_argument("--data-dir", required=True, help="checkout of the data branch")
    daily.set_defaults(func=_daily)

    ver = sub.add_parser("verify", help="check the data branch before committing")
    ver.add_argument("--data-dir", required=True, help="checkout of the data branch")
    ver.set_defaults(func=_verify)

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
