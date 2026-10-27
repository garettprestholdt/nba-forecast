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

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
