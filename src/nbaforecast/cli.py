"""Command-line entry point: `nbaf <command>`. Commands are added as each phase lands."""

import argparse

from nbaforecast import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nbaf", description=__doc__)
    parser.add_argument("--version", action="version", version=f"nbaf {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
