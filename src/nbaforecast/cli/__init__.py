"""Command-line entry point: `nbaf <command>`.

Commands live in modules by area (each has a `register` function):
`data` (build local data), `research` (backtests and model fits), `daily` (the daily job),
`agents` (agent prepare/publish steps), `site` (the static site). `nbaf --help` lists them.
"""

import argparse
import logging

from nbaforecast import __version__
from nbaforecast.cli import agents, daily, data, research, site


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nbaf",
        description="NBA win probabilities: data, backtests, the daily job, the site.",
    )
    parser.add_argument("--version", action="version", version=f"nbaf {__version__}")
    sub = parser.add_subparsers(dest="command")
    for module in (data, research, daily, agents, site):
        module.register(sub)

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
